import hmac, hashlib, time, base64, json, math, os
from fastapi import FastAPI, Request, HTTPException
import httpx

# ====== 넣을 것: 데모(API) 키 ======
BITGET_API_KEY    = "bg_6b62baa07b6f09eee4c5c5dfab033555"
BITGET_API_SECRET = "1fb2ebf41c0ede17fba0bfcb109f6743e58377ec5294c5c936432f4ccdab6609"
BITGET_PASSPHRASE = "akdlsj41"

# Demo도 host는 보통 동일
BITGET_BASE = "https://api.bitget.com"

# 풀시드 계산 파라미터
MARGIN_COIN   = "USDT"   # USDT-M 선물
LEVERAGE      = 10       # 10x
USE_PCT       = 1.0      # 100% 풀시드
PRODUCT_TYPE  = "umcbl"  # USDT-Perpetual product type (Bitget 표기)

app = FastAPI()

# ---------- 공통: 서명 ----------
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

# ---------- 시세 / 스펙 / 잔고 / 포지션 ----------
async def get_ticker(symbol_umcbl: str):
    path = f"/api/mix/v1/market/ticker?symbol={symbol_umcbl}"
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(BITGET_BASE + path)
        r.raise_for_status()
        j = r.json()
        # last: 체결가
        return float(j["data"]["last"])

async def get_contract_spec(symbol_umcbl: str):
    # 전체 리스트에서 해당 심볼 찾아 precision/최소수량 얻기
    path = f"/api/mix/v1/market/contracts?productType={PRODUCT_TYPE}"
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(BITGET_BASE + path)
        r.raise_for_status()
        data = r.json()["data"]
        for it in data:
            if it.get("symbol") == symbol_umcbl:
                # sizePlace: 수량 소수 자리수, minTradeNum: 최소 수량
                size_place = int(it.get("sizePlace", 0))
                min_qty    = float(it.get("minTradeNum", 0))
                return size_place, min_qty
    # 기본값 (안 오면 보수적으로)
    return 0, 1.0

async def get_available_equity_usdt():
    # 선물 계정 USDT 가용자산 (availableEquity) 사용
    path = f"/api/mix/v1/account/accounts?productType={PRODUCT_TYPE}"
    headers = sign("GET", path, "")
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(BITGET_BASE + path, headers=headers)
        r.raise_for_status()
        arr = r.json()["data"]
        for acc in arr:
            if acc.get("marginCoin") == MARGIN_COIN:
                # availableEquity가 없으면 equity 사용
                return float(acc.get("availableEquity") or acc.get("equity"))
    raise HTTPException(500, "Cannot fetch futures account equity")

async def get_position_size(symbol_umcbl: str):
    # 보유 포지션 수량(계약수) 조회 (없으면 0)
    path = f"/api/mix/v1/position/singlePosition?symbol={symbol_umcbl}&marginCoin={MARGIN_COIN}"
    headers = sign("GET", path, "")
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(BITGET_BASE + path, headers=headers)
        if r.status_code != 200:
            return 0.0, 0.0
        j = r.json()
        if not j.get("data"):
            return 0.0, 0.0
        d = j["data"]
        long_sz = float(d.get("long", {}).get("total", 0) or 0)
        short_sz = float(d.get("short", {}).get("total", 0) or 0)
        return long_sz, short_sz

# ---------- 수량 계산(풀시드) ----------
def round_qty(qty: float, size_place: int, min_qty: float):
    factor = 10 ** size_place
    q = math.floor(qty * factor) / factor
    if q < min_qty:
        q = min_qty
    return q

async def compute_fullseed_qty(symbol_umcbl: str):
    price = await get_ticker(symbol_umcbl)
    size_place, min_qty = await get_contract_spec(symbol_umcbl)
    equity = await get_available_equity_usdt()  # USDT
    notional = equity * USE_PCT * LEVERAGE      # 레버리지로 명목가
    # 계약수 = 명목가 / 현재가 (코인 단위)
    raw_qty = notional / price
    return round_qty(raw_qty, size_place, min_qty)

# ---------- 주문 ----------
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
    body_str = json.dumps(body, separators=(",", ":"))
    headers  = sign("POST", path, body_str)
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, data=body_str, headers=headers)
        if r.status_code != 200:
            raise HTTPException(500, f"Bitget error: {r.text}")
        return r.json()

# ---------- TV 웹훅 ----------
@app.post("/webhook")
async def tv_webhook(req: Request):
    data = await req.json()
    action = data.get("action")        # enter_long/enter_short/exit_long/exit_short
    tv_sym = data.get("symbol")        # 예: "BITGET:BAKEUSDT"
    # qty는 무시(풀시드 자동 계산). 남겨두면 fallback 용으로 쓸 수 있음.
    # qty_req = data.get("qty")

    if not tv_sym or ":" not in tv_sym:
        raise HTTPException(400, f"invalid symbol: {tv_sym}")
    base = tv_sym.split(":")[1]
    symbol_umcbl = f"{base}_UMCBL"     # 자동 변환

    # 풀시드 수량 계산 or 포지션 조회 후 전량 청산
    if action == "enter_long":
        qty = await compute_fullseed_qty(symbol_umcbl)
        return await place_order(symbol_umcbl, "open_long", qty)

    elif action == "enter_short":
        qty = await compute_fullseed_qty(symbol_umcbl)
        return await place_order(symbol_umcbl, "open_short", qty)

    elif action == "exit_long":
        long_sz, _ = await get_position_size(symbol_umcbl)
        if long_sz <= 0:
            return {"ok": True, "msg": "no long position"}
        return await place_order(symbol_umcbl, "close_long", long_sz)

    elif action == "exit_short":
        _, short_sz = await get_position_size(symbol_umcbl)
        if short_sz <= 0:
            return {"ok": True, "msg": "no short position"}
        return await place_order(symbol_umcbl, "close_short", short_sz)

    else:
        raise HTTPException(400, f"unknown action: {action}")
