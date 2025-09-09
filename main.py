import hmac, hashlib, time, base64, json
from fastapi import FastAPI, Request, HTTPException
import httpx

# 환경변수 대신 하드코딩 테스트 가능 (진짜 계정 금지!)
BITGET_API_KEY    = "bg_6b62baa07b6f09eee4c5c5dfab033555"
BITGET_API_SECRET = "1fb2ebf41c0ede17fba0bfcb109f6743e58377ec5294c5c936432f4ccdab6609"
BITGET_PASSPHRASE = "akdlsj41"

BITGET_BASE = "https://api.bitget.com"

SYMBOL_MAP = {
    "BITGET:BTCUSDT": "BTCUSDT_UMCBL"
}

app = FastAPI()

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

async def place_order(symbol: str, side: str, size: str):
    path = "/api/mix/v1/order/placeOrder"
    url  = BITGET_BASE + path
    body = {
        "symbol": symbol,
        "marginCoin": "USDT",
        "side": side,
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

@app.post("/webhook")
async def tv_webhook(req: Request):
    data = await req.json()
    action = data.get("action")
    tv_sym = data.get("symbol")
    qty    = str(data.get("qty", "1"))
    symbol = SYMBOL_MAP.get(tv_sym)
    if not symbol:
        raise HTTPException(400, f"symbol mapping missing: {tv_sym}")

    if action == "enter_long":
        return await place_order(symbol, "open_long", qty)
    elif action == "enter_short":
        return await place_order(symbol, "open_short", qty)
    elif action == "exit_long":
        return await place_order(symbol, "close_long", qty)
    elif action == "exit_short":
        return await place_order(symbol, "close_short", qty)
    else:
        raise HTTPException(400, f"unknown action: {action}")
