# app.py
import os, json
from typing import Optional, Dict, Any
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
import ccxt

app = FastAPI()

# ===== ENV =====
API_KEY    = os.getenv("BITGET_API_KEY", "bg_6b62baa07b6f09eee4c5c5dfab033555")
API_SECRET = os.getenv("BITGET_API_SECRET", "1fb2ebf41c0ede17fba0bfcb109f6743e58377ec5294c5c936432f4ccdab6609")
API_PASS   = os.getenv("BITGET_API_PASS", "akdlsj41")
PCT_EQUITY = float(os.getenv("PCT_EQUITY", "0.9"))          # 0.9 = 90% (풀시드)
TV_TOKEN   = os.getenv("TV_TOKEN", "")                      # ?token= 로 보호
DRY_RUN    = os.getenv("DRY_RUN", "true").lower() == "true" # 우선 테스트 모드
SANDBOX    = os.getenv("SANDBOX_MODE", "true").lower() == "true"  # Bitget 데모

# ===== CCXT EXCHANGE =====
cfg = {
    "options": {"defaultType": "swap"},  # USDT-M perpetual
    "timeout": 15000,
    "enableRateLimit": True,
}
if API_KEY and API_SECRET and API_PASS:
    cfg.update({"apiKey": API_KEY, "secret": API_SECRET, "password": API_PASS})
exchange = ccxt.bitget(cfg)

# 데모트레이딩(모의) 환경 사용
if SANDBOX:
    # ccxt가 Bitget 데모 엔드포인트/헤더로 전환
    exchange.set_sandbox_mode(True)

# ===== UTIL =====
def tv_to_ccxt_symbol(tv_symbol: str) -> str:
    """TV 심볼(BTCUSDT.P 등) -> ccxt 심볼(BTC/USDT:USDT)"""
    s = tv_symbol.upper().replace(".P", "").replace("_PERP", "").replace("PERP", "")
    if s.endswith("USDT"):
        base = s[:-4]
        return f"{base}/USDT:USDT"
    if "/" in s:
        return s
    raise HTTPException(status_code=400, detail=f"Unsupported symbol: {tv_symbol}")

def pick_free_usdt(balance: Dict[str, Any]) -> float:
    # 실계정: USDT, 데모: SUSDT (Bitget 데모 코인)
    free = 0.0
    if "USDT" in balance.get("free", {}):
        free = float(balance["free"]["USDT"])
    elif "SUSDT" in balance.get("free", {}):
        free = float(balance["free"]["SUSDT"])
    else:
        # fallback
        for k in ("USDT", "SUSDT"):
            if k in balance:
                val = balance[k]
                free = float(val.get("free") or val.get("total", 0))  # 일부 ccxt 버전 대응
                break
    return free

def notional_to_amount(symbol: str, notional_usdt: float) -> float:
    mk = exchange.market(symbol)
    last = float(exchange.fetch_ticker(symbol)["last"])
    amt = notional_usdt / max(last, 1e-12)
    # 거래소 규격에 맞추기
    return float(exchange.amount_to_precision(symbol, amt))

def place_market(symbol: str, side: str, amount: float, reduce_only: bool = False):
    if DRY_RUN:
        return {"dry_run": True, "symbol": symbol, "side": side, "amount": amount, "reduceOnly": reduce_only}
    params = {"reduceOnly": reduce_only}
    if side.lower() in ("buy", "long"):
        return exchange.create_market_buy_order(symbol, amount, params)
    return exchange.create_market_sell_order(symbol, amount, params)

def get_position_size(symbol: str) -> float:
    try:
        positions = exchange.fetch_positions([symbol])
        for p in positions:
            if p.get("symbol") == symbol:
                # ccxt 버전에 따라 필드명이 다를 수 있어 보수적으로 처리
                for key in ("contracts", "contractsSize", "size", "positionAmt", "position"):
                    v = p.get(key)
                    if v is not None:
                        try:
                            return abs(float(v))
                        except Exception:
                            continue
    except Exception:
        pass
    return 0.0

# ===== PAYLOAD =====
class TVPayload(BaseModel):
    action: str
    side: Optional[str] = None
    symbol: Optional[str] = None
    price: Optional[str] = None
    time: Optional[str] = None
    tag: Optional[str] = None
    secret: Optional[str] = None  # (선택) body에 넣어와도 인증 허용

def check_auth(token_from_query: str, secret_in_body: Optional[str]):
    if TV_TOKEN and (token_from_query != TV_TOKEN and secret_in_body != TV_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")

# ===== ROUTES =====
@app.post("/tv")
async def tv_webhook(request: Request):
    token = request.query_params.get("token", "")
    try:
        body = await request.json()
    except Exception:
        body = json.loads((await request.body()).decode("utf-8"))

    # Pine이 strategy.order.alert_message 문자열만 보낼 때 파싱
    if isinstance(body, dict) and "strategy" in body and "order" in body["strategy"]:
        msg = body["strategy"]["order"].get("alert_message")
        if isinstance(msg, str):
            body = json.loads(msg)

    payload = TVPayload(**body)
    check_auth(token, payload.secret)

    if not payload.symbol:
        raise HTTPException(status_code=400, detail="symbol required")
    symbol = tv_to_ccxt_symbol(payload.symbol)

    # 잔고 조회
    if DRY_RUN:
        free_usdt = 1000.0
    else:
        bal = exchange.fetch_balance()
        free_usdt = pick_free_usdt(bal)
        if free_usdt <= 0:
            raise HTTPException(status_code=400, detail="No free USDT/SUSDT")

    # 포지션 크기 계산 (배율/마진은 거래소 설정 그대로 사용)
    notional = free_usdt * PCT_EQUITY
    amount = notional_to_amount(symbol, notional)

    act = payload.action.lower()
    if act == "open":
        side = "buy" if (payload.side or "").lower() == "long" else "sell"
        order = place_market(symbol, side, amount, reduce_only=False)
        return {"ok": True, "placed": order, "env": "sandbox" if SANDBOX else "live", "dry_run": DRY_RUN}
    elif act in ("close", "exit", "flat"):
        pos_amt = get_position_size(symbol)
        if pos_amt <= 0:
            return {"ok": True, "note": "no position", "env": "sandbox" if SANDBOX else "live"}
        # 반대 방향 reduce-only 시장가로 정리
        side = "sell"  # long 보유 기준(샌드박스 단순화)
        order = place_market(symbol, side, pos_amt, reduce_only=True)
        return {"ok": True, "placed": order, "env": "sandbox" if SANDBOX else "live", "dry_run": DRY_RUN}
    else:
        return {"ok": False, "error": f"unknown action {payload.action}"}

@app.get("/")
def root():
    return {"service": "tv→bitget executor", "sandbox": SANDBOX, "dry_run": DRY_RUN}
