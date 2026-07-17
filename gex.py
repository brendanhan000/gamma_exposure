#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gex.py - Dealer Gamma Exposure (GEX) and gamma-flip estimator for SPX 0DTE bias.

Single-file, auditable tool. It pulls the full options-chain snapshot from the
Charles Schwab Trader API, recomputes gamma itself with Black-Scholes-Merton (does NOT trust
the vendor greeks), aggregates dealer dollar-gamma per strike, locates the
zero-gamma "flip" level by repricing total GEX across hypothetical spot prices,
finds the call/put walls, and prints a plain-text bias summary plus a chart.

============================================================================
METHODOLOGY AND ASSUMPTIONS  (made explicit here, in-code, and in the output)
============================================================================

(1) BSM (Merton) GAMMA  -- identical for calls and puts:

        gamma = exp(-q*T) * phi(d1) / (S * sigma * sqrt(T))
        d1    = [ ln(S/K) + (r - q + 0.5*sigma^2) * T ] / (sigma * sqrt(T))

    where phi is the standard-normal PDF, S=spot, K=strike, sigma=implied vol
    (we use Schwab's IV as the input vol), T=time-to-expiry in YEARS, r=risk-free
    rate (--rate), q=dividend yield (--div-yield).

    * We compute gamma OURSELVES; the vendor's own gamma/greeks fields are ignored.
    * T is measured in CALENDAR time (ACT/365) to 16:00 US/Eastern on the
      expiration date -- SPXW 0DTE / PM-settled contracts settle at the cash
      close. As T -> 0 the at-the-money gamma explodes (gamma ~ 1/sqrt(T)); we
      floor T at T_FLOOR_SECONDS and warn when the floor binds.
    * q defaults to 0.0 so the DEFAULT inputs are exactly the five quantities in
      the brief (S, K, T, sigma, r). SPX really yields ~1.3%; pass --div-yield
      to include it. The effect on gamma is tiny for short tenors.

(2) DOLLAR GEX PER CONTRACT  -- "dollar gamma per 1% move":

        GEX = gamma * open_interest * multiplier * S^2 * 0.01

    Interpretation: the dollar change in the aggregate (delta) position for a
    +1% move in the underlying. (gamma*S*0.01 = delta change per 1% move per
    share; * S * multiplier * OI converts that to dollars across the OI.)

(3) DEALER SIGN CONVENTION  -- the model's single biggest weakness, NOT a fact.

    DEFAULT (standard / SqueezeMetrics convention): dealers are LONG call gamma
    and SHORT put gamma, because customers are assumed to net-buy puts (hedges)
    and the street warehouses the other side:

        net GEX = SUM_calls(GEX) - SUM_puts(GEX)

    This is configurable (DealerConvention.call_sign / put_sign). The flip-level
    SENSITIVITY check recomputes everything with the PUT SIGN FLIPPED (puts -> +);
    if the flip moves materially we print a LOW CONFIDENCE warning. Flipping the
    put sign makes every contribution positive, which can remove the zero
    crossing entirely -- an honest demonstration that the flip's *existence*
    hinges on the short-put assumption.

(4) GAMMA FLIP / ZERO-GAMMA LEVEL:

    Reprice the TOTAL net GEX across a fine grid of hypothetical spot prices
    (default +/-5%). At each hypothetical spot we recompute gamma for every
    contract (and the S^2 term) holding K, T, sigma, OI fixed, then sum. The
    flip level is where that total crosses zero; we report the crossing nearest
    to the current spot (and flag if there are several / none).

(5) WALLS:
    Call wall  = strike with the largest POSITIVE net GEX (pin / resistance).
    Put wall   = strike with the largest NEGATIVE net GEX (support that becomes
                 a downside accelerant once breached).

(6) REGIME:
    spot > flip  -> dealers net LONG gamma  -> vol-dampening / mean-reverting.
    spot < flip  -> dealers net SHORT gamma -> vol-amplifying / trend-prone.

CAVEATS BUILT INTO THE OUTPUT: OI is end-of-prior-session (it updates overnight),
so the 0DTE GEX *lags* intraday positioning; the dealer sign convention is an
assumption; thin early-morning chains are handled by dropping contracts with no
OI / no IV and reporting the counts.

Setup (one time):
    export SCHWAB_APP_KEY=...  SCHWAB_APP_SECRET=...   # from developer.schwab.com
    python3 scripts/schwab_setup.py                    # schwab-py OAuth login + verify

Usage:
    python3 gex.py                 # 0DTE + all expiries, SPY (default)
    python3 gex.py --expiry 0dte
    python3 gex.py --expiry 2026-06-19 --rate 0.043
    python3 gex.py --ticker QQQ    # any optionable ETF/equity with listed OI
    python3 gex.py --demo          # offline synthetic chain, no credentials

NOTE: the default ticker is SPY (not SPX) because Schwab returns zero open
interest for cash-index ($SPX) options -- index GEX is impossible on this
source; SPY is the standard proxy and levels cross-quote to SPX via the live
SPX/SPY ratio.
"""
from __future__ import annotations

import warnings
# Quiet a benign LibreSSL notice emitted by urllib3 on stock macOS Python.
warnings.filterwarnings("ignore", message=r".*OpenSSL.*", module="urllib3")

import argparse
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, date, timezone, timedelta

import numpy as np
from scipy.stats import norm

# ---------------------------------------------------------------------------
# Defaults / tunables  (ALL are surfaced in the printed assumptions block)
# ---------------------------------------------------------------------------
# --- Charles Schwab Trader API (data source; auth via the schwab-py library) ---
# OAuth + token refresh are handled by schwab-py (see scripts/schwab_setup.py),
# mirroring the sibling overnight_vs_intraday project's setup.
DEFAULT_CALLBACK   = "https://127.0.0.1:8182"   # must EXACTLY match your Schwab app's callback
DEFAULT_TOKEN_PATH = ".schwab_token.json"        # project-local token file (git-ignored)

DEFAULT_TICKER       = "SPY"      # SPY, not SPX: Schwab returns ZERO open interest
                                  # for cash-index ($SPX) options, and GEX is
                                  # OI-weighted -- SPX GEX is impossible on this
                                  # source. SPY is the standard dealer-gamma proxy.
DEFAULT_MULTIPLIER   = 100        # standard equity/ETF & index option multiplier
DEFAULT_RATE         = 0.043      # risk-free, ~3M T-bill ballpark; OVERRIDE with --rate
DEFAULT_DIV_YIELD     = 0.0       # SPX really ~1.3%; default 0 (see methodology)
DEFAULT_PRICE_RANGE   = 0.05      # +/-5% repricing window for the flip search
DEFAULT_GRID_STEPS    = 1000      # grid resolution for the flip search
DAY_COUNT             = 365.0     # ACT/365 calendar-time convention
EXPIRY_HOUR_ET        = 16        # PM-settled SPXW expire at the 16:00 ET cash close
T_FLOOR_SECONDS       = 300.0     # floor T at 5 min so ATM 0DTE gamma stays finite
SPY_RATIO_FALLBACK    = 10.0      # used only if the live SPX/SPY ratio is unavailable
MATERIAL_FLIP_MOVE    = 0.01      # flip move > 1% of spot under flipped sign => LOW CONFIDENCE
PLOT_WINDOW_FRAC      = 0.08      # chart x-axis: spot +/- 8%

# Schwab cash-index symbols carry a "$" prefix (see SCHWAB_INDEX_SYMBOLS below).


# ---------------------------------------------------------------------------
# Data model + config
# ---------------------------------------------------------------------------
@dataclass
class Contract:
    """One option line from the chain snapshot, post-filtering."""
    strike: float
    expiry: date
    cp: str            # 'call' or 'put'
    oi: float          # open interest (contracts)
    iv: float          # implied volatility (decimal, e.g. 0.12)
    T: float = 0.0     # time-to-expiry in years; filled in by enrich step


@dataclass
class DealerConvention:
    """Sign applied to call vs put dollar-gamma to model dealer positioning."""
    call_sign: float = 1.0
    put_sign: float = -1.0
    label: str = "standard (dealers long calls, short puts)"


# Standard convention and the "flipped put sign" used for the sensitivity check.
CONV_STANDARD = DealerConvention(1.0, -1.0, "standard (dealers long calls, short puts)")
CONV_FLIPPED  = DealerConvention(1.0,  1.0, "flipped put sign (dealers long calls AND long puts)")


@dataclass
class Config:
    ticker: str = DEFAULT_TICKER
    multiplier: int = DEFAULT_MULTIPLIER
    rate: float = DEFAULT_RATE
    div_yield: float = DEFAULT_DIV_YIELD
    price_range: float = DEFAULT_PRICE_RANGE
    steps: int = DEFAULT_GRID_STEPS
    convention: DealerConvention = field(default_factory=lambda: CONV_STANDARD)
    flipped_convention: DealerConvention = field(default_factory=lambda: CONV_FLIPPED)


# ===========================================================================
# Black-Scholes-Merton gamma
# ===========================================================================
def compute_gamma_bsm(S, K, T, sigma, r=DEFAULT_RATE, q=0.0):
    """Merton (dividend-adjusted BSM) gamma. Identical for calls and puts:

        gamma = exp(-q*T) * phi(d1) / (S * sigma * sqrt(T))
        d1    = [ ln(S/K) + (r - q + sigma^2/2) * T ] / (sigma * sqrt(T))

    The exp(-q*T) factor is part of the closed form (Hull Ch. 19); omitting it
    overstates gamma by ~q*T when a dividend yield is supplied. With q=0 it
    reduces to the classic BSM gamma.

    Fully vectorized: S, K, T, sigma may be scalars or broadcastable arrays.
    Returns 0 wherever an input is non-positive / undefined (T<=0, sigma<=0,
    S<=0, K<=0) so callers never get NaN/inf from a degenerate contract.
    """
    S = np.asarray(S, dtype=float)
    K = np.asarray(K, dtype=float)
    T = np.asarray(T, dtype=float)
    sigma = np.asarray(sigma, dtype=float)

    valid = (T > 0) & (sigma > 0) & (S > 0) & (K > 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        vol_t = sigma * np.sqrt(T)
        d1 = (np.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / vol_t
        gamma = np.exp(-q * T) * norm.pdf(d1) / (S * vol_t)
    return np.where(valid, gamma, 0.0)


# ===========================================================================
# Per-contract / per-strike GEX
# ===========================================================================
def _to_arrays(contracts, convention):
    """Pack a contract list into parallel numpy arrays for vectorized math."""
    K     = np.array([c.strike for c in contracts], dtype=float)
    T     = np.array([c.T for c in contracts], dtype=float)
    iv    = np.array([c.iv for c in contracts], dtype=float)
    oi    = np.array([c.oi for c in contracts], dtype=float)
    iscall = np.array([c.cp == "call" for c in contracts], dtype=bool)
    sign  = np.where(iscall, convention.call_sign, convention.put_sign).astype(float)
    return K, T, iv, oi, sign, iscall


def _signed_dollar_gex(K, T, iv, oi, sign, S, cfg):
    """Per-contract signed dollar-GEX at hypothetical spot S (dealer sign applied)."""
    gamma = compute_gamma_bsm(S, K, T, iv, cfg.rate, cfg.div_yield)
    dollar = gamma * oi * cfg.multiplier * (S ** 2) * 0.01
    return sign * dollar


def compute_gex_profile(contracts, spot, convention, cfg):
    """Aggregate signed dollar-GEX per strike at the current spot.

    Returns dict with sorted unique 'strikes' and aligned 'net' / 'call_gex' /
    'put_gex' (all signed under the dealer convention) plus 'total'.
    """
    K, T, iv, oi, sign, iscall = _to_arrays(contracts, convention)
    signed = _signed_dollar_gex(K, T, iv, oi, sign, spot, cfg)

    strikes = np.unique(K)
    idx = np.searchsorted(strikes, K)
    net = np.zeros(len(strikes))
    call_gex = np.zeros(len(strikes))
    put_gex = np.zeros(len(strikes))
    np.add.at(net, idx, signed)
    np.add.at(call_gex, idx[iscall], signed[iscall])
    np.add.at(put_gex, idx[~iscall], signed[~iscall])

    return {
        "strikes": strikes,
        "net": net,
        "call_gex": call_gex,
        "put_gex": put_gex,
        "total": float(signed.sum()),
    }


def gross_dollar_gamma(contracts, spot, cfg):
    """Sum of |dollar gamma| across all contracts at spot (sign-agnostic).

    Used to report what fraction of total gamma sits in 0DTE vs later expiries.
    """
    if not contracts:
        return 0.0
    K, T, iv, oi, sign, iscall = _to_arrays(contracts, cfg.convention)
    gamma = compute_gamma_bsm(spot, K, T, iv, cfg.rate, cfg.div_yield)
    return float(np.sum(np.abs(gamma * oi * cfg.multiplier * (spot ** 2) * 0.01)))


# ===========================================================================
# Gamma flip / zero-gamma level
# ===========================================================================
def find_flip_level(contracts, spot, convention, cfg, price_range=None, steps=None):
    """Find the zero-gamma (flip) spot by repricing TOTAL net GEX on a grid.

    For each hypothetical spot S' in [spot*(1-range), spot*(1+range)] we recompute
    gamma for every contract and sum the signed dollar-GEX, then locate sign
    changes and linearly interpolate the crossings. Returns the crossing nearest
    the current spot as 'flip' (None if there is no crossing in range), plus all
    'crossings' and the ('grid','curve') for plotting/debugging.
    """
    price_range = cfg.price_range if price_range is None else price_range
    steps = cfg.steps if steps is None else steps

    K, T, iv, oi, sign, iscall = _to_arrays(contracts, convention)
    grid = np.linspace(spot * (1.0 - price_range), spot * (1.0 + price_range), steps)
    curve = np.empty_like(grid)
    for i, S in enumerate(grid):
        gamma = compute_gamma_bsm(S, K, T, iv, cfg.rate, cfg.div_yield)
        curve[i] = np.sum(sign * gamma * oi * cfg.multiplier * (S ** 2) * 0.01)

    # Locate zero crossings: exact grid zeros and sign changes (interpolated).
    crossings = []
    s = np.sign(curve)
    for i in range(len(grid) - 1):
        if s[i] == 0.0:
            crossings.append(float(grid[i]))
        elif s[i] * s[i + 1] < 0.0:
            x0, x1, y0, y1 = grid[i], grid[i + 1], curve[i], curve[i + 1]
            crossings.append(float(x0 - y0 * (x1 - x0) / (y1 - y0)))
    if s[-1] == 0.0:
        crossings.append(float(grid[-1]))

    crossings = np.array(crossings, dtype=float)
    nearest = None
    if crossings.size:
        nearest = float(crossings[np.argmin(np.abs(crossings - spot))])

    return {
        "flip": nearest,
        "crossings": crossings,
        "grid": grid,
        "curve": curve,
        "total_at_spot": float(curve[np.argmin(np.abs(grid - spot))]),
    }


def _flip_with_put_sign(contracts, spot, cfg, put_sign):
    """Helper for the graded put-sign sensitivity band: flip at a scaled put sign."""
    conv = DealerConvention(cfg.convention.call_sign, put_sign, "scaled")
    return find_flip_level(contracts, spot, conv, cfg)["flip"]


def find_walls(profile):
    """Call wall = strike of max (most positive) net GEX; put wall = strike of min."""
    strikes = profile["strikes"]
    net = profile["net"]
    if strikes.size == 0:
        return {"call_wall": None, "call_wall_gex": None,
                "put_wall": None, "put_wall_gex": None}
    i_call = int(np.argmax(net))
    i_put = int(np.argmin(net))
    return {
        "call_wall": float(strikes[i_call]),
        "call_wall_gex": float(net[i_call]),
        "put_wall": float(strikes[i_put]),
        "put_wall_gex": float(net[i_put]),
    }


# ===========================================================================
# Time-to-expiry helpers
# ===========================================================================
def _et_tz():
    """America/New_York tz (handles DST). Falls back to a fixed -04:00 offset."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:
        return timezone(timedelta(hours=-4))


def now_et():
    return datetime.now(tz=_et_tz())


def seconds_to_expiry(expiry, now):
    """Seconds from `now` to 16:00 ET on the expiration date (can be negative)."""
    et = _et_tz()
    expiry_dt = datetime(expiry.year, expiry.month, expiry.day,
                         EXPIRY_HOUR_ET, 0, 0, tzinfo=et)
    if now.tzinfo is None:
        now = now.replace(tzinfo=et)
    return (expiry_dt - now).total_seconds()


def enrich_and_filter_time(contracts, now):
    """Fill Contract.T (years). Drop already-expired contracts; floor tiny T.

    Returns (kept_contracts, n_dropped_expired, n_floored).
    """
    kept, dropped_expired, floored = [], 0, 0
    for c in contracts:
        secs = seconds_to_expiry(c.expiry, now)
        if secs <= 0:
            dropped_expired += 1
            continue
        if secs < T_FLOOR_SECONDS:
            floored += 1
            secs = T_FLOOR_SECONDS
        c.T = secs / (DAY_COUNT * 24.0 * 3600.0)
        kept.append(c)
    return kept, dropped_expired, floored


# ===========================================================================
# Charles Schwab Trader API -- Market Data (option chains) via schwab-py
# ===========================================================================
# Auth + token refresh are delegated to the schwab-py library (mirrors the
# sibling overnight_vs_intraday project):
#   * Register an app at developer.schwab.com -> App Key + App Secret (from env
#     SCHWAB_APP_KEY / SCHWAB_APP_SECRET; NEVER hardcoded), callback :8182.
#   * Run `python3 scripts/schwab_setup.py` ONCE to log in via browser and write
#     the token file (.schwab_token.json); refresh tokens last ~7 days.
#   * schwab-py needs Python >= 3.10, so its import is LAZY -- --demo and the unit
#     tests still run on 3.9. One get_option_chain() call returns the whole chain
#     plus the underlying spot, OI and IV; Schwab market data is free.

SCHWAB_INDEX_SYMBOLS = {  # cash indices take a "$" prefix on Schwab
    "SPX": "$SPX", "NDX": "$NDX", "RUT": "$RUT", "VIX": "$VIX", "DJI": "$DJI",
}


def to_schwab_symbol(ticker):
    """Map a friendly ticker to Schwab's symbol ('SPX' -> '$SPX'; 'SPY' stays 'SPY')."""
    t = ticker.upper().strip()
    if t.startswith("$"):
        return t
    return SCHWAB_INDEX_SYMBOLS.get(t, t)


def get_schwab_client(app_key, app_secret, token_path, callback=DEFAULT_CALLBACK):
    """Build a schwab-py client from a cached token file.

    OAuth and token refresh are delegated to schwab-py (the token is written by
    scripts/schwab_setup.py). Raises a clear RuntimeError if the credentials or
    token file are missing, or if schwab-py isn't installed. The ``schwab`` import
    is LAZY so the rest of the tool -- and the unit tests -- run on Python 3.9,
    where schwab-py (which needs >= 3.10) cannot be installed.
    """
    if not (app_key and app_secret):
        raise RuntimeError(
            "SCHWAB_APP_KEY / SCHWAB_APP_SECRET not set. Register a Market Data app "
            "on developer.schwab.com and export the credentials.")
    if not os.path.exists(token_path):
        raise RuntimeError(
            "No Schwab token at {!r}. Run the one-time login first:\n"
            "    python3 scripts/schwab_setup.py\n"
            "(refresh tokens expire after ~7 days, so re-run weekly).".format(token_path))
    try:
        from schwab.auth import client_from_token_file
    except ImportError as exc:
        raise RuntimeError(
            "schwab-py is required for live data: pip install 'schwab-py>=1.3' "
            "(needs Python >= 3.10).") from exc
    return client_from_token_file(token_path, app_key, app_secret)


def fetch_chain_schwab(client, symbol, from_date=None, to_date=None, strike_count=None):
    """One schwab-py get_option_chain() call -> full chain JSON (spot + OI + IV).

    `client` is a schwab-py client (see get_schwab_client). `from_date`/`to_date`
    are datetime.date objects. contractType defaults to ALL server-side, so both
    callExpDateMap and putExpDateMap come back in one un-paginated payload.
    """
    kwargs = {"include_underlying_quote": True}
    if from_date is not None:
        kwargs["from_date"] = from_date
    if to_date is not None:
        kwargs["to_date"] = to_date
    if strike_count is not None:
        kwargs["strike_count"] = strike_count
    resp = client.get_option_chain(symbol, **kwargs)
    resp.raise_for_status()
    return resp.json()


def parse_schwab_chain(data):
    """Parse a Schwab /chains response into (contracts, spot, ts_ns, dropped, status).

    Schwab specifics handled here:
      * callExpDateMap / putExpDateMap are keyed "YYYY-MM-DD:DTE" -> strike -> [opt].
        We take the expiration date from the map key (most reliable).
      * `volatility` is a PERCENT (e.g. 18.42) and uses -999.0 / non-finite as the
        "no IV" sentinel -> convert to a decimal and drop sentinels.
      * spot comes straight from `underlyingPrice` (fallback underlying.mark/last).
      * Skips contracts with no/zero OI or no/sentinel IV, counting the drops.
    """
    contracts = []
    dropped = {"no_oi": 0, "no_iv": 0, "malformed": 0}
    status = data.get("status")

    under = data.get("underlying") or {}
    spot = data.get("underlyingPrice")
    if spot is None:
        spot = under.get("mark") or under.get("last")
    spot = float(spot) if spot is not None else None

    # Underlying quote time is ms-epoch; convert to ns to match the rest of the tool.
    ts_ms = under.get("quoteTime") or under.get("tradeTime")
    ts_ns = int(ts_ms) * 1_000_000 if ts_ms else None

    for map_key, cp in (("callExpDateMap", "call"), ("putExpDateMap", "put")):
        exp_map = data.get(map_key) or {}
        for exp_key, by_strike in exp_map.items():
            try:
                exp_date = date.fromisoformat(str(exp_key).split(":")[0])
            except ValueError:
                dropped["malformed"] += sum(len(v) for v in by_strike.values())
                continue
            for strike_str, opts in by_strike.items():
                for o in opts:
                    try:
                        strike = float(o.get("strikePrice", strike_str))
                    except (TypeError, ValueError):
                        dropped["malformed"] += 1
                        continue
                    oi = o.get("openInterest")
                    if oi is None or float(oi) <= 0:
                        dropped["no_oi"] += 1
                        continue
                    try:
                        ivf = float(o.get("volatility"))
                    except (TypeError, ValueError):
                        dropped["no_iv"] += 1
                        continue
                    # Guard NaN/inf and non-positive IV; Schwab's "no IV"
                    # sentinel (-999.0) is caught by the <= 0 test.
                    if not math.isfinite(ivf) or ivf <= 0:
                        dropped["no_iv"] += 1
                        continue
                    contracts.append(Contract(strike, exp_date, cp,
                                              float(oi), ivf / 100.0))
    return contracts, spot, ts_ns, dropped, status


def fetch_spx_spy_ratio(client, base_ticker, spot):
    """Live SPX/SPY ratio, runnable from EITHER leg of the pair.

    base_ticker is the underlying we're analyzing ('SPX' or 'SPY') and `spot` its
    price from the chain; the other leg is fetched with one Schwab quote call.
    Verified response shape: {"<symbol>": {"quote": {"lastPrice": ...}}}; index
    quotes ($SPX) populate lastPrice/closePrice but may leave mark None, hence
    the fallback order. Returns (spx_over_spy_ratio, other_leg_px, source_label).
    """
    other = "SPY" if base_ticker == "SPX" else "$SPX"
    try:
        resp = client.get_quote(other)
        resp.raise_for_status()
        q = ((resp.json().get(other) or {}).get("quote")) or {}
        px = q.get("lastPrice") or q.get("mark") or q.get("closePrice")
        if px:
            px = float(px)
            ratio = (spot / px) if base_ticker == "SPX" else (px / spot)
            return ratio, px, "live Schwab {} quote".format(other)
    except Exception:
        pass
    return SPY_RATIO_FALLBACK, None, "fallback (hardcoded ~10, NOT live)"


# ===========================================================================
# Formatting helpers
# ===========================================================================
def fmt_usd(x):
    if x is None:
        return "n/a"
    sign = "-" if x < 0 else ""
    return "{}${:,.0f}".format(sign, abs(x))


def fmt_bn(x):
    if x is None:
        return "n/a"
    return "{:+.3f} $Bn".format(x / 1e9)


def fmt_px(x):
    return "n/a" if x is None else "{:,.2f}".format(x)


# ===========================================================================
# Per-view computation
# ===========================================================================
def compute_view(contracts, spot, cfg):
    """Run the full pipeline for one slice of the chain (0DTE / all / a date)."""
    if not contracts:
        return {"empty": True, "n": 0}
    profile = compute_gex_profile(contracts, spot, cfg.convention, cfg)
    walls = find_walls(profile)
    flip_std = find_flip_level(contracts, spot, cfg.convention, cfg)
    # Spec-required literal check: flip the put sign (puts -> +). This makes every
    # contribution positive, so the flip typically VANISHES -- an honest sign that
    # the flip's existence rests on the short-put assumption.
    flip_flp = find_flip_level(contracts, spot, cfg.flipped_convention, cfg)
    # Graded sensitivity: vary the short-put MAGNITUDE +/-50% so we get a real
    # "how far does it move" number (the binary flip alone never crosses zero).
    base_put = cfg.convention.put_sign
    flip_band = {}
    if base_put != 0:
        for scale in (0.5, 1.5):
            flip_band[scale] = _flip_with_put_sign(contracts, spot, cfg, base_put * scale)
    gross = gross_dollar_gamma(contracts, spot, cfg)
    return {
        "empty": False,
        "n": len(contracts),
        "profile": profile,
        "walls": walls,
        "flip_std": flip_std,
        "flip_flipped": flip_flp,
        "flip_band": flip_band,
        "total": profile["total"],
        "gross": gross,
    }


def regime_word(spot, flip):
    if flip is None:
        return "UNDETERMINED"
    return "LONG gamma" if spot > flip else "SHORT gamma"


def interpretation_line(spot, flip, total):
    if flip is None:
        if total > 0:
            return ("No zero-gamma crossing in range and total GEX is POSITIVE: "
                    "model says dealers are net long gamma throughout -> expect "
                    "vol-dampening / mean reversion. (Flip likely sits below the search window.)")
        return ("No zero-gamma crossing in range and total GEX is NEGATIVE: "
                "model says dealers are net short gamma throughout -> expect "
                "vol-amplification / trend risk. (Flip likely sits above the search window.)")
    if spot > flip:
        dist = (spot - flip) / spot * 100.0
        return ("Spot is {:.2f}% ABOVE the flip -> dealers net LONG gamma: they sell "
                "rallies / buy dips, dampening vol. Bias: range-bound, fade extremes, "
                "watch for a pin near the call wall. Losing the flip flips the regime."
                ).format(dist)
    dist = (flip - spot) / spot * 100.0
    return ("Spot is {:.2f}% BELOW the flip -> dealers net SHORT gamma: they buy "
            "rallies / sell dips, amplifying vol. Bias: momentum/trend, wider ranges; "
            "a break of the put wall can accelerate lower. Reclaiming the flip calms it."
            ).format(dist)


# ===========================================================================
# Rendering
# ===========================================================================
def print_assumptions(cfg, rate_is_default):
    print("=" * 78)
    print("ASSUMPTIONS  (every number below is a modeling choice, not ground truth)")
    print("=" * 78)
    print("  Data source ......... Charles Schwab Trader API (Market Data /chains)")
    print("  Gamma source ........ computed via Black-Scholes-Merton from Schwab IV")
    print("                        (Schwab's own gamma/greeks are IGNORED).")
    print("  Dealer convention ... {}".format(cfg.convention.label))
    print("                        call_sign={:+.0f}  put_sign={:+.0f}  (flippable)"
          .format(cfg.convention.call_sign, cfg.convention.put_sign))
    print("  Risk-free rate r .... {:.4f}{}".format(
        cfg.rate, "   <-- DEFAULT; set --rate to your current value" if rate_is_default else ""))
    print("  Dividend yield q .... {:.4f}   (SPY ~0.012, QQQ ~0.006; default 0 -> set --div-yield)"
          .format(cfg.div_yield))
    print("  Multiplier .......... {}".format(cfg.multiplier))
    print("  Day count ........... ACT/{:.0f}, time-to-expiry to {:02d}:00 ET (PM settle)"
          .format(DAY_COUNT, EXPIRY_HOUR_ET))
    print("  Flip search ......... total net GEX repriced over +/-{:.0%} in {} steps"
          .format(cfg.price_range, cfg.steps))
    print("  GEX formula ......... gamma * OI * {} * spot^2 * 0.01   ($ per 1% move)"
          .format(cfg.multiplier))
    print("  Net GEX ............. SUM(call GEX) - SUM(put GEX) under the convention above")
    print()


def print_data_health(spot, ts_ns, dropped, dropped_expired, floored, n_kept, today, prior_session):
    print("-" * 78)
    print("DATA HEALTH & STALENESS")
    print("-" * 78)
    print("  Spot used ........... {}".format(fmt_px(spot)))
    if ts_ns:
        try:
            t = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).astimezone(_et_tz())
            age = (now_et() - t).total_seconds()
            print("  Underlying snapshot . {} ET  (age {:.0f}s)".format(t.strftime("%Y-%m-%d %H:%M:%S"), age))
        except Exception:
            print("  Underlying snapshot . {} (ns)".format(ts_ns))
    print("  Contracts used ...... {}".format(n_kept))
    print("  Dropped: no OI={}  no IV={}  malformed={}  expired={}".format(
        dropped["no_oi"], dropped["no_iv"], dropped["malformed"], dropped_expired))
    if floored:
        print("  WARNING: {} contract(s) had time-to-expiry below the {:.0f}s floor; T was "
              "clamped (ATM 0DTE gamma is numerically explosive near the close).".format(floored, T_FLOOR_SECONDS))
    print()
    print("  *** OPEN-INTEREST STALENESS (read this) ***")
    print("  OI updates only ONCE per day (overnight, from the OCC end-of-day file).")
    print("  Today is {}; the OI here reflects the {} close.".format(
        today.isoformat(), prior_session.isoformat()))
    print("  => For 0DTE this is the key caveat: positions opened TODAY are NOT in this")
    print("     OI, so the 0DTE GEX/flip LAGS real intraday dealer positioning.")
    print()


def print_side_by_side(views):
    """Totals table across the computed views (e.g. 0DTE vs ALL)."""
    labels = [lbl for lbl, _ in views]
    print("-" * 78)
    print("NET DEALER GEX  ($ per 1% move)   [#1 KEY OUTPUT]")
    print("-" * 78)
    header = "  {:<26}".format("") + "".join("{:>22}".format(l) for l in labels)
    print(header)

    def row(name, fn):
        cells = ""
        for _, v in views:
            cells += "{:>22}".format("(empty)" if v.get("empty") else fn(v))
        print("  {:<26}{}".format(name, cells))

    row("Total net GEX", lambda v: fmt_bn(v["total"]))
    row("Total net GEX ($)", lambda v: fmt_usd(v["total"]))
    row("Gross |gamma| ($/1%)", lambda v: fmt_bn(v["gross"]))
    row("Contracts", lambda v: str(v["n"]))
    print()


def cross_quote(ticker, value, spy_ratio):
    """Cross-quote a level between SPX and SPY. Returns (other_label, value) or None."""
    t = ticker.upper().lstrip("$")
    if t == "SPX":
        return ("SPY", value / spy_ratio)   # SPX -> SPY (divide by ~10)
    if t == "SPY":
        return ("SPX", value * spy_ratio)   # SPY -> SPX (multiply by ~10)
    return None


def print_view_detail(label, view, spot, spy_ratio, cfg):
    print("-" * 78)
    print("VIEW: {}".format(label))
    print("-" * 78)
    if view.get("empty"):
        print("  No usable contracts in this slice (thin/empty chain).")
        print()
        return

    flip = view["flip_std"]["flip"]
    flip_flp = view["flip_flipped"]["flip"]
    walls = view["walls"]

    # ---- Flip level (#2) ----
    print("  Gamma flip / zero-gamma level [#2]:")
    if flip is None:
        tot = view["flip_std"]["total_at_spot"]
        print("    No zero crossing within +/-{:.0%}. Total GEX at spot = {} ({} regime)."
              .format(cfg.price_range, fmt_bn(tot), "LONG" if tot > 0 else "SHORT"))
    else:
        print("    {}  {:,.2f}".format(cfg.ticker, flip))
        eq = cross_quote(cfg.ticker, flip, spy_ratio)
        if eq:
            print("    {}-equiv  {:,.2f}   (SPX/SPY ratio {:.3f})".format(eq[0], eq[1], spy_ratio))
        if view["flip_std"]["crossings"].size > 1:
            extra = ", ".join("{:,.0f}".format(c) for c in view["flip_std"]["crossings"])
            print("    NOTE: {} crossings in range [{}]; reporting the one nearest spot."
                  .format(view["flip_std"]["crossings"].size, extra))

    # ---- Walls (#3, #4) ----
    print("  Call wall [#3] (resistance/pin): {} {}   net GEX {}".format(
        cfg.ticker, fmt_px(walls["call_wall"]), fmt_bn(walls["call_wall_gex"])))
    print("  Put wall  [#4] (support/accel):  {} {}   net GEX {}".format(
        cfg.ticker, fmt_px(walls["put_wall"]), fmt_bn(walls["put_wall_gex"])))

    # ---- Sensitivity / LOW CONFIDENCE (guardrail) ----
    # The dealer put-sign convention is the model's single biggest assumption, so
    # we probe it two ways: (a) the spec-required literal flip (puts -> +), and
    # (b) a graded +/-50% move on the short-put magnitude that yields an actual
    # "how far does the flip move" number. (a) almost always removes the flip
    # entirely -- reported as a structural caveat; (b) drives the LOW CONFIDENCE call.
    band = view.get("flip_band", {})
    print("  PUT-SIGN SENSITIVITY (the model's biggest assumption -- not a fact):")
    print("    Base flip (standard, dealers short puts) ... {}".format(fmt_px(flip)))
    if flip_flp is None:
        print("    Literal put-sign flip (puts -> long) ....... NO flip (dealers long ALL gamma)")
        print("      => A gamma flip exists ONLY because we assume dealers are short puts.")
        print("         This is the model's biggest structural weakness; keep it in mind.")
    elif flip is not None:
        print("    Literal put-sign flip (puts -> long) ....... {} (move {:,.2f} SPX)"
              .format(fmt_px(flip_flp), abs(flip - flip_flp)))
    else:
        print("    Literal put-sign flip (puts -> long) ....... {} (base had no flip in range)"
              .format(fmt_px(flip_flp)))

    low_conf = False
    band_vals = [v for v in band.values() if v is not None]
    if flip is not None and band_vals:
        allv = band_vals + [flip]
        lo, hi = min(allv), max(allv)
        move = max(abs(hi - flip), abs(lo - flip))
        print("    Short-put magnitude +/-50% ................. flip in [{:,.2f} .. {:,.2f}]"
              .format(lo, hi))
        print("                                                 max move {:,.2f} SPX = {:.2f}% of spot"
              .format(move, move / spot * 100.0))
        if move > MATERIAL_FLIP_MOVE * spot:
            low_conf = True
    elif flip is None:
        low_conf = True  # we couldn't even locate a base flip in range

    if low_conf:
        print("    *** LOW CONFIDENCE: the flip level is materially sensitive to the")
        print("        (assumed) dealer put positioning. Treat the regime as directional")
        print("        context, not a precise level. ***")
    elif flip is not None and band_vals:
        print("    OK: flip is robust to a +/-50% change in the short-put magnitude")
        print("        (< {:.0%} of spot), though it still hinges on dealers being short puts."
              .format(MATERIAL_FLIP_MOVE))
    print()


def render_summary(label, view, spot, spy_ratio, cfg):
    """The required plain-text bias block (#6)."""
    print("#" * 78)
    print("# BIAS SUMMARY  --  {}".format(label))
    print("#" * 78)
    if view.get("empty"):
        print("  (no data)")
        print("#" * 78)
        print()
        return
    flip = view["flip_std"]["flip"]
    walls = view["walls"]
    reg = regime_word(spot, flip)
    print("  Current spot ...... {} {:,.2f}".format(cfg.ticker, spot))
    print("  Regime ............ {}  (spot {} flip)".format(
        reg, ">" if (flip is not None and spot > flip) else "<" if flip is not None else "?"))
    if flip is not None:
        line = "  Gamma flip ........ {} {:,.2f}".format(cfg.ticker, flip)
        eq = cross_quote(cfg.ticker, flip, spy_ratio)
        if eq:
            line += "  |  {} {:,.2f}".format(eq[0], eq[1])
        print(line)
    else:
        print("  Gamma flip ........ none in +/-{:.0%} window".format(cfg.price_range))
    print("  Call wall ......... {} {}".format(cfg.ticker, fmt_px(walls["call_wall"])))
    print("  Put wall .......... {} {}".format(cfg.ticker, fmt_px(walls["put_wall"])))
    print("  Total net GEX ..... {}  ({})".format(fmt_bn(view["total"]), fmt_usd(view["total"])))
    print("  Interpretation .... " + interpretation_line(spot, flip, view["total"]))
    print("#" * 78)
    print()


# ===========================================================================
# Plot
# ===========================================================================
def plot_profile(label, view, spot, flip, walls, ticker, outpath,
                 window_frac=PLOT_WINDOW_FRAC, x_tick_step=10.0):
    """Per-strike net-GEX bar chart with spot / flip / walls marked. Saved to file.

    The x-axis (price) is ticked and gridded on round multiples of `x_tick_step`
    (default 10), auto-coarsened for high-priced underlyings so labels stay legible.
    """
    import matplotlib
    matplotlib.use("Agg")  # headless / no display needed
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MultipleLocator

    profile = view["profile"]
    strikes = profile["strikes"]
    net = profile["net"] / 1e9  # $Bn per 1%
    mask = (strikes >= spot * (1 - window_frac)) & (strikes <= spot * (1 + window_frac))
    if not mask.any():
        mask = np.ones_like(strikes, dtype=bool)
    ks, ns = strikes[mask], net[mask]

    spacing = np.median(np.diff(np.unique(ks))) if ks.size > 1 else 5.0
    width = max(spacing * 0.85, 0.5)

    fig, ax = plt.subplots(figsize=(12, 6.5))
    colors = np.where(ns >= 0, "#1a9850", "#d73027")  # green = +GEX, red = -GEX
    ax.bar(ks, ns, width=width, color=colors, alpha=0.85,
           label="net dealer GEX per strike")
    ax.axhline(0, color="black", lw=0.8)

    ax.axvline(spot, color="black", ls="-", lw=1.6, label="spot {:,.0f}".format(spot))
    if flip is not None:
        ax.axvline(flip, color="#2166ac", ls="--", lw=1.8, label="flip {:,.0f}".format(flip))
    if walls["call_wall"] is not None:
        ax.axvline(walls["call_wall"], color="#1a9850", ls=":", lw=1.8,
                   label="call wall {:,.0f}".format(walls["call_wall"]))
    if walls["put_wall"] is not None:
        ax.axvline(walls["put_wall"], color="#d73027", ls=":", lw=1.8,
                   label="put wall {:,.0f}".format(walls["put_wall"]))

    # X-axis on round price levels: ticks + gridlines every `x_tick_step` (default
    # 10). Coarsen to a nice multiple (x2, x2.5, x2 -> 20, 50, 100, ...) if the
    # window would produce too many ticks, so high-priced names (e.g. SPX) stay
    # readable; multiples of 10 are unaffected for SPY/QQQ-priced underlyings.
    base = float(x_tick_step) if x_tick_step and x_tick_step > 0 else 10.0
    span = float(ks.max() - ks.min()) if ks.size else 0.0
    step = base
    _bumps = (2.0, 2.5, 2.0)
    _i = 0
    while span > 0 and span / step > 25:
        step *= _bumps[_i % 3]
        _i += 1
    ax.xaxis.set_major_locator(MultipleLocator(step))
    if span > 0 and span / step > 15:   # rotate only when ticks get dense
        ax.tick_params(axis="x", labelrotation=45)

    ax.set_title("{} dealer GEX profile - {}  (green=long gamma, red=short gamma)"
                 .format(ticker, label))
    ax.set_xlabel("strike / price level  (gridlines every {:g})".format(step))
    ax.set_ylabel("net dealer GEX  ($Bn per 1% move)")
    ax.legend(loc="best", fontsize=9)
    ax.grid(True, axis="both", alpha=0.3, linestyle=":")
    fig.tight_layout()
    fig.savefig(outpath, dpi=120)
    plt.close(fig)
    return outpath


# ===========================================================================
# Demo (offline) chain
# ===========================================================================
def make_demo_chain(spot, today, later):
    """Deterministic synthetic SPX-like chain for offline testing (NO network).

    Calls cluster above spot, puts below (so a put wall sits under and a call wall
    over the market) with a simple vol skew. Purely illustrative.
    """
    contracts = []
    lo = int(round(spot * 0.90 / 5.0) * 5)
    hi = int(round(spot * 1.10 / 5.0) * 5)
    for K in range(lo, hi + 1, 5):
        m = (K - spot) / spot                     # moneyness
        iv = max(0.05, 0.12 + 0.6 * m * m - 0.35 * m)  # skew: richer puts
        call_oi = 2000.0 * math.exp(-(((K - spot * 1.03) / (spot * 0.02)) ** 2)) + 400.0
        put_oi  = 2600.0 * math.exp(-(((K - spot * 0.97) / (spot * 0.02)) ** 2)) + 500.0
        for exp in (today, later):
            scale = 1.0 if exp == today else 0.7
            contracts.append(Contract(float(K), exp, "call", round(call_oi * scale), iv))
            contracts.append(Contract(float(K), exp, "put",  round(put_oi * scale), iv))
    return contracts


# ===========================================================================
# CLI / main
# ===========================================================================
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Dealer gamma exposure (GEX) and gamma-flip estimator (SPX 0DTE bias).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--ticker", default=DEFAULT_TICKER,
                   help="underlying (default SPY). NOTE: Schwab has no open interest for "
                        "cash indices ($SPX etc.), so index GEX is impossible here -- use "
                        "the ETF (SPY/QQQ).")
    p.add_argument("--expiry", default=None,
                   help="'0dte' | 'all' | YYYY-MM-DD. Default: compute BOTH 0DTE and all expiries.")
    p.add_argument("--rate", type=float, default=DEFAULT_RATE, help="risk-free rate (annual, decimal).")
    p.add_argument("--div-yield", type=float, default=DEFAULT_DIV_YIELD, help="dividend yield (annual, decimal).")
    p.add_argument("--multiplier", type=int, default=DEFAULT_MULTIPLIER, help="contract multiplier.")
    p.add_argument("--price-range", type=float, default=DEFAULT_PRICE_RANGE, help="+/- fraction for flip search.")
    p.add_argument("--steps", type=int, default=DEFAULT_GRID_STEPS, help="grid steps for flip search.")
    p.add_argument("--convention", choices=["standard", "flipped"], default="standard",
                   help="dealer sign convention. standard=long calls/short puts.")
    p.add_argument("--call-sign", type=float, default=None, help="override dealer call sign (+1/-1).")
    p.add_argument("--put-sign", type=float, default=None, help="override dealer put sign (+1/-1).")
    p.add_argument("--all-days", type=int, default=45,
                   help="for 'all'/default: fetch expiries from today out this many days. "
                        "Kept modest because Schwab 502s on very large chains (e.g. a full "
                        "year of SPX); raise it for more coverage at the risk of a 502.")
    p.add_argument("--out-prefix", default=None, help="output chart filename prefix.")
    p.add_argument("--x-tick", type=float, default=10.0,
                   help="chart x-axis tick/gridline spacing in price levels "
                        "(auto-coarsened for high-priced underlyings).")
    p.add_argument("--no-plot", action="store_true", help="skip chart generation.")
    p.add_argument("--callback", default=os.environ.get("SCHWAB_CALLBACK_URL", DEFAULT_CALLBACK),
                   help="OAuth callback URL; must EXACTLY match your Schwab app config.")
    p.add_argument("--token-path", default=None,
                   help="Schwab token file (default .schwab_token.json or $SCHWAB_TOKEN_PATH). "
                        "Create it with: python3 scripts/schwab_setup.py")
    p.add_argument("--levels-only", action="store_true",
                   help="print only a compact levels block (for notifications / quick pulls).")
    p.add_argument("--demo", action="store_true", help="run on an offline synthetic chain (no credentials).")
    return p.parse_args(argv)


def build_config(args):
    conv = CONV_STANDARD if args.convention == "standard" else CONV_FLIPPED
    # Power-user overrides take precedence and stay auditable in the printed label.
    if args.call_sign is not None or args.put_sign is not None:
        cs = args.call_sign if args.call_sign is not None else conv.call_sign
        ps = args.put_sign if args.put_sign is not None else conv.put_sign
        conv = DealerConvention(cs, ps, "custom (call_sign={:+.0f}, put_sign={:+.0f})".format(cs, ps))
    # Sensitivity always flips the put sign relative to the chosen convention.
    flipped = DealerConvention(conv.call_sign, -conv.put_sign,
                               "put sign flipped to {:+.0f}".format(-conv.put_sign))
    return Config(ticker=args.ticker, multiplier=args.multiplier, rate=args.rate,
                  div_yield=args.div_yield, price_range=args.price_range, steps=args.steps,
                  convention=conv, flipped_convention=flipped)


def prior_trading_session(today):
    """Most recent weekday strictly before `today` (holidays not modeled)."""
    d = today - timedelta(days=1)
    while d.weekday() >= 5:  # Sat=5, Sun=6
        d -= timedelta(days=1)
    return d


def select_views(all_contracts, expiry_arg, today):
    """Return list of (label, contracts) per the --expiry selection."""
    by_0dte = [c for c in all_contracts if c.expiry == today]
    if expiry_arg is None:
        return [("0DTE", by_0dte), ("ALL EXPIRIES", all_contracts)]
    if expiry_arg.lower() == "0dte":
        return [("0DTE", by_0dte)]
    if expiry_arg.lower() == "all":
        return [("ALL EXPIRIES", all_contracts)]
    # explicit date
    d = date.fromisoformat(expiry_arg)
    return [("EXPIRY {}".format(expiry_arg), [c for c in all_contracts if c.expiry == d])]


def render_levels_compact(label, view, spot, spy_ratio, cfg, today):
    """Compact, stable levels block for --levels-only (notifications / quick pulls)."""
    oi_date = prior_trading_session(today).isoformat()
    if view.get("empty"):
        print("{} | {}: no usable contracts (empty/thin chain).".format(cfg.ticker, label))
        print()
        return
    flip = view["flip_std"]["flip"]
    walls = view["walls"]
    print("{} | {} | spot {} | OI {}".format(cfg.ticker, label, fmt_px(spot), oi_date))
    print("  regime .... {}".format(regime_word(spot, flip)))
    if flip is not None:
        eq = cross_quote(cfg.ticker, flip, spy_ratio)
        extra = "  ({} {})".format(eq[0], fmt_px(eq[1])) if eq else ""
        print("  flip ...... {}{}".format(fmt_px(flip), extra))
    else:
        print("  flip ...... none in +/-{:.0%}".format(cfg.price_range))
    print("  call wall . {}".format(fmt_px(walls["call_wall"])))
    print("  put wall .. {}".format(fmt_px(walls["put_wall"])))
    print("  net GEX ... {}".format(fmt_bn(view["total"])))
    print()


def run(cfg, args, all_contracts, spot, spy_ratio, today, ts_ns, dropped,
        dropped_expired, floored, rate_is_default):
    """Compute + print everything given an already-fetched/parsed chain."""
    views_raw = select_views(all_contracts, args.expiry, today)
    computed = [(lbl, compute_view(cs, spot, cfg)) for lbl, cs in views_raw]

    if args.levels_only:
        for lbl, view in computed:
            render_levels_compact(lbl, view, spot, spy_ratio, cfg, today)
    else:
        print_assumptions(cfg, rate_is_default)
        print_data_health(spot, ts_ns, dropped, dropped_expired, floored,
                           len(all_contracts), today, prior_trading_session(today))
        print_side_by_side(computed)

        # Fraction of gamma in 0DTE vs later (needs the all-expiries population)
        if args.expiry is None:
            all_view = dict(computed)["ALL EXPIRIES"]
            zero_view = dict(computed)["0DTE"]
            if not all_view.get("empty") and all_view["gross"] > 0:
                frac = (0.0 if zero_view.get("empty") else zero_view["gross"]) / all_view["gross"]
                print("-" * 78)
                print("GAMMA CONCENTRATION")
                print("-" * 78)
                print("  0DTE gross gamma / all-expiry gross gamma = {:.1%}".format(frac))
                print("  (remaining {:.1%} sits in later expiries)".format(1 - frac))
                print()

        for lbl, view in computed:
            print_view_detail(lbl, view, spot, spy_ratio, cfg)
        for lbl, view in computed:
            render_summary(lbl, view, spot, spy_ratio, cfg)

    if not args.no_plot:
        prefix = args.out_prefix or "gex_{}_{}".format(cfg.ticker.replace(":", ""), today.isoformat())
        for lbl, view in computed:
            if view.get("empty"):
                continue
            safe = lbl.lower().replace(" ", "_")
            outpath = "{}_{}.png".format(prefix, safe)
            plot_profile(lbl, view, spot, view["flip_std"]["flip"], view["walls"],
                         cfg.ticker, outpath, x_tick_step=args.x_tick)
            print("  chart saved: {}".format(outpath))
        print()


def main(argv=None):
    t_start = time.time()
    args = parse_args(argv)
    cfg = build_config(args)
    rate_is_default = abs(args.rate - DEFAULT_RATE) < 1e-12

    print("=" * 78)
    print("DEALER GAMMA EXPOSURE (GEX)  /  GAMMA FLIP  --  {}".format(cfg.ticker))
    print("=" * 78)
    print()

    today = now_et().date()

    # Validate --expiry up front so BOTH the demo and live paths reject garbage
    # cleanly instead of tracebacking later in select_views().
    if args.expiry and args.expiry.lower() not in ("0dte", "all"):
        try:
            date.fromisoformat(args.expiry)
        except ValueError:
            print("ERROR: --expiry must be '0dte', 'all', or YYYY-MM-DD.", file=sys.stderr)
            return 2

    if args.demo:
        print(">>> DEMO MODE: synthetic offline chain, NOT live data. <<<\n")
        # Ticker-appropriate synthetic spot so labels/cross-quotes stay sane.
        spot = {"SPX": 5900.0, "SPY": 590.0, "QQQ": 500.0}.get(
            cfg.ticker.upper().lstrip("$"), 1000.0)
        # Use a representative mid-session timestamp so the synthetic 0DTE bucket
        # is populated no matter what wall-clock time the demo is run at (after the
        # 16:00 ET close, real 0DTE has expired and would correctly be empty).
        real = now_et()
        session_close = datetime(today.year, today.month, today.day, 15, 55, tzinfo=_et_tz())
        demo_now = real if real < session_close else \
            datetime(today.year, today.month, today.day, 13, 0, tzinfo=_et_tz())
        demo = make_demo_chain(spot, today, today + timedelta(days=30))
        all_contracts, dropped_expired, floored = enrich_and_filter_time(demo, demo_now)
        ts_ns = int(demo_now.timestamp() * 1e9)
        dropped = {"no_oi": 0, "no_iv": 0, "malformed": 0}
        spy_ratio = SPY_RATIO_FALLBACK
        run(cfg, args, all_contracts, spot, spy_ratio, today, ts_ns, dropped,
            dropped_expired, floored, rate_is_default)
        print("runtime: {:.2f}s".format(time.time() - t_start))
        return 0

    app_key = os.environ.get("SCHWAB_APP_KEY")
    app_secret = os.environ.get("SCHWAB_APP_SECRET")
    if not app_key or not app_secret:
        print("ERROR: set SCHWAB_APP_KEY and SCHWAB_APP_SECRET in your environment "
              "(never hardcode them).", file=sys.stderr)
        print("       Create a Market-Data app at developer.schwab.com to get them.",
              file=sys.stderr)
        print("       Or run `python3 gex.py --demo` for an offline synthetic example.",
              file=sys.stderr)
        return 2

    token_path = args.token_path or os.environ.get("SCHWAB_TOKEN_PATH", DEFAULT_TOKEN_PATH)
    try:
        client = get_schwab_client(app_key, app_secret, token_path, callback=args.callback)
    except RuntimeError as e:
        print("ERROR: {}".format(e), file=sys.stderr)
        return 2

    # Translate --expiry into a date window (date objects for schwab-py). Schwab
    # returns one un-paginated payload for the whole window; we still partition in
    # memory so the default run shows 0DTE and all-expiries side by side.
    if args.expiry and args.expiry.lower() == "0dte":
        from_date = to_date = today
    elif args.expiry and args.expiry.lower() == "all":
        from_date, to_date = today, today + timedelta(days=args.all_days)
    elif args.expiry:
        from_date = to_date = date.fromisoformat(args.expiry)  # validated above
    else:  # default: both 0DTE and all -> fetch the full near-dated window
        from_date, to_date = today, today + timedelta(days=args.all_days)

    symbol = to_schwab_symbol(cfg.ticker)
    print("Fetching Schwab option chain for {} ({} .. {}) ...".format(symbol, from_date, to_date))
    try:
        data = fetch_chain_schwab(client, symbol, from_date=from_date, to_date=to_date)
        # Some index symbols use the legacy '.X' suffix; retry once if unsuccessful.
        if data.get("status") != "SUCCESS" and symbol.startswith("$") and not symbol.endswith(".X"):
            data = fetch_chain_schwab(client, symbol + ".X", from_date=from_date, to_date=to_date)
    except Exception as e:  # schwab-py uses httpx; catch any transport/HTTP error
        print("ERROR fetching chain: {}".format(e), file=sys.stderr)
        if "502" in str(e) or "Bad Gateway" in str(e):
            print("  (A 502 usually means the requested chain is too large; "
                  "reduce --all-days or pass a specific --expiry.)", file=sys.stderr)
        return 1

    contracts, spot, ts_ns, dropped, status = parse_schwab_chain(data)
    if status and status != "SUCCESS":
        print("  WARNING: Schwab chains status = {}".format(status))
    print("  {} raw contracts parsed.".format(
        len(contracts) + dropped["no_oi"] + dropped["no_iv"] + dropped["malformed"]))
    if spot is None:
        print("ERROR: could not determine underlying spot price from the chain.",
              file=sys.stderr)
        return 1

    all_contracts, dropped_expired, floored = enrich_and_filter_time(contracts, now_et())
    if not all_contracts:
        print("WARNING: no usable contracts after filtering.")
        if not contracts and dropped["no_oi"]:
            # Everything was dropped for ZERO open interest at parse time.
            if symbol.startswith("$"):
                print("  NOTE: Schwab returns ZERO open interest for cash-INDEX options "
                      "like {}. GEX is OI-weighted, so this source cannot do".format(symbol))
                print("        index GEX -- use the ETF proxy instead:  --ticker SPY")
            else:
                print("  NOTE: all strikes showed 0 open interest. OI is an overnight (OCC) "
                      "figure; before the morning post it may not be available yet.")

    # SPX<->SPY cross-quote ratio: fetched live when running either leg of the
    # pair; meaningless for other tickers (QQQ, ...), where no ratio is printed
    # and no cross-quote appears in the output.
    base = cfg.ticker.upper().lstrip("$")
    spy_ratio = SPY_RATIO_FALLBACK
    if base in ("SPX", "SPY"):
        spy_ratio, other_px, ratio_src = fetch_spx_spy_ratio(client, base, spot)
        print("  SPX/SPY ratio: {:.3f} ({})\n".format(spy_ratio, ratio_src))
    else:
        print()

    run(cfg, args, all_contracts, spot, spy_ratio, today, ts_ns, dropped,
        dropped_expired, floored, rate_is_default)

    print("runtime: {:.2f}s".format(time.time() - t_start))
    return 0


if __name__ == "__main__":
    sys.exit(main())
