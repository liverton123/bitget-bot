import os, json
from typing import Optional, Dict, Any, Callable
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
import ccxt

app = FastAPI()

# ========= ENV =========
API_KEY    = os.getenv("BITGET_API_KEY", "")
API_SECRET = os.getenv("BITGET_API_SECRET", "")
API_PASS   = os.getenv("BITGET_API_PASS", "") or os.getenv("BITGET_PASSPHRASE", "")
PCT_EQUITY = float(os.getenv("PCT_EQUITY", "0.25"))        # 가용잔고 비율
SANDBOX    = os.getenv("SANDBOX_MODE", "true").lower() == "true"
TV_TOKEN   = os.getenv("TV_TOKEN", "")                     # 비우면 인증 생략
FALLBACK_USDT = float(os.getenv("FALLBACK_USDT", "10"))    # 잔고조회 실패시 임시 명목가(0=사용안함)

# ======= CCXT SETUP =======
cfg = {
    "options": {"defaultType": "swap"},  # USDT-M Perpetual
    "timeout": 20000,
    "enableRateLimit": True,
}
if API_KEY and API_SECRET and API_PASS:
    cfg.update({"apiKey": API_KEY, "secret": API_SECRET, "password": API_PASS})
exchange = ccxt.bitget(cfg)

# 테스트넷 고정(헤더/메인도메인 혼선 제거)
if SANDBOX:
    exchange.set_sandbox_mode(True)
    exchange.urls["api"] = "https://api-testnet.bitget.com"
    # 혹시 남아있을 수 있는 헤더 제거
    h = getattr(exchange, "headers", {}) or {}
    if "X-SIMULATED-TRADING" in h:
        del h["X-SIMULATED-TRADING"]
    exchange.headers = h

# 40099가 나오면 한 번은 메인+헤더로 스위칭해서 재시도
def _should_flip(e: Exception) -> bool:
    s = str(e)
    return ("40099" in s) or ("environment is incorrect" in s.lower())

def _flip_env():
    if not SANDBOX:
        return
    if exchange.urls.get("api", "").startswith("https://api-testnet"):
        # 테스트넷 -> 메인 + 시뮬헤더
        exchange.urls["api"] = "https://api.bitget.com"
        h = getattr(exchange, "headers", {}) or {}
        h["X-SIMULATED-TRADING"] = "1"
        exchange.headers = h
    else:
        # 메인 + 헤더 -> 테스트넷
        exchange.urls["api"] = "https://api-testnet.bitget.com"
        h = getattr(exchange, "headers", {}) or {}
        if "X-SIMULATED-TRADING" in h:
            del h["X-SIMULATED-TRADING"]
        exchange.headers = h

def _with_env_retry(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as e:
        if SANDBOX and _should_flip(e):
            _flip_env()
            return fn()
        raise

# ======= UTIL =======
def tv_to_ccxt_symbol(tv_symbol: str) -> str:
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
    try:
        mk   = _with_env_retry(lambda: exchange.market(symbol))
        last = float(_with_env_retry(lambda: exchange.fetch_ticker(symbol)["last"]))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"fetch_ticker/market failed: {e}")
    if last <= 0:
        raise HTTPException(status_code=400, detail="invalid last price")
    amt = notional_usdt / last
    try:
        amt = float(exchange.amount_to_precision(symbol, amt))
    except Exception:
        amt = float(f"{amt:.6f}")
    try:
        min_amt = mk.get("limits", {}).get("amount", {}).get("min")
        if min_amt and amt < float(min_amt):
            amt = float(min_amt)
    except Exception:
        pass
    return amt

def get_position_info(symbol: str) -> Dict[str, Any]:
    try:
        poss = _with_env_retry(lambda: exchange.fetch_positions([symbol]))
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
            if size > 0:  side = "long"
            elif size < 0: side = "short"
            return {"size": abs(size), "side": side}
    return {"size": 0.0, "side": None}

def place_market(symbol: str, side: str, amount: float, reduce_only: bool = False):
    params = {"reduceOnly": reduce_only}
    try:
        if side.lower() in ("buy", "long"):
            return _with_env_retry(lambda: exchange.create_market_buy_order(symbol, amount, params))
        else:
            return _with_env_retry(lambda: exchange.create_market_sell_order(symbol, amount, params))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"order failed: {e}")

def fetch_balance_strong() -> Dict[str, Any]:
    """
    Bitget 테스트넷에서 잔고 호출이 종종 비정상 응답을 줄 수 있어
    파라미터 조합을 바꿔가며 순차 폴백. 실패 시 원문 응답을 detail에 실어줌.
    """
    combos = [
        {"type": "swap", "productType": "USDT-FUTURES", "marginCoin": "USDT"},
        {"type": "swap", "productType": "USDT-FUTURES"},
        {"type": "swap", "marginCoin": "USDT"},
        {"type": "swap"},
        {},
    ]
    last_http = getattr(exchange, "last_http_response", None)
    last_resp = getattr(exchange, "last_response", None)
    err0 = None
    for p in combos:
        try:
            return _with_env_retry(lambda: exchange.fetch_balance(p))
        except Exception as e:
            err0 = e
            last_http = getattr(exchange, "last_http_response", last_http)
            last_resp = getattr(exchange, "last_response", last_resp)
            continue
    raise HTTPException(
        status_code=400,
        detail=f"fetch_balance failed: {err0}; last_http={last_http}; last_resp={last_resp}"
    )

# ======= MODELS & AUTH =======
class TVPayload(BaseModel):
    action: str                   # "open" | "close"
    side: Optional[str] = None    # "long"|"short"
    symbol: Optional[str] = None  # 예: BTCUSDT.P
    price: Optional[str] = None
    time: Optional[str] = None
    tag: Optional[str] = None
    secret: Optional[str] = None

def check_auth(token_from_query: str, secret_in_body: Optional[str]):
    if TV_TOKEN and (token_from_query != TV_TOKEN and secret_in_body != TV_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")

# ======= ROUTES =======
@app.post("/tv")
async def tv_webhook(request: Request):
    token = request.query_params.get("token", "")
    try:
        body = await request.json()
    except Exception:
        raw = (await request.body()).decode("utf-8")
        try: body = json.loads(raw)
        except Exception: raise HTTPException(status_code=400, detail="Invalid JSON")

    # Pine {{strategy.order.alert_message}} 지원
    if isinstance(body, dict) and "strategy" in body and "order" in body["strategy"]:
        msg = body["strategy"]["order"].get("alert_message")
        if isinstance(msg, str):
            body = json.loads(msg)

    payload = TVPayload(**body)
    check_auth(token, payload.secret)

    symbol = tv_to_ccxt_symbol(payload.symbol or "")
    act    = (payload.action or "").lower()
    side_h = (payload.side or "").lower()

    # 잔고 조회(강건)
    free_usdt = 0.0
    bal_err_detail = None
    try:
        bal = fetch_balance_strong()
        free_usdt = pick_free_usdt(bal)
    except HTTPException as e:
        bal_err_detail = e.detail

    # 잔고가 0이고, FALLBACK_USDT가 설정되어 있으면 임시 금액으로 진행(주문 테스트용)
    if free_usdt <= 0:
        if FALLBACK_USDT > 0:
            free_usdt = FALLBACK_USDT
        else:
            if bal_err_detail:
                raise HTTPException(status_code=400, detail=bal_err_detail)
            raise HTTPException(status_code=400, detail="No free USDT/SUSDT in wallet")

    notional = free_usdt * PCT_EQUITY
    amount   = notional_to_amount(symbol, notional)

    if act == "open":
        side = "buy" if side_h in ("", "long") else "sell"
        order = place_market(symbol, side, amount, reduce_only=False)
        return {"ok": True, "action": "open", "symbol": symbol, "amount": amount,
                "env": "sandbox" if SANDBOX else "live", "api_base": exchange.urls.get("api"), "order": order}

    if act in ("close", "exit", "flat"):
        pos = get_position_info(symbol)
        if pos["size"] <= 0:
            return {"ok": True, "action": "close", "symbol": symbol, "note": "no position",
                    "env": "sandbox" if SANDBOX else "live", "api_base": exchange.urls.get("api")}
        close_side = "sell" if pos["side"] == "long" else "buy"
        order = place_market(symbol, close_side, pos["size"], reduce_only=True)
        return {"ok": True, "action": "close", "symbol": symbol, "closed_size": pos["size"],
                "env": "sandbox" if SANDBOX else "live", "api_base": exchange.urls.get("api"), "order": order}

    raise HTTPException(status_code=400, detail=f"unknown action: {payload.action}")

@app.get("/")
def root():
    return {"service": "tv→bitget executor", "sandbox": SANDBOX}

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
