import hmac, hashlib, time, base64, json, math, re, asyncio
from fastapi import FastAPI, Request, HTTPException
import httpx

# ====== 데모(API) 키를 여기에 넣기 ======
BITGET_API_KEY    = "bg_6b62baa07b6f09eee4c5c5dfab033555"
BITGET_API_SECRET = "1fb2ebf41c0ede17fba0bfcb109f6743e58377ec5294c5c936432f4ccdab6609"
BITGET_PASSPHRASE = "akdlsj41"
# =====================================

BITGET_BASE  = "https://api.bitget.com"   # Demo도 보통 동일
MARGIN_COIN  = "USDT"                     # USDT-M 선물
PRODUCT_TYPE = "umcbl"                    # Bitget USDT Perpetual 분류
LEVERAGE     = 10                         # 풀시드 계산 시 사용
USE_PCT      = 1.0                        # 잔고 100%

app = FastAPI()
_http = httpx.AsyncClient(timeout=10)

# ---------------- 공통 서명 ----------------
def sign(method: str, path: str, body: str=""):
    ts = str(int(time.time() * 1000))
    pre = ts + method.upper() + path + body
    sig = base64.b64encode(
        hmac.new(BITGET_API_SECRET.encode(), pre.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sig,
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": BITGET_PASSPHRASE,
        "Content-Type": "application/json"
    }

# ---------------- 심볼 유틸 ----------------
_contract_cache = {"when": 0, "data": []}

async def fetch_contracts(force=False):
    # 캐시 60초
    now = time.time()
    if not force and _contract_cache["data"] and now - _contract_cache["when"] < 60:
        return _contract_cache["data"]
    path = f"/api/mix/v1/market/contracts?productType={PRODUCT_TYPE}"
    r = await _http.get(BITGET_BASE + path)
    r.raise_for_status()
    data = r.json()["data"]
    _contract_cache["data"] = data
    _contract_cache["when"] = now
    return data

def normalize_tv_symbol(tv_sym: str) -> str:
    """
    TradingView 심볼을 Bitget 선물 심볼로 변환.
    허용 예:
      "BITGET:BAKEUSDT.P" / "BITGET:BAKEUSDT" / "BAKEUSDT.P" / "BAKEUSDT"
    결과:
      "BAKEUSDT_UMCBL"
    """
    if not tv_sym:
        raise HTTPException(400, "symbol missing")

    s = tv_sym.strip().upper()

    # 1) "EXCHANGE:SYMBOL" → SYMBOL
    if ":" in s:
        s = s.split(":")[1]

    # 2) 접미사 제거 (PERP/선물 표기)
    #   예: ".P", ".PERP", "-PERP", " PERPETUAL MIX CONTRACT" 등
    s = re.sub(r"\.P(ERP)?$", "", s)
    s = re.sub(r"[-_ ]?PERP(ETUAL)?", "", s)
    s = re.sub(r"\s+PERPETUAL MIX CONTRACT", "", s)

    # 3) 영숫자만 남기기 (안전)
    s = re.sub(r"[^A-Z0-9]", "", s)

    if not s.endswith("USDT"):
        # Bitget USDT 선물만 지원
        raise HTTPException(400, f"unsupported symbol (need *USDT): {tv_sym}")

    return f"{s}_UMCBL"

async def to_bitget_symbol_best(tv_sym: str) -> str:
    """
    표준 규칙으로 만들고, 실제 계약 리스트에서 존재하는지 확인.
    규칙 매칭 실패 시, 계약 리스트에서 유사 심볼 탐색.
    """
    target = normalize_tv_symbol(tv_sym)
    contracts = await fetch_contracts()
    all_symbols = {c["symbol"] for c in contracts}

    if target in all_symbols:
        return target

    # 규칙이 안 맞는 특이 케이스 탐색 (드물지만 대비)
    base = target.replace("_UMCBL", "")
    candidates = [s for s in all_symbols if s.startswith(base)]
    if candidates:
        return candidates[0]

    # 마지막 시도: USDT-계약 아무거나
    for s in all_symbols:
        if s.endswith("_UMCBL"):
            return s
    raise HTTPException(400, f"cannot resolve symbol: {tv_sym}")

# ---------------- 시세/스펙/잔고/포지션 ----------------
async def get_ticker(symbol_umcbl: str) -> float:
    r = await _http.get(BITGET_BASE + f"/api/mix/v1/market/ticker?symbol={symbol_umcbl}")
    r.raise_for_status()
    return float(r.json()["data"]["last"])

async def get_contract_spec(symbol_umcbl: str):
    data = await fetch_contracts()
    for it in data:
        if it.get("symbol") == symbol_umcbl:
            size_place = int(it.get("sizePlace", 0))          # 수량 소수점
            min_qty    = float(it.get("minTradeNum", 0) or 0) # 최소 수량
            return size_place, min_qty
    return 0, 0.0

async def get_available_equity_usdt() -> float:
    path = f"/api/mix/v1/account/accounts?productType={PRODUCT_TYPE}"
    headers = sign("GET", path, "")
    r = await _http.get(BITGET_BASE + path, headers=headers)
    r.raise_for_status()
    for acc in r.json()["data"]:
        if acc.get("marginCoin") == MARGIN_COIN:
            return float(acc.get("availableEquity") or acc.get("equity") or 0.0)
    raise HTTPException(500, "cannot fetch USDT futures equity")

async def get_position_size(symbol_umcbl: str):
    path = f"/api/mix/v1/position/singlePosition?symbol={symbol_umcbl}&marginCoin={MARGIN_COIN}"
    headers = sign("GET", path, "")
    r = await _http.get(BITGET_BASE + path, headers=headers)
    if r.status_code != 200:
        return 0.0, 0.0
    j = r.json()
    if not j.get("data"):
        return 0.0, 0.0
    d = j["data"]
    long_sz  = float(d.get("long", {}).get("total", 0) or 0)
    short_sz = float(d.get("short", {}).get("total", 0) or 0)
    return long_sz, short_sz

# ---------------- 수량(풀시드) 계산 ----------------
def round_qty(qty: float, size_place: int, min_qty: float) -> float:
    factor = 10 ** size_place
    q = math.floor(qty * factor) / factor
    if q < max(min_qty, 0):
        q = max(min_qty, 0)
    return q

async def compute_fullseed_qty(symbol_umcbl: str) -> float:
    price = await get_ticker(symbol_umcbl)
    size_place, min_qty = await get_contract_spec(symbol_umcbl)
    equity = await get_available_equity_usdt()
    notional = equity * USE_PCT * LEVERAGE     # 명목가 = 잔고 × 비율 × 레버리지
    raw_qty = notional / price                 # 계약 수 = 명목가 / 가격
    return round_qty(raw_qty, size_place, min_qty)

# ---------------- 주문 ----------------
async def place_order(symbol: str, side: str, size: float):
    path = "/api/mix/v1/order/placeOrder"
    url  = BITGET_BASE + path
    body = {
        "symbol": symbol,
        "marginCoin": MARGIN_COIN,
        "side": side,               # open_long/open_short/close_long/close_short
        "orderType": "market",
        "size": str(size)
    }
    headers = sign("POST", path, json.dumps(body, separators=(",", ":")))
    r = await _http.post(url, data=json.dumps(body, separators=(",", ":")), headers=headers)
    if r.status_code != 200:
        raise HTTPException(500, f"Bitget error: {r.text}")
    return r.json()

# ---------------- 엔드포인트 ----------------
@app.get("/")
async def root():
    return {"ok": True, "service": "bitget-bot", "productType": PRODUCT_TYPE}

@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/webhook")
async def tv_webhook(req: Request):
    data = await req.json()
    action = (data.get("action") or "").strip().lower()
    tv_sym = data.get("symbol")

    if action not in {"enter_long", "enter_short", "exit_long", "exit_short"}:
        raise HTTPException(400, f"unknown action: {action}")

    # 심볼 정규화/검증 (BAKEUSDT.P, BITGET:BAKEUSDT.P 모두 OK)
    symbol_umcbl = await to_bitget_symbol_best(tv_sym)

    if action == "enter_long":
        qty = await compute_fullseed_qty(symbol_umcbl)
        return await place_order(symbol_umcbl, "open_long", qty)

    if action == "enter_short":
        qty = await compute_fullseed_qty(symbol_umcbl)
        return await place_order(symbol_umcbl, "open_short", qty)

    if action == "exit_long":
        long_sz, _ = await get_position_size(symbol_umcbl)
        if long_sz <= 0:
            return {"ok": True, "msg": "no long position"}
        return await place_order(symbol_umcbl, "close_long", long_sz)

    if action == "exit_short":
        _, short_sz = await get_position_size(symbol_umcbl)
        if short_sz <= 0:
            return {"ok": True, "msg": "no short position"}
        return await place_order(symbol_umcbl, "close_short", short_sz)
