import hmac, hashlib, time, base64, json, math, re, asyncio
from fastapi import FastAPI, Request, HTTPException
import httpx

# ===== Demo API 키 넣기 =====
BITGET_API_KEY    = "bg_6b62baa07b6f09eee4c5c5dfab033555"
BITGET_API_SECRET = "1fb2ebf41c0ede17fba0bfcb109f6743e58377ec5294c5c936432f4ccdab6609"
BITGET_PASSPHRASE = "akdlsj41"
# ===========================

BITGET_BASE  = "https://api.bitget.com"   # Demo도 동일 호스트
MARGIN_COIN  = "USDT"                     # USDT-M 선물
PRODUCT_TYPE = "umcbl"                    # USDT Perp
LEVERAGE     = 10                         # 풀시드 레버리지
USE_PCT      = 1.0                        # 잔고 100%
COOLDOWN_SEC = 3                          # 중복 알림 쿨다운

app = FastAPI()
_http = httpx.AsyncClient(timeout=10)

# per-symbol 동시 처리 잠금/쿨다운용
_locks: dict[str, asyncio.Lock] = {}
_last_hit: dict[str, float] = {}

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

# ---------- 심볼 변환 ----------
_contract_cache = {"when": 0.0, "data": []}

async def fetch_contracts(force=False):
    now = time.time()
    if not force and _contract_cache["data"] and now - _contract_cache["when"] < 60:
        return _contract_cache["data"]
    path = f"/api/mix/v1/market/contracts?productType={PRODUCT_TYPE}"
    r = await _http.get(BITGET_BASE + path)
    r.raise_for_status()
    _contract_cache["data"] = r.json()["data"]
    _contract_cache["when"] = now
    return _contract_cache["data"]

def normalize_tv_symbol(tv_sym: str) -> str:
    if not tv_sym:
        raise HTTPException(400, "symbol missing")
    s = tv_sym.strip().upper()
    if ":" in s:
        s = s.split(":")[1]          # EXCHANGE:SYMBOL -> SYMBOL
    # 접미사/노이즈 제거
    s = re.sub(r"\.P(ERP)?$", "", s)             # .P / .PERP
    s = re.sub(r"[-_ ]?PERP(ETUAL)?", "", s)     # -PERP / PERPETUAL
    s = re.sub(r"\s+PERPETUAL MIX CONTRACT", "", s)
    s = re.sub(r"[^A-Z0-9]", "", s)              # 안전 필터
    if not s.endswith("USDT"):
        raise HTTPException(400, f"unsupported symbol (need *USDT): {tv_sym}")
    return f"{s}_UMCBL"

async def to_bitget_symbol_best(tv_sym: str) -> str:
    target = normalize_tv_symbol(tv_sym)
    contracts = await fetch_contracts()
    symbols = {c["symbol"] for c in contracts}
    if target in symbols:
        return target
    base = target.replace("_UMCBL", "")
    for s in symbols:
        if s.startswith(base):
            return s
    raise HTTPException(400, f"cannot resolve symbol: {tv_sym}")

# ---------- 시세/스펙/잔고/포지션 ----------
async def get_ticker(symbol_umcbl: str) -> float:
    r = await _http.get(BITGET_BASE + f"/api/mix/v1/market/ticker?symbol={symbol_umcbl}")
    r.raise_for_status()
    return float(r.json()["data"]["last"])

async def get_contract_spec(symbol_umcbl: str):
    data = await fetch_contracts()
    for it in data:
        if it.get("symbol") == symbol_umcbl:
            size_place = int(it.get("sizePlace", 0))
            min_qty    = float(it.get("minTradeNum", 0) or 0)
            return size_place, min_qty
    return 0, 0.0

async def get_available_equity_usdt() -> float:
    # ✅ accounts(복수) 대신 account(단수) + marginCoin 지정
    path = f"/api/mix/v1/account/account?productType={PRODUCT_TYPE}&marginCoin={MARGIN_COIN}"
    headers = sign("GET", path, "")
    r = await _http.get(BITGET_BASE + path, headers=headers)
    try:
        r.raise_for_status()
    except httpx.HTTPStatusError:
        # Bitget 원문 에러를 그대로 노출
        raise HTTPException(500, f"bitget account error {r.status_code}: {r.text}")
    d = r.json().get("data") or {}
    return float(d.get("availableEquity") or d.get("equity") or 0.0)

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

# ---------- 수량(풀시드) ----------
def round_qty(qty: float, size_place: int, min_qty: float) -> float:
    factor = 10 ** size_place
    q = math.floor(qty * factor) / factor
    return max(q, min_qty)

async def compute_fullseed_qty(symbol_umcbl: str) -> float:
    price = await get_ticker(symbol_umcbl)
    size_place, min_qty = await get_contract_spec(symbol_umcbl)
    equity = await get_available_equity_usdt()
    notional = equity * USE_PCT * LEVERAGE
    raw_qty = notional / price
    return round_qty(raw_qty, size_place, min_qty)

# ---------- 주문 ----------
async def place_order(symbol: str, side: str, size: float):
    path = "/api/mix/v1/order/placeOrder"
    body = {
        "symbol": symbol,
        "marginCoin": MARGIN_COIN,
        "side": side,               # open_long/open_short/close_long/close_short
        "orderType": "market",
        "size": str(size)
    }
    payload = json.dumps(body, separators=(",", ":"))
    headers = sign("POST", path, payload)
    r = await _http.post(BITGET_BASE + path, data=payload, headers=headers)
    # Bitget 원문을 그대로 돌려주어 디버깅 용이
    if r.status_code != 200:
        raise HTTPException(500, f"bitget place error {r.status_code}: {r.text}")
    return r.json()

# ---------- 유틸 ----------
def get_lock(symbol_umcbl: str) -> asyncio.Lock:
    if symbol_umcbl not in _locks:
        _locks[symbol_umcbl] = asyncio.Lock()
    return _locks[symbol_umcbl]

def cooling(symbol_umcbl: str) -> bool:
    now = time.time()
    last = _last_hit.get(symbol_umcbl, 0)
    if now - last < COOLDOWN_SEC:
        return True
    _last_hit[symbol_umcbl] = now
    return False

# ---------- 엔드포인트 ----------
@app.get("/")
async def root():
    return {"ok": True, "service": "bitget-bot", "productType": PRODUCT_TYPE}

@app.get("/probe/{tv_symbol}")
async def probe(tv_symbol: str):
    # 심볼 변환/잔고/수량 계산을 사전 점검
    s = await to_bitget_symbol_best(tv_symbol)
    qty = await compute_fullseed_qty(s)
    return {"ok": True, "symbol": s, "fullseed_qty": qty}

@app.post("/webhook")
async def tv_webhook(req: Request):
    data = await req.json()
    action = (data.get("action") or "").strip().lower()
    tv_sym = data.get("symbol")

    if action not in {"enter_long", "enter_short", "exit_long", "exit_short"}:
        raise HTTPException(400, f"unknown action: {action}")

    symbol_umcbl = await to_bitget_symbol_best(tv_sym)
    # 중복·동시 타격 보호
    if cooling(symbol_umcbl):
        return {"ok": True, "skipped": "cooldown", "symbol": symbol_umcbl}

    lock = get_lock(symbol_umcbl)
    async with lock:
        try:
            if action == "enter_long":
                qty = await compute_fullseed_qty(symbol_umcbl)
                return await place_order(symbol_umcbl, "open_long", qty)

            if action == "enter_short":
                qty = await compute_fullseed_qty(symbol_umcbl)
                return await place_order(symbol_umcbl, "open_short", qty)

            if action == "exit_long":
                long_sz, _ = await get_position_size(symbol_umcbl)
                if long_sz <= 0:
                    return {"ok": True, "msg": "no long position", "symbol": symbol_umcbl}
                return await place_order(symbol_umcbl, "close_long", long_sz)

            if action == "exit_short":
                _, short_sz = await get_position_size(symbol_umcbl)
                if short_sz <= 0:
                    return {"ok": True, "msg": "no short position", "symbol": symbol_umcbl}
                return await place_order(symbol_umcbl, "close_short", short_sz)

        except HTTPException as e:
            # Bitget 원문을 response에 남김
            return {"ok": False, "error": e.detail, "action": action, "symbol": symbol_umcbl}
