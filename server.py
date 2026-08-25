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
  * On an auth error the cached Schwab client is dropped, so after you re-run
    scripts/schwab_setup.py the server heals on the next request -- no restart.
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
    """Return a Schwab client built from the CURRENT token file.

    Critical: this server is long-lived, so a cached client can outlive the token
    it was built from (a re-login rewrites the file). Presenting a superseded
    refresh token makes Schwab revoke the whole family -- observed live: a server
    started Jul 23 killed an Aug 2 login within a day. So we rebuild whenever the
    token file changes on disk instead of caching forever.
    """
    global _client
    with _lock:
        if _client is not None and gex.schwab_client_stale(_client):
            _client = None          # token file was rewritten -> discard
        if _client is None:
            _client = gex.get_schwab_client(
                os.environ.get("SCHWAB_APP_KEY"), os.environ.get("SCHWAB_APP_SECRET"),
                os.environ.get("SCHWAB_TOKEN_PATH", gex.DEFAULT_TOKEN_PATH))
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
    if "oauth" in type(exc).__name__.lower() or "token" in s:
        return 503, "schwab_token_expired", \
            "Schwab token expired. On the Mac run: python3 scripts/schwab_setup.py"
    if "timed out" in s or "connection" in s or "reset" in s:
        return 502, "schwab_unreachable", "Schwab API unreachable (transient); retry shortly."
    return 500, "internal_error", str(exc)[:200]


def _token_health():
    path = os.environ.get("SCHWAB_TOKEN_PATH", gex.DEFAULT_TOKEN_PATH)
    out = {"token_present": os.path.exists(path), "token_age_days": None,
           "token_days_left": None}
    try:
        import json
        with open(path) as f:
            ct = json.load(f).get("creation_timestamp")
        if ct:
            age = (time.time() - ct) / 86400.0
            out["token_age_days"] = round(age, 2)
            out["token_days_left"] = round(max(0.0, 7.0 - age), 2)
    except Exception:
        pass
    return out


@app.get("/api/health")
def health():
    h = _token_health()
    h["auth_error"] = _auth_error
    h["ok"] = bool(h["token_present"] and (h["token_days_left"] or 0) > 0
                   and _auth_error is None)
    h["message"] = ("Schwab token rejected -- run scripts/schwab_setup.py"
                    if _auth_error == "schwab_token_expired" else None)
    h["now_et"] = gex.now_et().isoformat()
    return h


# ---------------------------------------------------------------------------
# Phone-based re-authentication (/setup)
# ---------------------------------------------------------------------------
# Schwab refresh tokens are hard-capped at 7 days with no programmatic renewal,
# so re-login is permanent and weekly. These endpoints move that chore off the
# terminal: approve on the phone, paste the redirect URL, done -- from anywhere.
# The app secret never leaves the server; the browser only ever handles the
# short-lived authorization code, exactly as in the CLI flow.
_auth_ctx = {}                  # state -> AuthContext (pending logins)


@app.get("/api/auth/start")
def auth_start():
    key = os.environ.get("SCHWAB_APP_KEY")
    if not key:
        raise HTTPException(status_code=500, detail={
            "code": "no_credentials", "message": "SCHWAB_APP_KEY not set on the server."})
    try:
        from schwab.auth import get_auth_context
    except ImportError:
        raise HTTPException(status_code=500, detail={
            "code": "no_schwab_py", "message": "schwab-py not installed on the server."})
    callback = os.environ.get("SCHWAB_CALLBACK_URL", gex.DEFAULT_CALLBACK)
    ctx = get_auth_context(key, callback)
    _auth_ctx.clear()                       # only one pending login at a time
    _auth_ctx[ctx.state] = ctx
    return {"authorize_url": ctx.authorization_url, "state": ctx.state,
            "callback": callback}


@app.post("/api/auth/complete")
async def auth_complete(payload: dict):
    """Exchange the pasted redirect URL for a token and write it to disk."""
    received = (payload or {}).get("redirect_url", "").strip()
    if "code=" not in received:
        raise HTTPException(status_code=422, detail={
            "code": "bad_redirect",
            "message": "That URL has no 'code=' in it. Copy the FULL address bar "
                       "contents after approving."})
    if not _auth_ctx:
        raise HTTPException(status_code=409, detail={
            "code": "no_pending_login",
            "message": "No login in progress. Tap 'Start login' again."})
    key = os.environ.get("SCHWAB_APP_KEY")
    secret = os.environ.get("SCHWAB_APP_SECRET")
    token_path = os.environ.get("SCHWAB_TOKEN_PATH", gex.DEFAULT_TOKEN_PATH)
    ctx = list(_auth_ctx.values())[0]       # the context that minted this URL
    try:
        import schwab.auth as sa
        writer = getattr(sa, "_" + "_make_update_token_func")(token_path)
        # Serialize against any in-flight API call: a fresh login invalidates the
        # old family, and a concurrent call presenting the OLD token would revoke
        # the NEW one moments after it is created.
        with gex.token_lock(token_path):
            sa.client_from_received_url(key, secret, ctx, received, writer)
    except Exception as e:
        msg = str(e)
        if "state" in msg.lower():
            msg = ("State mismatch -- that redirect came from an older attempt. "
                   "Tap 'Start login' and use only the newest link.")
        raise HTTPException(status_code=400, detail={"code": "exchange_failed",
                                                     "message": msg[:240]})
    _auth_ctx.clear()

    # Validate what was written: a token with no refresh_token dies in 30 min and
    # can never renew -- catch it now, not at tomorrow's 07:45 run.
    try:
        import json as _json
        with open(token_path) as f:
            tok = (_json.load(f).get("token") or {})
        if not tok.get("refresh_token"):
            raise HTTPException(status_code=400, detail={
                "code": "bad_token",
                "message": "Login wrote a token with no refresh token. Try again."})
    except HTTPException:
        raise
    except Exception:
        pass

    _drop_client()                          # force a rebuild from the new token
    _note_auth(True)
    _exp_cache.clear()
    _gex_cache.clear()
    h = _token_health()
    return {"ok": True, "token_days_left": h.get("token_days_left"),
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
    band_vals = [x for x in view.get("flip_band", {}).values() if x is not None]
    if flip is None:
        low_conf = True
    elif band_vals:
        allv = band_vals + [flip]
        low_conf = max(abs(max(allv) - flip), abs(min(allv) - flip)) \
            > gex.MATERIAL_FLIP_MOVE * spot
    else:
        low_conf = False
    eq = gex.cross_quote(cfg.ticker, flip, spy_ratio) if flip is not None else None

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
        "regime": gex.regime_word(spot, flip),
        "flip": flip,
        "flip_equiv": {"ticker": eq[0], "level": round(eq[1], 2)} if eq else None,
        "flip_close": round(flip_close, 2) if flip_close is not None else None,
        "flip_close_move": round(flip_move, 2) if flip_move is not None else None,
        "crossings": [round(float(c), 2) for c in view["flip_std"]["crossings"]],
        "flip_flipped": view["flip_flipped"]["flip"],
        "low_confidence": low_conf,
        "is_0dte": "0DTE" in label.upper(),
        "call_wall": walls["call_wall"], "call_wall_gex": walls["call_wall_gex"],
        "put_wall": walls["put_wall"], "put_wall_gex": walls["put_wall_gex"],
        "interpretation": gex.interpretation_line(spot, flip, view["total"]),
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
            date.fromisoformat(exp)
        except ValueError:
            raise HTTPException(status_code=422, detail={
                "code": "bad_expiry",
                "message": "expiry must be 'both', '0dte', 'all', or YYYY-MM-DD"})

    # fresh=1 bypasses the cache entirely: the phone's "GET FRESH LEVELS" button
    # must re-pull spot/IV from Schwab, never replay a 60s-old snapshot.
    key = (t, exp, all_days, rate, div_yield)
    hit = _gex_cache.get(key)
    if not fresh and hit and time.time() - hit[0] < CACHE_TTL_S:
        payload = dict(hit[1])
        payload["cached"] = True
        payload["cache_age_s"] = round(time.time() - hit[0], 1)
        return payload

    # Config mirrors gex.main(): explicit args win, else per-ticker q map.
    base = t.lstrip("$")
    if div_yield is not None:
        q, q_src = div_yield, "query param"
    elif base in gex.TICKER_DIV_YIELDS:
        q, q_src = gex.TICKER_DIV_YIELDS[base], "built-in map"
    else:
        q, q_src = 0.0, "default 0"
    cfg = gex.Config(ticker=t, rate=rate if rate is not None else gex.DEFAULT_RATE,
                     div_yield=q, div_src=q_src)

    if exp == "0dte":
        from_d = to_d = today
    elif exp in ("all", "both"):
        from_d, to_d = today, today + timedelta(days=all_days)
    else:
        from_d = to_d = date.fromisoformat(exp)

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

    if exp == "both":
        views_raw = [("0DTE", [c for c in usable if c.expiry == today]),
                     ("ALL EXPIRIES", usable)]
    elif exp == "0dte":
        views_raw = [("0DTE", [c for c in usable if c.expiry == today])]
    elif exp == "all":
        views_raw = [("ALL EXPIRIES", usable)]
    else:
        views_raw = [("EXPIRY {}".format(exp), usable)]

    views = []
    for lbl, cs in views_raw:
        views.append(_view_json(lbl, gex.compute_view(cs, spot, cfg), spot, spy_ratio, cfg, today))

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
            move = {
                "expiry": mv["expiry"].isoformat(),
                "is_0dte": mv["expiry"] == today,
                "strike": mv["strike"],
                "call_px": round(mv["call_px"], 2),
                "put_px": round(mv["put_px"], 2),
                "straddle": round(mv["straddle"], 2),
                "pct": mv["pct"],                 # breakeven / expected |move|
                "points": mv["straddle"],
                "sd_pct": mv["sd_pct"],           # 1-SD (VIX/16-comparable)
                "sd_points": mv["sd_points"],
                "iv_atm": mv["iv_atm"],
                "iv_sd_pct": mv["iv_sd_pct"],
            }

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
