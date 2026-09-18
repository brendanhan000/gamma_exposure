#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py -- local HTTP API + iPhone web app over gex.py.

Thin FastAPI wrapper around the audited functions in gex.py: same math, same
filters, same assumptions -- just JSON out. Serves the mobile UI from ./static.

Run (manually):        /opt/anaconda3/bin/python server.py
Run (always-on):       install scripts/com.brendanhan.gex-server.plist (launchd)
Phone (same Wi-Fi):    http://<mac-lan-ip>:8787   -> Share -> Add to Home Screen

Endpoints:
    GET /api/health                 token age / days left / status
    GET /api/expirations?ticker=    real listed expirations (for the picker)
    GET /api/gex?ticker=&expiry=    levels + profile + buckets as JSON
        expiry: 'both' (default) | '0dte' | 'all' | YYYY-MM-DD;  &all_days=45

Notes:
  * Results are cached for CACHE_TTL_S per (ticker, expiry, window) so pull-to-
    refresh doesn't hammer Schwab. `cached: true` marks a cache hit.
  * The hub owns the token: after `../schwab_hub/run.sh login` (or /setup.html) the
    server works on the next request -- no restart.
  * Binds 0.0.0.0: reachable on your LAN only (behind the router). No account
    actions are possible through this API; it reads market data and computes.
"""
from __future__ import annotations

import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import gex

HOST = os.environ.get("GEX_SERVER_HOST", "0.0.0.0")
PORT = int(os.environ.get("GEX_SERVER_PORT", "8787"))
CACHE_TTL_S = float(os.environ.get("GEX_CACHE_TTL", "60"))
EXPIRATIONS_TTL_S = 600.0
PROFILE_WINDOW = 0.10          # per-strike profile returned for spot +/- 10%


def _load_dotenv(path=".env"):
    """Minimal .env loader (launchd doesn't source shells). Existing env wins."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


os.chdir(os.path.dirname(os.path.abspath(__file__)))  # .env / token / static paths
_load_dotenv()

app = FastAPI(title="GEX API", description="Dealer gamma exposure levels (gex.py)",
              version="1.0")

_lock = threading.Lock()
_client = None
_gex_cache = {}          # (ticker, expiry, all_days) -> (ts, payload)
_exp_cache = {}          # ticker -> (ts, [dates])
_pool = ThreadPoolExecutor(max_workers=4)
FETCH_DEADLINE_S = float(os.environ.get("GEX_FETCH_DEADLINE", "90"))


def _fetch_bounded(client, symbol, **kw):
    """Bound the TOTAL wall-time of a chain fetch.

    httpx's read-timeout only fires after 30s of socket SILENCE; a glacial but
    alive stream can trickle a big chain for many minutes (observed: 868s).
    This enforces a hard deadline; on breach the worker thread is abandoned and
    the caller gets a clean timeout the client-heal path already handles.
    """
    fut = _pool.submit(gex.fetch_chain_schwab, client, symbol, **kw)
    try:
        return fut.result(timeout=FETCH_DEADLINE_S)
    except FutTimeout:
        fut.cancel()
        raise TimeoutError(
            "Schwab chain fetch exceeded the {:.0f}s deadline".format(FETCH_DEADLINE_S))


def _get_client():
    """The hub client (stateless: the hub owns the token, so nothing here can go stale)."""
    global _client
    with _lock:
        if _client is None:
            _client = gex.get_schwab_client()
        return _client


def _drop_client():
    global _client
    with _lock:
        _client = None


# Last observed auth state. File age alone is a LIE when the token family has
# been revoked (age says "6 days left" while every call 401s), so health reports
# the last real API outcome instead of just the timestamp.
_auth_error = None


def _note_auth(ok, code=None):
    global _auth_error
    _auth_error = None if ok else code


def _classify(exc):
    """Map an exception to (http_status, code, user_message)."""
    s = str(exc).lower()
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 401 or "oauth" in type(exc).__name__.lower() or "token" in s:
        return 503, "schwab_token_expired", \
            "Schwab token expired. Run: ../schwab_hub/run.sh login"
    if "timed out" in s or "connection" in s or "reset" in s:
        return 502, "schwab_unreachable", "Schwab API unreachable (transient); retry shortly."
    return 500, "internal_error", str(exc)[:200]


def _token_health():
    """Token state as reported by the hub (the only holder of the token)."""
    out = {"token_present": False, "token_age_days": None, "token_days_left": None}
    try:
        import requests
        h = requests.get(_get_client().url + "/health", timeout=3).json()
        out.update(token_present=bool(h.get("ok")), token_age_days=h.get("token_age_days"),
                   token_days_left=h.get("relogin_in_days"))
    except Exception:
        pass
    return out


@app.get("/api/health")
def health():
    h = _token_health()
    h["auth_error"] = _auth_error
    h["ok"] = bool(h["token_present"] and (h["token_days_left"] or 0) > 0
                   and _auth_error is None)
    h["message"] = ("Schwab token rejected -- run ../schwab_hub/run.sh login"
                    if _auth_error == "schwab_token_expired" else None)
    h["now_et"] = gex.now_et().isoformat()
    return h


# ---------------------------------------------------------------------------
# Phone-based re-authentication (/setup) -- proxied to the central schwab_hub
# ---------------------------------------------------------------------------
# Schwab refresh tokens are hard-capped at 7 days, so re-login is weekly. The hub
# owns the token and runs the OAuth exchange (the app secret never leaves it);
# this server only relays, so the phone flow at /setup.html keeps working.
def _hub(method, path, **kw):
    import requests
    try:
        r = requests.request(method, _get_client().url + path, timeout=30, **kw)
    except requests.RequestException:
        raise HTTPException(status_code=502, detail={
            "code": "hub_unreachable", "message": "schwab_hub is not running: start ../schwab_hub/run.sh"})
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail={
            "code": body.get("error", "hub_error"), "message": body.get("detail", r.text[:200])})
    return body


@app.get("/api/auth/start")
def auth_start():
    return _hub("GET", "/auth/start")


@app.post("/api/auth/complete")
async def auth_complete(payload: dict):
    """Relay the pasted redirect URL to the hub, which exchanges it for a token."""
    body = _hub("POST", "/auth/complete", json={"redirect_url": (payload or {}).get("redirect_url", "")})
    _note_auth(True)
    _exp_cache.clear()
    _gex_cache.clear()
    return {"ok": True, "token_days_left": body.get("relogin_in_days"),
            "message": "Token installed. Levels are live again."}


@app.get("/api/expirations")
def expirations(ticker: str = Query("SPY", max_length=8)):
    """Real listed expirations for the picker (small strikeCount=1 fetch, cached)."""
    t = ticker.upper().strip()
    now = time.time()
    hit = _exp_cache.get(t)
    if hit and now - hit[0] < EXPIRATIONS_TTL_S:
        return {"ticker": t, "expirations": hit[1], "cached": True}
    today = gex.now_et().date()
    try:
        data = _fetch_bounded(_get_client(), gex.to_schwab_symbol(t),
                              from_date=today, to_date=today + timedelta(days=150),
                              strike_count=1)
    except Exception as e:
        _drop_client()
        status, code, msg = _classify(e)
        _note_auth(code != "schwab_token_expired", code)
        raise HTTPException(status_code=status, detail={"code": code, "message": msg})
    # Drop expirations that have already settled (16:00 ET) -- selecting one
    # yields an empty chain (the exact "no data at 23:48" footgun).
    now_dt = gex.now_et()
    dates = set()
    for m in ("callExpDateMap", "putExpDateMap"):
        for k in (data.get(m) or {}):
            d = str(k).split(":")[0]
            try:
                if gex.seconds_to_expiry(date.fromisoformat(d), now_dt) > 0:
                    dates.add(d)
            except ValueError:
                pass
    _note_auth(True)
    out = sorted(dates)
    _exp_cache[t] = (now, out)
    return {"ticker": t, "expirations": out, "cached": False}


def _view_json(label, view, spot, spy_ratio, cfg, today=None):
    if view.get("empty"):
        reason = gex.explain_empty_view(label, today or gex.now_et().date())
        return {"label": label, "empty": True, "n": 0, "reason": reason}
    flip = view["flip_std"]["flip"]
    walls = view["walls"]
    eq =gex.cross_quote(cfg.ticker, flip, spy_ratio) if flip is not None else None
    total_at_spot = view["flip_std"].get("total_at_spot")

    decay = view.get("flip_decay") or {}
    flip_close = decay.get("flip_close")
    flip_move = decay.get("move")

    strikes = view["profile"]["strikes"]
    net = view["profile"]["net"]
    m = (strikes >= spot * (1 - PROFILE_WINDOW)) & (strikes <= spot * (1 + PROFILE_WINDOW))
    return {
        "label": label,
        "empty": False,
        "n": view["n"],
        "total": view["total"],
        "gross": view["gross"],
        "regime": gex.regime_word(spot, flip, total_at_spot),
        "flip": flip,
        "flip_equiv": {"ticker": eq[0], "level": round(eq[1], 2)} if eq else None,
        "flip_close": round(flip_close, 2) if flip_close is not None else None,
        "flip_close_move": round(flip_move, 2) if flip_move is not None else None,
        "crossings": [round(float(c), 2) for c in view["flip_std"]["crossings"]],
        "flip_flipped": view["flip_flipped"]["flip"],
        "low_confidence": view["low_confidence"],
        "is_0dte": "0DTE" in label.upper(),
        "call_wall": walls["call_wall"], "call_wall_gex": walls["call_wall_gex"],
        "put_wall": walls["put_wall"], "put_wall_gex": walls["put_wall_gex"],
        "interpretation": gex.interpretation_line(spot, flip, total_at_spot),
        "profile": {
            "strikes": [round(float(k), 2) for k in strikes[m]],
            "net": [round(float(x)) for x in net[m]],
        },
    }


@app.get("/api/gex")
def api_gex(ticker: str = Query("SPY", max_length=8),
            expiry: str = Query("both"),
            all_days: int = Query(45, ge=1, le=180),
            rate: float = Query(None),
            div_yield: float = Query(None),
            fresh: int = Query(0, ge=0, le=1)):
    t0 = time.time()
    t = ticker.upper().strip()
    exp = expiry.lower().strip()
    today = gex.now_et().date()

    if exp not in ("both", "0dte", "all"):
        try:
            exp_d = date.fromisoformat(exp)
        except ValueError:
            raise HTTPException(status_code=422, detail={
                "code": "bad_expiry",
                "message": "expiry must be 'both', '0dte', 'all', or YYYY-MM-DD"})
        if exp_d < today:   # Schwab 400s on a past fromDate
            raise HTTPException(status_code=422, detail={
                "code": "bad_expiry",
                "message": "expiry {} is in the past; those contracts have settled.".format(exp)})

    # fresh=1 bypasses the cache entirely: the phone's "GET FRESH LEVELS" button
    # must re-pull spot/IV from Schwab, never replay a 60s-old snapshot.
    key = (t, exp, all_days, rate, div_yield)
    hit = _gex_cache.get(key)
    if not fresh and hit and time.time() - hit[0] < CACHE_TTL_S:
        payload = dict(hit[1])
        payload["cached"] = True
        payload["cache_age_s"] = round(time.time() - hit[0], 1)
        return payload

    base = t.lstrip("$")
    q, q_src = gex.div_yield_for(t, div_yield)
    cfg = gex.Config(ticker=t, rate=rate if rate is not None else gex.DEFAULT_RATE,
                     div_yield=q, div_src=q_src)
    expiry_arg = None if exp == "both" else exp
    from_d, to_d = gex.fetch_window(expiry_arg, today, all_days)

    symbol = gex.to_schwab_symbol(t)
    try:
        client = _get_client()
        data = _fetch_bounded(client, symbol, from_date=from_d, to_date=to_d)
        if data.get("status") != "SUCCESS" and symbol.startswith("$") and not symbol.endswith(".X"):
            data = _fetch_bounded(client, symbol + ".X", from_date=from_d, to_date=to_d)
    except Exception as e:
        _drop_client()
        status, code, msg = _classify(e)
        _note_auth(code != "schwab_token_expired", code)
        raise HTTPException(status_code=status, detail={"code": code, "message": msg})

    _note_auth(True)
    # Archive every real fetch. Open interest is never backfillable, so if the
    # Mac slept through the scheduled morning job, an app tap still captures the
    # day. Same-day writes overwrite (identical OI, more complete volume), and
    # this only runs on a cache MISS, so it costs ~20ms against a multi-second
    # network fetch.
    gex.save_chain_snapshot(data, t, chain_dir=os.environ.get("GEX_CHAIN_DIR", gex.CHAIN_DIR))

    contracts, spot, ts_ns, dropped, status = gex.parse_schwab_chain(data)
    if spot is None:
        raise HTTPException(status_code=502, detail={
            "code": "no_spot", "message": "Chain carried no underlying price."})
    usable, dropped_expired, floored = gex.enrich_and_filter_time(contracts, gex.now_et())

    warnings = []
    if not usable:
        if not contracts and dropped.get("no_oi"):
            warnings.append("All strikes had zero open interest"
                            + (" (Schwab has no OI for cash indices -- use SPY/QQQ)"
                               if symbol.startswith("$") else "") + ".")
        elif dropped_expired:
            warnings.append("Requested expiration(s) already settled (16:00 ET).")
        else:
            warnings.append("No usable contracts after filtering (thin chain).")

    spy_ratio = gex.SPY_RATIO_FALLBACK
    if base in ("SPX", "SPY"):
        spy_ratio, _px, _src = gex.fetch_spx_spy_ratio(client, base, spot)

    views = [_view_json(lbl, gex.compute_view(cs, spot, cfg), spot, spy_ratio, cfg, today)
             for lbl, cs in gex.select_views(usable, expiry_arg, today)]

    buckets = gex.gamma_expiry_buckets(usable, spot, cfg, today) if usable else []

    # ATM straddle implied move, priced off the 0DTE chain (or the nearest live
    # expiry once today's has settled). Reported as BOTH the breakeven/expected
    # absolute move and the 1-SD equivalent, since only the latter is comparable
    # to the VIX/16 rule of thumb.
    move = None
    if usable:
        exp_for_move = today if any(c.expiry == today for c in usable) else \
            min(c.expiry for c in usable)
        mv = gex.atm_straddle_move(usable, spot, expiry=exp_for_move)
        if mv:
            move = {**mv, "expiry": mv["expiry"].isoformat(), "is_0dte": mv["expiry"] == today}

    payload = {
        "ticker": t, "symbol": symbol, "spot": spot,
        "oi_date": gex.prior_trading_session(today).isoformat(),
        "as_of_et": gex.now_et().isoformat(),
        "spy_ratio": spy_ratio if base in ("SPX", "SPY") else None,
        "rate": cfg.rate, "div_yield": cfg.div_yield, "div_src": cfg.div_src,
        "convention": cfg.convention.label,
        "requested_expiry": exp, "all_days": all_days,
        "views": views, "buckets": buckets, "implied_move": move,
        "dropped": dropped, "dropped_expired": dropped_expired,
        "warnings": warnings, "runtime_s": round(time.time() - t0, 2),
        "cached": False,
    }
    _gex_cache[key] = (time.time(), payload)
    return payload


@app.exception_handler(HTTPException)
async def _http_exc(request, exc):
    detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content={"error": detail})

# Static UI (mounted last so /api/* wins).
app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
