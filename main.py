import hmac, hashlib, time, base64, json, math, re, asyncio
from fastapi import FastAPI, Request, HTTPException
import httpx

# ===== 여기에 데모(API) 키 넣기 =====
BITGET_API_KEY    = "bg_6b62baa07b6f09eee4c5c5dfab033555"
BITGET_API_SECRET = "1fb2ebf41c0ede17fba0bfcb109f6743e58377ec5294c5c936432f4ccdab6609"
BITGET_PASSPHRASE = "akdlsj41"
# ===================================

BITGET_BASE  = "https://api.bitget.com"   # 데모도 동일 호스트
PRODUCT_TYPE = "USDT-FUTURES"             # v2 표기
MARGIN_COIN  = "USDT"
LEVERAGE     = 10                          # 풀시드 계산에 사용(거래소 레버리지는 Bitget 설정값 사용)
USE_PCT      = 1.0                         # 잔고 100%
COOLDOWN_SEC = 3                           # 동일 심볼 연속 신호 쿨다운

app = FastAPI()
_http = httpx.AsyncClient(timeout=10)

# 동시 신호 잠금/쿨다운
_locks: dict[str, asyncio.Lock] = {}
_last_hit: dict[str, float] = {}

# ---------- 공통 서명(데모 헤더 필수) ----------
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
        "Content-Type": "application/json",
        "paptrading": "1",  # 데모 전용 헤더(실계정 전환 시 제거)
    }

# ---------- 심볼 변환/계약 캐시 ----------
_contract_cache = {"when": 0.0, "data": []}

async def fetch_contracts(force=False):
    now = time.time()
    if not force and _contract_cache["data"] and now - _contract_cache["when"] < 60:
        return _contract_cache["data"]
    path = f"/api/v2/mix/market/contracts?productType={PRODUCT_TYPE}"
    r = await _http.get(BITGET_BASE + path)
    r.raise_for_status()
    _contract_cache["data"] = r.json()["data"]
    _contract_cache["when"] = now
    return _contract_cache["data"]

def normalize_tv_symbol(tv_sym: str) -> str:
    """
    "BITGET:BAKEUSDT.P" / "BAKEUSDT.P" / "BAKEUSDT" 등 → "BAKEUSDT_UMCBL" 로 내부 표준화
    (주문 보낼 때는 다시 BAKEUSDT 형식으로 변환)
    """
    if not tv_sym:
        raise HTTPException(400, "symbol missing")
    s = tv_sym.strip().upper()
    if ":" in s:
        s = s.split(":")[1]                 # EXCHANGE:SYMBOL -> SYMBOL
    s = re.sub(r"\.P(ERP)?$", "", s)       # .P / .PERP 제거
    s = re.sub(r"[-_ ]?PERP(ETUAL)?", "", s)
    s = re.sub(r"\s+PERPETUAL MIX CONTRACT", "", s)
    s = re.sub(r"[^A-Z0-9]", "", s)
    if not s.endswith("USDT"):
        raise HTTPException(400, f"unsupported symbol (need *USDT): {tv_sym}")
    return f"{s}_UMCBL"

async def to_bitget_symbol_best(tv_sym: str) -> str:
    target = normalize_tv_symbol(tv_sym)         # BAKEUSDT_UMCBL
    contracts = await fetch_contracts()
    symbols = {c["symbol"] for c in contracts}   # v2도 symbol 필드 제공
    if target in symbols:
        return target
    base = target.replace("_UMCBL", "")
    for s in symbols:
        if s.startswith(base):
            return s
    raise HTTPException(400, f"cannot resolve symbol: {tv_sym}")

# ---------- 시세/스펙/잔고/포지션 ----------
async def get_ticker(symbol_umcbl: str) -> float:
    sym = symbol_umcbl.replace("_UMCBL","")  # v2는 BAKEUSDT 형식
    path = f"/api/v2/mix/market/ticker?productType={PRODUCT_TYPE}&symbol={sym}"
    r = await _http.get(BITGET_BASE + path)
    r.raise_for_status()
    return float(r.json()["data"]["lastPr"])

async def get_contract_spec(symbol_umcbl: str):
    data = await fetch_contracts()
    for it in data:
        if it.get("symbol") == symbol_umcbl:
            size_place = int(it.get("sizePlace", 0))
            min_qty    = float(it.get("minTradeNum", 0) or 0)
            return size_place, min_qty
    return 0, 0.0

async def get_available_equity_usdt() -> float:
    path = f"/api/v2/mix/account/accounts?productType={PRODUCT_TYPE}"
    headers = sign("GET", path, "")
    r = await _http.get(BITGET_BASE + path, headers=headers)
    try:
        r.raise_for_status()
    except httpx.HTTPStatusError:
        raise HTTPException(500, f"bitget account error {r.status_code}: {r.text}")
    arr = r.json().get("data") or []
    for acc in arr:
        if (acc.get("marginCoin","")).upper() == "USDT":
            # v2 응답: available(가용), accountEquity(총자산)
            return float(acc.get("available") or acc.get("accountEquity") or 0.0)
    raise HTTPException(500, "USDT futures account not found")

async def get_margin_mode_for(symbol_no_suffix: str) -> str:
    path = ("/api/v2/mix/account/account"
            f"?symbol={symbol_no_suffix}&productType={PRODUCT_TYPE}&marginCoin={MARGIN_COIN}")
    headers = sign("GET", path, "")
    r = await _http.get(BITGET_BASE + path, headers=headers)
    r.raise_for_status()
    return (r.json().get("data") or {}).get("marginMode", "crossed")  # crossed/isolated

async def get_position_size(symbol_umcbl: str):
    sym = symbol_umcbl.replace("_UMCBL","")
    path = f"/api/v2/mix/position/all-position?productType={PRODUCT_TYPE}&marginCoin={MARGIN_COIN}"
    headers = sign("GET", path, "")
    r = await _http.get(BITGET_BASE + path, headers=headers)
    if r.status_code != 200:
        return 0.0, 0.0
    lst = r.json().get("data") or []
    long_sz = short_sz = 0.0
    for p in lst:
        if p.get("symbol") != sym:
            continue
        side = (p.get("holdSide") or "").lower()    # long/short
        total = float(p.get("total") or 0)
        if side == "long":
            long_sz = total
        elif side == "short":
            short_sz = total
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

# ---------- 주문(v2) ----------
async def place_order_v2(symbol_umcbl: str, side: str, trade_side: str, size: float):
    sym = symbol_umcbl.replace("_UMCBL","")
    margin_mode = await get_margin_mode_for(sym)  # 거래소 설정 자동 추종
    path = "/api/v2/mix/order/place-order"
    body = {
        "symbol": sym,
        "productType": PRODUCT_TYPE,
        "marginMode": margin_mode,  # crossed/isolated
        "marginCoin": MARGIN_COIN,
        "size": str(size),
        "side": side,               # "buy" or "sell"
        "tradeSide": trade_side,    # "open" or "close"
        "orderType": "market",
    }
    payload = json.dumps(body, separators=(",",":"))
    headers = sign("POST", path, payload)
    r = await _http.post(BITGET_BASE + path, data=payload, headers=headers)
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

    symbol_umcbl = await to_bitget_symbol_best(tv_sym)  # 내부 표준
    if cooling(symbol_umcbl):
        return {"ok": True, "skipped": "cooldown", "symbol": symbol_umcbl}

    lock = get_lock(symbol_umcbl)
    async with lock:
        try:
            if action == "enter_long":   # buy + open
                qty = await compute_fullseed_qty(symbol_umcbl)
                return await place_order_v2(symbol_umcbl, "buy", "open", qty)

            if action == "enter_short":  # sell + open
                qty = await compute_fullseed_qty(symbol_umcbl)
                return await place_order_v2(symbol_umcbl, "sell", "open", qty)

            if action == "exit_long":    # sell + close
                long_sz, _ = await get_position_size(symbol_umcbl)
                if long_sz <= 0:
                    return {"ok": True, "msg": "no long position", "symbol": symbol_umcbl}
                return await place_order_v2(symbol_umcbl, "sell", "close", long_sz)

            if action == "exit_short":   # buy + close
                _, short_sz = await get_position_size(symbol_umcbl)
                if short_sz <= 0:
                    return {"ok": True, "msg": "no short position", "symbol": symbol_umcbl}
                return await place_order_v2(symbol_umcbl, "buy", "close", short_sz)

        except HTTPException as e:
            # Bitget 원문을 그대로 노출(디버깅 편의)
            return {"ok": False, "error": e.detail, "action": action, "symbol": symbol_umcbl}
