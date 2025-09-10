import os, json
from typing import Optional, Dict, Any, Callable
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
import ccxt

app = FastAPI()

# =======================
# ENV
# =======================
API_KEY    = os.getenv("BITGET_API_KEY", "")
API_SECRET = os.getenv("BITGET_API_SECRET", "")
API_PASS   = os.getenv("BITGET_API_PASS", "") or os.getenv("BITGET_PASSPHRASE", "")
PCT_EQUITY = float(os.getenv("PCT_EQUITY", "0.9"))           # 가용잔고 비율
SANDBOX    = os.getenv("SANDBOX_MODE", "true").lower() == "true"
TV_TOKEN   = os.getenv("TV_TOKEN", "")                        # 비워두면 인증 생략

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

# 데모 접속 모드: "header"(메인도메인+시뮬헤더) | "testnet"(테스트넷 도메인)
DEMO_MODE = "header"

def _apply_demo_mode(mode: str):
    """Bitget 데모 접속 모드 적용"""
    global DEMO_MODE
    DEMO_MODE = mode
    if not SANDBOX:
        return
    exchange.set_sandbox_mode(True)
    if mode == "header":
        # 메인 도메인 + X-SIMULATED-TRADING 헤더
        exchange.urls["api"] = "https://api.bitget.com"
        h = getattr(exchange, "headers", {}) or {}
        h["X-SIMULATED-TRADING"] = "1"
        exchange.headers = h
    else:
        # 테스트넷 전용 도메인(헤더 제거)
        exchange.urls["api"] = "https://api-testnet.bitget.com"
        h = getattr(exchange, "headers", {}) or {}
        if "X-SIMULATED-TRADING" in h:
            del h["X-SIMULATED-TRADING"]
        exchange.headers = h

def _env_bootstrap():
    """
    부팅 시 마켓 로드로 환경 검증(일부 테스트넷에서 fetch_time()이 비정상 문자열을
    돌려주는 사례가 있어 load_markets 사용). 실패하면 모드 전환.
    """
    if not SANDBOX:
        return
    for mode in ("header", "testnet"):
        try:
            _apply_demo_mode(mode)
            exchange.load_markets(reload=True)
            return
        except Exception as e:
            if mode == "testnet":
                print("bitget demo bootstrap failed:", e)

_env_bootstrap()

def _should_flip(e: Exception) -> bool:
    s = str(e)
    return ("40099" in s) or ("environment is incorrect" in s.lower())

def _with_env_retry(fn: Callable[[], Any]) -> Any:
    """호출 중 40099가 나오면 데모 모드를 전환하고 1회 재시도"""
    try:
        return fn()
    except Exception as e:
        if SANDBOX and _should_flip(e):
            _apply_demo_mode("testnet" if DEMO_MODE == "header" else "header")
            return fn()
        raise

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
    """포지션(사이즈/방향)"""
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
            return _with_env_retry(lambda: exchange.create_market_buy_order(symbol, amount, params))
        else:
            return _with_env_retry(lambda: exchange.create_market_sell_order(symbol, amount, params))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"order failed: {e}")

def fetch_balance_strong():
    """
    Bitget V5 테스트넷에서 productType 미지정 시 비정상 응답을 주는 경우가 있어
    USDT-FUTURES를 기본으로, 몇 가지 값으로 폴백 후 마지막 HTTP 원문까지 반환.
    """
    last_http = getattr(exchange, "last_http_response", None)
    try:
        return _with_env_retry(lambda: exchange.fetch_balance({"productType": "USDT-FUTURES"}))
    except Exception as e1:
        last_http = getattr(exchange, "last_http_response", last_http)
        for pt in ["SUSDT-FUTURES", "USDT-FUTURES", "UMCBL", "umcbl"]:
            try:
                return _with_env_retry(lambda: exchange.fetch_balance({"productType": pt}))
            except Exception:
                last_http = getattr(exchange, "last_http_response", last_http)
                continue
        raise HTTPException(status_code=400, detail=f"fetch_balance failed: {e1}; last_http={last_http}")

# =======================
# 모델 & 인증
# =======================
class TVPayload(BaseModel):
    action: str                   # "open" | "close"
    side: Optional[str] = None    # "long"|"short"
    symbol: Optional[str] = None  # 예: BTCUSDT.P
    price: Optional[str] = None
    time: Optional[str] = None
    tag: Optional[str] = None
    secret: Optional[str] = None  # 본문 토큰 인증 허용

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

    # Pine {{strategy.order.alert_message}} 문자열 지원
    if isinstance(body, dict) and "strategy" in body and "order" in body["strategy"]:
        msg = body["strategy"]["order"].get("alert_message")
        if isinstance(msg, str):
            body = json.loads(msg)

    payload = TVPayload(**body)
    check_auth(token, payload.secret)

    symbol = tv_to_ccxt_symbol(payload.symbol or "")
    act    = (payload.action or "").lower()
    side_h = (payload.side or "").lower()

    # (주의) /tv 요청 시엔 환경 핑 호출 제거 — 일부 테스트넷이 문자열을 반환함
    # 대신 실제 호출들은 _with_env_retry 경유로 40099 자동 폴백 수행

    # 잔고 조회(강건화)
    bal = fetch_balance_strong()
    free_usdt = pick_free_usdt(bal)
    if free_usdt <= 0:
        raise HTTPException(status_code=400, detail="No free USDT/SUSDT in wallet")

    notional = free_usdt * PCT_EQUITY
    amount   = notional_to_amount(symbol, notional)

    if act == "open":
        side = "buy" if side_h in ("", "long") else "sell"
        order = place_market(symbol, side, amount, reduce_only=False)
        return {
            "ok": True, "action": "open", "symbol": symbol, "amount": amount,
            "env": "sandbox" if SANDBOX else "live", "demo_mode": DEMO_MODE, "order": order
        }

    if act in ("close", "exit", "flat"):
        pos = get_position_info(symbol)
        if pos["size"] <= 0:
            return {"ok": True, "action": "close", "symbol": symbol,
                    "note": "no position", "env": "sandbox" if SANDBOX else "live",
                    "demo_mode": DEMO_MODE}
        close_side = "sell" if pos["side"] == "long" else "buy"
        order = place_market(symbol, close_side, pos["size"], reduce_only=True)
        return {
            "ok": True, "action": "close", "symbol": symbol, "closed_size": pos["size"],
            "env": "sandbox" if SANDBOX else "live", "demo_mode": DEMO_MODE, "order": order
        }

    raise HTTPException(status_code=400, detail=f"unknown action: {payload.action}")

@app.get("/")
def root():
    return {"service": "tv→bitget executor", "sandbox": SANDBOX}

@app.get("/debug")
def debug():
    return {
        "sandbox": SANDBOX,
        "demo_mode": DEMO_MODE,
        "has_key": bool(API_KEY),
        "has_secret": bool(API_SECRET),
        "has_pass": bool(API_PASS),
        "headers": getattr(exchange, "headers", {}),
        "api_base": exchange.urls.get("api"),
    }
