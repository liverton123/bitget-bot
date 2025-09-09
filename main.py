# main.py
import hmac, hashlib, time, base64, json
from fastapi import FastAPI, Request, HTTPException
import httpx

# === Bitget Demo API Keys (데모 전용) ===
BITGET_API_KEY    = "bg_6b62baa07b6f09eee4c5c5dfab033555"
BITGET_API_SECRET = "1fb2ebf41c0ede17fba0bfcb109f6743e58377ec5294c5c936432f4ccdab6609"
BITGET_PASSPHRASE = "akdlsj41"

# API 엔드포인트 (데모도 동일한 경우가 많음)
BITGET_BASE = "https://api.bitget.com"

app = FastAPI()


# ---- Helper: Sign 요청 ----
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


# ---- 주문 실행 ----
async def place_order(symbol: str, side: str, size: str):
    """
    side: "open_long" / "open_short" / "close_long" / "close_short"
    """
    path = "/api/mix/v1/order/placeOrder"
    url  = BITGET_BASE + path
    body = {
        "symbol": symbol,       # 예: "BAKEUSDT_UMCBL"
        "marginCoin": "USDT",
        "side": side,
        "orderType": "market",  # 시장가 주문
        "size": str(size)
    }
    body_str = json.dumps(body, separators=(",", ":"))
    headers  = sign("POST", path, body_str)

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, data=body_str, headers=headers)
        if r.status_code != 200:
            raise HTTPException(500, f"Bitget error: {r.text}")
        return r.json()


# ---- Webhook 엔드포인트 ----
@app.post("/webhook")
async def tv_webhook(req: Request):
    data = await req.json()
    action = data.get("action")           # "enter_long", "enter_short", "exit_long", "exit_short"
    tv_sym = data.get("symbol")           # 예: "BITGET:BAKEUSDT"
    qty    = str(data.get("qty", "1"))    # 없으면 기본 1

    # 1) 심볼 변환: "BITGET:BAKEUSDT" → "BAKEUSDT_UMCBL"
    symbol = None
    if tv_sym and ":" in tv_sym:
        base_symbol = tv_sym.split(":")[1]
        symbol = base_symbol + "_UMCBL"

    if not symbol:
        raise HTTPException(400, f"Invalid symbol: {tv_sym}")

    # 2) 주문 실행
    if action == "enter_long":
        return await place_order(symbol, "open_long", qty)
    elif action == "enter_short":
        return await place_order(symbol, "open_short", qty)
    elif action == "exit_long":
        return await place_order(symbol, "close_long", qty)
    elif action == "exit_short":
        return await place_order(symbol, "close_short", qty)
    else:
        raise HTTPException(400, f"Unknown action: {action}")
