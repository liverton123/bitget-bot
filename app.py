# app.py
import os, json
from typing import Optional, Dict, Any
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
import ccxt

app = FastAPI()

# ===== 환경변수 =====
API_KEY    = os.getenv("BITGET_API_KEY", "")
API_SECRET = os.getenv("BITGET_API_SECRET", "")
API_PASS   = os.getenv("BITGET_API_PASS", "")
PCT_EQUITY = float(os.getenv("PCT_EQUITY", "0.9"))         # 1.0 = 가용잔고 100% 사용
SANDBOX    = os.getenv("SANDBOX_MODE", "true").lower() == "true"  # 데모(모의) 사용
TV_TOKEN   = os.getenv("TV_TOKEN", "")                     # 비워두면 인증 생략

# ===== ccxt 초기화 =====
cfg = {
    "options": {"defaultType": "swap"},  # USDT-M Perpetual
    "timeout": 15000,
    "enableRateLimit": True,
}
if API_KEY and API_SECRET and API_PASS:
    cfg.update({"apiKey": API_KEY, "secret": API_SECRET, "password": API_PASS})
exchange = ccxt.bitget(cfg)

if SANDBOX:
    exchange.set_sandbox_mode(True)
    # ▶ Bitget 테스트넷 엔드포인트를 강제로 사용
    exchange.urls["api"] = "https://api-testnet.bitget.com"
if SANDBOX:
    # 데모 환경
    exchange.set_sandbox_mode(True)

# ===== 유틸 =====
def tv_to_ccxt_symbol(tv_symbol: str) -> str:
    """TV 심볼(BTCUSDT.P 등)을 ccxt 심볼(BTC/USDT:USDT)로 변환"""
    if not tv_symbol:
        raise HTTPException(status_code=400, detail="symbol required")
    s = tv_symbol.upper().replace(".P", "").replace("_PERP", "").replace("PERP", "")
    if s.endswith("USDT"):
        base = s[:-4]
        return f"{base}/USDT:USDT"
    if "/" in s:
        return s
    raise HTTPException(status_code=400, detail=f"Unsupported symbol: {tv_symbol}")

def pick_free_usdt(balance: Dict[str, Any]) -> float:
    """실계정=USDT, 데모=SUSDT 잔고 대응"""
    free = 0.0
    try:
        if "USDT" in balance.get("free", {}):
            free = float(balance["free"]["USDT"])
        elif "SUSDT" in balance.get("free", {}):
            free = float(balance["free"]["SUSDT"])
        else:
            # ccxt 버전별 대응
            for k in ("USDT", "SUSDT"):
                if k in balance:
                    v = balance[k]
                    free = float(v.get("free") or v.get("total", 0) or 0)
                    break
    except Exception:
        free = 0.0
    return free

def notional_to_amount(symbol: str, notional_usdt: float) -> float:
    """명목가(USDT) -> 거래수량(베이스)"""
    try:
        mk = exchange.market(symbol)
        last = float(exchange.fetch_ticker(symbol)["last"])
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"fetch_ticker/market failed: {e}")
    if last <= 0:
        raise HTTPException(status_code=400, detail="invalid last price")
    amt = notional_usdt / last
    try:
        amt = float(exchange.amount_to_precision(symbol, amt))
    except Exception:
        amt = float(f"{amt:.6f}")
    # 최소 수량 체크(있으면 보정)
    try:
        min_amt = mk.get("limits", {}).get("amount", {}).get("min")
        if min_amt and amt < float(min_amt):
            amt = float(min_amt)
    except Exception:
        pass
    return amt

def get_position_info(symbol: str) -> Dict[str, Any]:
    """심볼 포지션 정보(사이즈/방향)"""
    try:
        poss = exchange.fetch_positions([symbol])
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"fetch_positions failed: {e}")
    for p in poss:
        if p.get("symbol") == symbol:
            size = 0.0
            side = None
            # ccxt 버전별 다양한 필드 대응
            for k in ("contracts", "contractsSize", "size", "positionAmt"):
                v = p.get(k)
                if v is not None:
                    try:
                        size = float(v)
                        break
                    except Exception:
                        continue
            if size > 0:
                side = "long"
            elif size < 0:
                side = "short"
            return {"size": abs(size), "side": side}
    return {"size": 0.0, "side": None}

def place_market(symbol: str, side: str, amount: float, reduce_only: bool = False):
    params = {"reduceOnly": reduce_only}
    try:
        if side.lower() in ("buy", "long"):
            return exchange.create_market_buy_order(symbol, amount, params)
        else:
            return exchange.create_market_sell_order(symbol, amount, params)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"order failed: {e}")

# ===== 페이로드 =====
class TVPayload(BaseModel):
    action: str
    side: Optional[str] = None
    symbol: Optional[str] = None
    price: Optional[str] = None
    time: Optional[str] = None
    tag: Optional[str] = None
    secret: Optional[str] = None  # 본문에 넣어도 인증 허용

def check_auth(token_from_query: str, secret_in_body: Optional[str]):
    if TV_TOKEN and (token_from_query != TV_TOKEN and secret_in_body != TV_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")

# ===== 라우트 =====
@app.post("/tv")
async def tv_webhook(request: Request):
    token = request.query_params.get("token", "")
    # JSON 파싱
    try:
        body = await request.json()
    except Exception:
        raw = (await request.body()).decode("utf-8")
        try:
            body = json.loads(raw)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")

    # Pine이 {{strategy.order.alert_message}} 문자열을 보내는 경우 처리
    if isinstance(body, dict) and "strategy" in body and "order" in body["strategy"]:
        msg = body["strategy"]["order"].get("alert_message")
        if isinstance(msg, str):
            body = json.loads(msg)

    payload = TVPayload(**body)
    check_auth(token, payload.secret)

    symbol = tv_to_ccxt_symbol(payload.symbol or "")
    act = (payload.action or "").lower()
    side_hint = (payload.side or "").lower()

    # 잔고 → 수량 계산
    try:
        bal = exchange.fetch_balance()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"fetch_balance failed: {e}")
    free_usdt = pick_free_usdt(bal)
    if free_usdt <= 0:
        raise HTTPException(status_code=400, detail="No free USDT/SUSDT in wallet")

    notional = free_usdt * PCT_EQUITY
    amount = notional_to_amount(symbol, notional)

    if act == "open":
        side = "buy" if side_hint == "long" or side_hint == "" else "sell"
        order = place_market(symbol, side, amount, reduce_only=False)
        return {"ok": True, "action": "open", "symbol": symbol, "amount": amount, "order": order, "env": "sandbox" if SANDBOX else "live"}

    elif act in ("close", "exit", "flat"):
        pos = get_position_info(symbol)
        if pos["size"] <= 0:
            return {"ok": True, "action": "close", "symbol": symbol, "note": "no position"}
        # 보유 방향의 반대로 reduceOnly 주문
        close_side = "sell" if pos["side"] == "long" else "buy"
        order = place_market(symbol, close_side, pos["size"], reduce_only=True)
        return {"ok": True, "action": "close", "symbol": symbol, "closed_size": pos["size"], "order": order, "env": "sandbox" if SANDBOX else "live"}

    else:
        raise HTTPException(status_code=400, detail=f"unknown action: {payload.action}")

@app.get("/")
def root():
    return {"service": "tv→bitget executor", "sandbox": SANDBOX}
