# app.py
import os, json
from typing import Optional, Dict, Any
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
import ccxt

app = FastAPI()

# =======================
# ENV
# =======================
API_KEY    = os.getenv("BITGET_API_KEY", "")
API_SECRET = os.getenv("BITGET_API_SECRET", "")
# 둘 중 아무 이름으로 넣어도 읽히게
API_PASS   = os.getenv("BITGET_API_PASS", "") or os.getenv("BITGET_PASSPHRASE", "")
PCT_EQUITY = float(os.getenv("PCT_EQUITY", "0.25"))  # 가용 잔고의 몇 %로 주문할지
SANDBOX    = os.getenv("SANDBOX_MODE", "true").lower() == "true"  # 데모/실계정 스위치
TV_TOKEN   = os.getenv("TV_TOKEN", "")  # 비워두면 인증 생략

# =======================
# CCXT 초기화
# =======================
cfg = {
    "options": {"defaultType": "swap"},  # USDT-M Perpetual
    "timeout": 15000,
    "enableRateLimit": True,
}
if API_KEY and API_SECRET and API_PASS:
    cfg.update({"apiKey": API_KEY, "secret": API_SECRET, "password": API_PASS})

exchange = ccxt.bitget(cfg)

def _force_bitget_demo_env():
    """
    Bitget 데모 접속을 강제하고, 40099("exchange environment is incorrect")가 나올 경우
    테스트넷 전용 도메인으로 자동 폴백.
    """
    if not SANDBOX:
        return
    # 1) 메인 도메인 + 시뮬레이티드 헤더 방식
    exchange.set_sandbox_mode(True)
    exchange.headers = {**getattr(exchange, "headers", {}), "X-SIMULATED-TRADING": "1"}
    try:
        exchange.fetch_time()  # 가벼운 핑
        return
    except Exception as e:
        err = str(e)
        # 40099 or 문구 매칭 → 테스트넷 도메인으로 폴백
        if ("40099" in err) or ("environment is incorrect" in err.lower()):
            try:
                if hasattr(exchange, "headers") and "X-SIMULATED-TRADING" in exchange.headers:
                    del exchange.headers["X-SIMULATED-TRADING"]
            except Exception:
                pass
            exchange.urls["api"] = "https://api-testnet.bitget.com"
            # 폴백 환경 확인
            exchange.fetch_time()
        else:
            raise

# 앱 부팅 시 1회 시도(실패해도 /tv에서 다시 보정)
try:
    _force_bitget_demo_env()
except Exception as e:
    print("bitget demo env bootstrap failed:", e)

# =======================
# 유틸
# =======================
def tv_to_ccxt_symbol(tv_symbol: str) -> str:
    """TV 심볼(BTCUSDT.P 등) -> ccxt 심볼(BTC/USDT:USDT)"""
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
    """실계정: USDT, 데모: SUSDT 둘 다 대응"""
    try:
        if "USDT" in balance.get("free", {}):
            return float(balance["free"]["USDT"])
        if "SUSDT" in balance.get("free", {}):
            return float(balance["free"]["SUSDT"])
        for k in ("USDT", "SUSDT"):
            if k in balance:
                v = balance[k]
                return float(v.get("free") or v.get("total") or 0)
    except Exception:
        pass
    return 0.0

def notional_to_amount(symbol: str, notional_usdt: float) -> float:
    """명목가(USDT) -> 수량(베이스)"""
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
    # 최소 수량 보정
    try:
        min_amt = mk.get("limits", {}).get("amount", {}).get("min")
        if min_amt and amt < float(min_amt):
            amt = float(min_amt)
    except Exception:
        pass
    return amt

def get_position_info(symbol: str) -> Dict[str, Any]:
    """해당 심볼 포지션(사이즈/방향)"""
    try:
        poss = exchange.fetch_positions([symbol])
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"fetch_positions failed: {e}")
    for p in poss:
        if p.get("symbol") == symbol:
            size = 0.0
            side = None
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

# =======================
# 요청 모델
# =======================
class TVPayload(BaseModel):
    action: str                   # "open" | "close"
    side: Optional[str] = None    # "long"|"short"
    symbol: Optional[str] = None  # 예: BTCUSDT.P
    price: Optional[str] = None
    time: Optional[str] = None
    tag: Optional[str] = None
    secret: Optional[str] = None  # 본문으로도 인증 허용

def check_auth(token_from_query: str, secret_in_body: Optional[str]):
    if TV_TOKEN and (token_from_query != TV_TOKEN and secret_in_body != TV_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")

# =======================
# 라우트
# =======================
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

    # Pine의 {{strategy.order.alert_message}}를 그대로 보낸 경우 처리
    if isinstance(body, dict) and "strategy" in body and "order" in body["strategy"]:
        msg = body["strategy"]["order"].get("alert_message")
        if isinstance(msg, str):
            body = json.loads(msg)

    payload = TVPayload(**body)
    check_auth(token, payload.secret)

    # SANDBOX면 매 요청마다 환경 보정(초기 부팅 실패 대비)
    if SANDBOX:
        try:
            _force_bitget_demo_env()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"bitget sandbox bootstrap failed: {e}")

    symbol = tv_to_ccxt_symbol(payload.symbol or "")
    act    = (payload.action or "").lower()
    side_h = (payload.side or "").lower()

    # 잔고 조회(USDT-M)
    try:
        bal = exchange.fetch_balance({"productType": "USDT-FUTURES"})
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"fetch_balance failed: {e}")

    free_usdt = pick_free_usdt(bal)
    if free_usdt <= 0:
        raise HTTPException(status_code=400, detail="No free USDT/SUSDT in wallet")

    notional = free_usdt * PCT_EQUITY
    amount   = notional_to_amount(symbol, notional)

    if act == "open":
        side = "buy" if side_h in ("", "long") else "sell"
        order = place_market(symbol, side, amount, reduce_only=False)
        return {
            "ok": True, "action": "open", "symbol": symbol,
            "amount": amount, "env": "sandbox" if SANDBOX else "live", "order": order
        }

    if act in ("close", "exit", "flat"):
        pos = get_position_info(symbol)
        if pos["size"] <= 0:
            return {"ok": True, "action": "close", "symbol": symbol, "note": "no position"}
        close_side = "sell" if pos["side"] == "long" else "buy"
        order = place_market(symbol, close_side, pos["size"], reduce_only=True)
        return {
            "ok": True, "action": "close", "symbol": symbol,
            "closed_size": pos["size"], "env": "sandbox" if SANDBOX else "live", "order": order
        }

    raise HTTPException(status_code=400, detail=f"unknown action: {payload.action}")

@app.get("/")
def root():
    return {"service": "tv→bitget executor", "sandbox": SANDBOX}

# 디버그(선택)
@app.get("/debug")
def debug():
    return {
        "sandbox": SANDBOX,
        "has_key": bool(API_KEY),
        "has_secret": bool(API_SECRET),
        "has_pass": bool(API_PASS),
        "headers": getattr(exchange, "headers", {}),
        "api_base": exchange.urls.get("api"),
    }
