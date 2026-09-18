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
    * PER-STRIKE IV: every contract is priced with its OWN vendor IV -- the smile
      is never flattened to a single ATM vol (flattening destroys wing gamma and
      is the most common silent GEX bug).
    * EXERCISE STYLE: gamma is the European (Black-Scholes-Merton) closed form for
      ALL contracts. SPY/QQQ options are American; the early-exercise premium is
      ignored. The difference concentrates in deep-ITM (low-gamma) strikes and is
      small for the short-dated/near-the-money strikes that dominate GEX, but it
      is an approximation, not an oversight.
    * CLOCK CONSISTENCY: T is CALENDAR time (ACT/365) to 16:00 US/Eastern (PM
      settle at the cash close), NOT trading time (390x252). This is deliberate:
      the input IVs are vendor quotes annualized on a calendar clock, and gamma
      only needs the total variance sigma^2*T to be internally consistent.
      Pairing vendor sigma with a trading-time T would break that pairing and
      inflate 0DTE gamma severalfold; a trading-time clock is only correct when
      IV is re-derived from prices under the same clock.
    * As T -> 0 the at-the-money gamma explodes (gamma ~ 1/sqrt(T)); we floor T
      at T_FLOOR_SECONDS and warn when the floor binds.
    * q defaults to 0.0 so the DEFAULT inputs are exactly the five quantities in
      the brief (S, K, T, sigma, r). SPX really yields ~1.3%; pass --div-yield
      to include it. The effect on gamma is tiny for short tenors.

(2) DOLLAR GEX PER CONTRACT  -- "dollar gamma per 1% move":

        GEX = gamma * open_interest * multiplier * S^2 * 0.01

    Interpretation: the dollar change in the aggregate (delta) position for a
    +1% move in the underlying. (gamma*S*0.01 = delta change per 1% move per
    share; * S * multiplier * OI converts that to dollars across the OI.)

    UNIT WARNING: this is the PER-1% convention. The per-POINT convention is
    gamma*OI*mult*S (= this / (0.01*S)); SqueezeMetrics' published series is
    per-point. Comparing per-1% numbers to per-point numbers is a category error.

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
    (default +/-10%). At each hypothetical spot we recompute gamma for every
    contract (and the S^2 term) holding K, T, sigma, OI fixed, then sum. The
    flip level is where that total crosses zero; we report the crossing nearest
    to the current spot (and flag if there are several / none -- the curve can
    legitimately have multiple zero crossings).

    STICKY-STRIKE CAVEAT: holding each contract's IV fixed while S moves is the
    sticky-strike assumption. Reality sits between sticky-strike and
    sticky-delta (the smile follows moneyness as spot moves), so the flip is an
    estimate under a stated vol-dynamics assumption, not a model-free level.

    TIME-DECAY CAVEAT: the flip is computed at the CURRENT T and is NOT static.
    As T decays toward the close, ATM gamma (~1/sqrt(T)) grows and the zero-gamma
    level migrates -- materially so for 0DTE-heavy chains. The output therefore
    also reports the flip projected at the 16:00 ET close (every contract's T
    advanced, K/sigma/OI held fixed) so the migration is visible, not hidden.

(5) WALLS  (per-side, industry convention):
    Call wall  = strike with the largest CALL-side dollar gamma (pin / resistance).
    Put wall   = strike with the largest PUT-side dollar gamma, in absolute terms
                 (support that becomes a downside accelerant once breached).
    Walls are computed PER-SIDE, not from net GEX: a strike with huge call AND
    put OI nets to ~zero but still carries enormous gross gamma and acts as a
    real pin, so netting would hide it. Each wall measures its own side's size.

(6) REGIME:
    spot > flip  -> dealers net LONG gamma  -> vol-dampening / mean-reverting.
    spot < flip  -> dealers net SHORT gamma -> vol-amplifying / trend-prone.

SCOPE (single-underlying, NOT the full SPX complex): a complete dealer-gamma
picture for the S&P complex would aggregate SPX + SPXW + XSP + SPY + ES options
normalized to a common notional. That is impossible on this data source: Schwab
returns no open interest for cash-index options ($SPX/XSP) and no futures
options (ES). This tool therefore measures the gamma of ONE listed underlying
(SPY or QQQ) -- a liquid, correlated PROXY for the complex, not its total.
Levels are in the traded underlying's own terms; magnitudes understate the
full-complex dealer book. Summing raw GEX across underlyings without notional
normalization would be meaningless and is deliberately not offered.

CAVEATS BUILT INTO THE OUTPUT: OI is end-of-prior-session (it updates overnight),
so the 0DTE GEX *lags* intraday positioning; the dealer sign convention is an
assumption (dealer-direction data like CBOE open-close is not available here);
thin early-morning chains are handled by dropping contracts with no OI / no IV
(and crossed quotes) and reporting the counts.

Setup (one time):
    ../schwab_hub/run.sh login                         # Schwab login, weekly (hub owns the token)
    ../schwab_hub/run.sh                               # leave the hub running
    python3 scripts/schwab_setup.py                    # verify the live chain path

Usage:
    python3 gex.py                 # 0DTE + all expiries, SPY (default)
    python3 gex.py --expiry 0dte
    python3 gex.py --expiry week      # pool all expiries in the current Mon-Fri week
    python3 gex.py --expiry 2026-06-19 --rate 0.0469
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
from dataclasses import dataclass, field, replace
from datetime import datetime, date, timezone, timedelta
from zoneinfo import ZoneInfo

import numpy as np

# ---------------------------------------------------------------------------
# Defaults / tunables  (ALL are surfaced in the printed assumptions block)
# ---------------------------------------------------------------------------
# --- Charles Schwab Trader API (data source; auth lives in the central schwab_hub) ---

DEFAULT_TICKER       = "SPY"      # SPY, not SPX: Schwab returns ZERO open interest
                                  # for cash-index ($SPX) options, and GEX is
                                  # OI-weighted -- SPX GEX is impossible on this
                                  # source. SPY is the standard dealer-gamma proxy.
DEFAULT_MULTIPLIER   = 100        # standard equity/ETF & index option multiplier
DEFAULT_RATE         = 0.0469     # risk-free, ~3M T-bill ballpark; OVERRIDE with --rate
DEFAULT_DIV_YIELD     = 0.0       # generic fallback; per-ticker map below applies when known
DEFAULT_PRICE_RANGE   = 0.10      # +/-10% repricing window for the flip search (a deep
                                  # short-gamma day can push the flip well past +/-5%)

# Approximate trailing dividend yields, applied when --div-yield is NOT passed.
# "Get dividends right": q enters d1 and the exp(-q*T) gamma discount. Values are
# ballpark and printed at runtime; override with --div-yield for precision.
TICKER_DIV_YIELDS = {"SPY": 0.012, "QQQ": 0.006, "SPX": 0.013, "IWM": 0.011, "DIA": 0.017}

# Quote-quality filters ("garbage IV -> garbage gamma"):
#   * crossed quotes (bid > ask) are dropped anywhere -- stale/locked markets.
#   * DEEP-ITM contracts (intrinsic depth > ITM_DEPTH_FILTER of spot) with no bid
#     or a relative spread wider than MAX_ITM_REL_SPREAD are dropped: their IV is
#     extracted from a sliver of extrinsic value and is noise-dominated.
#   * Deep-OTM wings are NEVER quote-filtered (zero-bid wings still carry real
#     tail gamma via the ask-side IV; dropping them would destroy wing gamma).
ITM_DEPTH_FILTER    = 0.05        # "deep ITM" = more than 5% in the money
MAX_ITM_REL_SPREAD  = 0.25        # deep-ITM (ask-bid)/mid wider than this = stale/garbage
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
    oi: float          # open interest (contracts) -- PRIOR session close
    iv: float          # implied volatility (decimal, e.g. 0.12)
    T: float = 0.0     # time-to-expiry in years; filled in by enrich step
    volume: float = 0.0  # TODAY's traded contracts (live, unlike OI)
    size: float = None   # exposure weight actually used; set by apply_weighting()
    bid: float = None    # live quote -- kept for straddle pricing
    ask: float = None
    last: float = None


@dataclass
class DealerConvention:
    """Sign applied to call vs put dollar-gamma to model dealer positioning."""
    call_sign: float = 1.0
    put_sign: float = -1.0
    label: str = "standard (dealers long calls, short puts)"


CONV_STANDARD = DealerConvention()


@dataclass
class Config:
    ticker: str = DEFAULT_TICKER
    multiplier: int = DEFAULT_MULTIPLIER
    rate: float = DEFAULT_RATE
    div_yield: float = DEFAULT_DIV_YIELD
    div_src: str = "default 0"    # where q came from (flag / built-in map / default)
    price_range: float = DEFAULT_PRICE_RANGE
    steps: int = DEFAULT_GRID_STEPS
    convention: DealerConvention = field(default_factory=lambda: CONV_STANDARD)


# ===========================================================================
# Black-Scholes-Merton gamma
# ===========================================================================
# Standard normal PDF computed directly with numpy rather than scipy.stats.norm.pdf,
# which carries heavy per-call overhead. The flip search evaluates phi() tens of
# millions of times per run, so this is the single biggest runtime factor
# (~30x faster; a full SPY/QQQ run drops from ~60s to a few seconds).
_INV_SQRT_2PI = 1.0 / math.sqrt(2.0 * math.pi)


def _norm_pdf(x):
    """phi(x): standard-normal density. Vectorized; matches scipy.stats.norm.pdf."""
    return _INV_SQRT_2PI * np.exp(-0.5 * x * x)


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
        gamma = np.exp(-q * T) * _norm_pdf(d1) / (S * vol_t)
    return np.where(valid, gamma, 0.0)


# ===========================================================================
# Two profiles: intraday (trading layer) vs structural (overview layer)
# ===========================================================================
# The same contract set answers two different questions, and conflating them is
# a modeling error:
#
#   INTRADAY (trading layer) -- 0DTE through the front week, plus the next
#     monthly OpEx. That is where essentially all real-time hedging pressure
#     lives. Weighted by OI BLENDED WITH TODAY'S VOLUME, because prior-close OI
#     is stale by mid-morning on 0DTE and systematically understates the gamma
#     actually driving the tape. This is the single biggest intraday error
#     source. The flip and nearest walls move as spot traverses strikes and as
#     0DTE volume builds, so this profile must be recomputed on a fast cadence
#     (re-run it, or refresh the phone app); a level computed at 09:35 is not the level at 13:00.
#
#   STRUCTURAL (overview layer) -- every expiry in the window, weighted by OI
#     ONLY. Volume is today's churn; OI is durable positioning. This profile
#     barely moves day to day, and that is the point: it locates the multi-day
#     walls and the regime flip (above it dealers are long gamma and suppress
#     vol, below it they are short and amplify).
#
# Both use PER-STRIKE implied vol -- the surface is never collapsed to a single
# vol point, which would smear the flip across expiries whose vols genuinely
# differ (front-week vs monthly).
FRONT_WEEK_DAYS = 5          # "front week" horizon for the intraday profile
VOL_WEIGHT_0DTE = 1.0        # today's volume counts fully for 0DTE
VOL_WEIGHT_FRONT = 0.5       # partially for the rest of the front week


def contract_weight(c, weighting, today):
    """Exposure weight for one contract: OI, or OI blended with today's volume.

    'oi'    -> prior-session open interest. Durable positioning (structural).
    'blend' -> oi + volume * w(DTE), where w = 1.0 for 0DTE, 0.5 out to the
               front week, 0.0 beyond. Rationale: 0DTE positions open AND close
               within the session, so prior-close OI describes contracts that
               existed overnight, not what is being hedged now.

    ASSUMPTION (stated, not hidden): traded volume includes closing as well as
    opening trades, so the blend is an UPPER BOUND on fresh positioning. It is
    deliberately biased toward over-counting near-dated activity rather than
    under-counting it, because understating live 0DTE gamma is the larger error.
    """
    if weighting != "blend":
        return float(c.oi)
    dte = (c.expiry - today).days
    if dte <= 0:
        w = VOL_WEIGHT_0DTE
    elif dte <= FRONT_WEEK_DAYS:
        w = VOL_WEIGHT_FRONT
    else:
        w = 0.0
    return float(c.oi) + float(c.volume or 0.0) * w


def apply_weighting(contracts, weighting, today):
    """Stamp c.size on every contract; all downstream math uses size, not oi."""
    for c in contracts:
        c.size = contract_weight(c, weighting, today)
    return contracts


def intraday_horizon(today):
    """Last expiry the intraday profile includes: front week OR next monthly OpEx."""
    return max(today + timedelta(days=FRONT_WEEK_DAYS), next_monthly_opex(today))


def select_profile_contracts(contracts, profile, today):
    """Filter the contract set for a profile and stamp the right exposure weight.

    Returns a NEW list of copies so the two profiles never share mutated state.
    """
    import copy
    if profile == "intraday":
        horizon = intraday_horizon(today)
        sel = [c for c in contracts if c.expiry <= horizon]
        weighting = "blend"
    else:                                   # structural
        sel = list(contracts)
        weighting = "oi"
    return apply_weighting([copy.copy(c) for c in sel], weighting, today)


def is_rth(now=None):
    """True during regular trading hours (Mon-Fri 09:30-16:00 ET)."""
    now = now_et() if now is None else now
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= mins < 16 * 60


def option_price(c):
    """Best available price for one contract: mid of the quote, else last.

    Mid ((bid+ask)/2) is the standard mark for straddle pricing -- `last` can be
    minutes stale on a quiet strike, and on 0DTE a stale print badly distorts the
    implied move. Returns None when nothing usable exists.
    """
    b, a, l = c.bid, c.ask, c.last
    if b is not None and a is not None and a >= b and a > 0:
        return 0.5 * (b + a)
    if l is not None and l > 0:
        return float(l)
    return None


# Expected |move| of a lognormal at horizon T is sigma*sqrt(T)*sqrt(2/pi).
# An ATM straddle costs ~0.7979 * S * sigma * sqrt(T), i.e. it prices the
# EXPECTED ABSOLUTE move, not the 1-standard-deviation move. Dividing by this
# constant converts one to the other.
STRADDLE_TO_SD = math.sqrt(math.pi / 2.0)          # 1.2533


def atm_straddle_move(contracts, spot, expiry=None):
    """Implied move from the ATM straddle: (ATM call + ATM put) / spot.

    This is the market's own priced-in move and is more precise than the VIX/16
    rule of thumb, which is a crude annual->daily scaling (sqrt(252) ~ 15.87) of
    a 30-day variance-swap index rather than a quote on the actual session.

    WHAT THE NUMBER MEANS -- the distinction that matters:
      * straddle / spot  = the BREAKEVEN move, and equals the EXPECTED ABSOLUTE
        move E|dS|/S. This is what you pay to own the move.
      * 1-SD move        = straddle/spot * sqrt(pi/2) ~ x1.2533. THIS is the
        quantity VIX/16 estimates, so only this one is comparable to it.
    Reporting the straddle as if it were 1-SD understates the band by ~20%.

    The ATM strike is the listed strike nearest spot that has BOTH a call and a
    put with a usable price. Returns None when the expiry has no usable pair.
    """
    if not contracts or not spot:
        return None
    sel = [c for c in contracts if expiry is None or c.expiry == expiry]
    if not sel:
        return None

    # Index priced calls/puts by strike, then take the nearest strike quoting both.
    calls, puts = {}, {}
    for c in sel:
        px = option_price(c)
        if px is None:
            continue
        (calls if c.cp == "call" else puts)[c.strike] = (px, c)
    both = sorted(set(calls) & set(puts), key=lambda k: abs(k - spot))
    if not both:
        return None

    K = both[0]
    call_px, call_c = calls[K]
    put_px, put_c = puts[K]
    straddle = call_px + put_px
    pct = straddle / spot

    # IV-implied 1-SD for the same horizon, as an independent cross-check.
    iv_atm = 0.5 * ((call_c.iv or 0.0) + (put_c.iv or 0.0))
    T = call_c.T or put_c.T or 0.0
    iv_sd_pct = iv_atm * math.sqrt(T) if (iv_atm > 0 and T > 0) else None

    return {
        "strike": K,
        "distance_from_spot": K - spot,
        "call_px": call_px,
        "put_px": put_px,
        "straddle": straddle,
        "expiry": call_c.expiry,
        # Breakeven / expected absolute move.
        "pct": pct,
        "points": straddle,
        # 1-SD equivalent -- the VIX/16-comparable figure.
        "sd_pct": pct * STRADDLE_TO_SD,
        "sd_points": straddle * STRADDLE_TO_SD,
        "iv_atm": iv_atm,
        "iv_sd_pct": iv_sd_pct,
        "T": T,
    }


def zero_dte_cliff(contracts, spot, cfg, today, now=None):
    """How much of this profile evaporates at today's close, and when.

    The intraday flip is only valid until the 0DTE contracts settle. After the
    16:00 ET close the profile you were trading is gone -- this quantifies the
    drop so it is never a surprise.
    """
    now = now_et() if now is None else now
    zero = [c for c in contracts if c.expiry == today]
    if not zero:
        return None
    total = gross_dollar_gamma(contracts, spot, cfg)
    if total <= 0:
        return None
    secs = max(0.0, seconds_to_expiry(today, now))
    return {
        "share": gross_dollar_gamma(zero, spot, cfg) / total,
        "seconds_left": secs,
        "hhmm": "{}h {:02d}m".format(int(secs // 3600), int((secs % 3600) // 60)),
        "n": len(zero),
    }


# ===========================================================================
# Per-contract / per-strike GEX
# ===========================================================================
def _to_arrays(contracts, convention):
    """Pack a contract list into parallel numpy arrays for vectorized math.

    The exposure weight is c.size when set by apply_weighting() (which may blend
    today's volume into OI for the intraday profile), else plain open interest.
    """
    K     = np.array([c.strike for c in contracts], dtype=float)
    T     = np.array([c.T for c in contracts], dtype=float)
    iv    = np.array([c.iv for c in contracts], dtype=float)
    oi    = np.array([c.oi if c.size is None else c.size for c in contracts], dtype=float)
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
    K, T, iv, size, sign, iscall = _to_arrays(contracts, cfg.convention)
    gamma = compute_gamma_bsm(spot, K, T, iv, cfg.rate, cfg.div_yield)
    return float(np.sum(np.abs(gamma * size * cfg.multiplier * (spot ** 2) * 0.01)))


# ===========================================================================
# Gamma flip / zero-gamma level
# ===========================================================================
def _total_net_gex_at(S, K, T, iv, oi, sign, cfg):
    """Total signed net dollar-GEX at a single hypothetical spot S.

    This is the objective the flip search zeroes. Kept as a standalone so the
    root-finder can reprice at arbitrary S (not just grid nodes) and so
    'total_at_spot' is evaluated EXACTLY at the current spot rather than read off
    the nearest grid node (which need not contain spot and can even carry the
    opposite sign on a steep 0DTE curve).
    """
    gamma = compute_gamma_bsm(S, K, T, iv, cfg.rate, cfg.div_yield)
    return float(np.sum(sign * gamma * oi * cfg.multiplier * (S ** 2) * 0.01))


def _refine_root(f, x0, x1):
    """Bisect a bracketed root of f (f(x0), f(x1) opposite signs) to 1e-10."""
    a, b = float(x0), float(x1)
    fa = f(a)
    for _ in range(200):
        m = 0.5 * (a + b)
        fm = f(m)
        if fm == 0.0 or (b - a) < 1e-10:
            return m
        if fa * fm < 0.0:
            b = m
        else:
            a, fa = m, fm
    return 0.5 * (a + b)


def find_flip_level(contracts, spot, convention, cfg, price_range=None, steps=None):
    """Find the zero-gamma (flip) spot by repricing TOTAL net GEX on a grid.

    For each hypothetical spot S' in [spot*(1-range), spot*(1+range)] we recompute
    gamma for every contract and sum the signed dollar-GEX, then locate sign
    changes. Each bracketed crossing is refined to machine precision by bisection
    (the net-GEX curve is smooth but can be near-discontinuous for 0DTE,
    where linear interpolation across a wide grid cell is a poor model). Returns
    the crossing nearest the current spot as 'flip' (None if there is no crossing
    in range), plus all 'crossings' and the ('grid','curve') for plotting/debugging.

    Exact-zero grid nodes are NOT treated as crossings on their own: far from the
    strikes, 0DTE gamma underflows to a literal 0.0, which would otherwise report
    dozens of phantom "crossings" in the flat wings. A node is only a crossing if
    it is a genuine sign change relative to its nearest non-zero neighbours.
    """
    price_range = cfg.price_range if price_range is None else price_range
    steps = cfg.steps if steps is None else steps

    K, T, iv, oi, sign, iscall = _to_arrays(contracts, convention)
    grid = np.linspace(spot * (1.0 - price_range), spot * (1.0 + price_range), steps)
    curve = np.empty_like(grid)
    for i, S in enumerate(grid):
        curve[i] = _total_net_gex_at(S, K, T, iv, oi, sign, cfg)

    def f(S):
        return _total_net_gex_at(S, K, T, iv, oi, sign, cfg)

    # Sign changes between consecutive NON-ZERO nodes; exact zeros (underflow in
    # the flat wings) are skipped so they never register as phantom crossings.
    nz = np.flatnonzero(curve)
    s = np.sign(curve[nz])
    crossings = [_refine_root(f, grid[nz[j]], grid[nz[j + 1]])
                 for j in np.flatnonzero(s[:-1] != s[1:])]

    crossings = np.array(sorted(set(crossings)), dtype=float)
    nearest = None
    if crossings.size:
        nearest = float(crossings[np.argmin(np.abs(crossings - spot))])

    return {
        "flip": nearest,
        "crossings": crossings,
        "grid": grid,
        "curve": curve,
        # Evaluated EXACTLY at spot (not the nearest grid node) so it always
        # agrees in sign and magnitude with the per-strike profile total.
        "total_at_spot": f(spot),
    }


def _flip_with_put_sign(contracts, spot, cfg, put_sign):
    """Helper for the graded put-sign sensitivity band: flip at a scaled put sign."""
    conv = DealerConvention(cfg.convention.call_sign, put_sign, "scaled")
    return find_flip_level(contracts, spot, conv, cfg)["flip"]


def _decayed_contracts(contracts, decay_seconds):
    """Return copies of `contracts` with time-to-expiry advanced by decay_seconds.

    Used for the flip time-decay projection: the flip is not static -- as T
    decays, ATM gamma (~1/sqrt(T)) grows and the zero-gamma level migrates. We
    recompute each contract's T as of a later wall-clock moment (e.g. the 16:00
    ET close) holding K, sigma, OI fixed, drop any that expire in the interval,
    and floor the survivors at the same T_FLOOR used for the live snapshot so
    the projection is numerically consistent with the live number.
    """
    year_s = DAY_COUNT * 24.0 * 3600.0
    return [replace(c, T=max(secs, T_FLOOR_SECONDS) / year_s)
            for c in contracts
            if (secs := c.T * year_s - decay_seconds) > 0]


def flip_time_decay(contracts, spot, cfg, now):
    """Project where the gamma flip migrates by the 16:00 ET close (time decay).

    The headline flip is computed at the CURRENT T. For 0DTE-heavy chains the
    flip moves materially through the session as T -> 0 (ATM gamma ~ 1/sqrt(T)).
    This recomputes the flip with every contract's T advanced to the close,
    holding K / sigma / OI fixed. Returns a dict:
        flip_now    : flip at current T (None if no crossing)
        flip_close  : flip at close-of-day T (None if no crossing / no contracts)
        move        : flip_close - flip_now (None if either is None)
        seconds     : seconds from `now` to the 16:00 ET close (<=0 after close)
    After the close (or with no time left) `flip_close` is None and `seconds`<=0.
    """
    secs_to_close = seconds_to_expiry(now.date(), now)
    flip_now = find_flip_level(contracts, spot, cfg.convention, cfg)["flip"]
    decayed = _decayed_contracts(contracts, secs_to_close) if secs_to_close > 0 else []
    flip_close = find_flip_level(decayed, spot, cfg.convention, cfg)["flip"] if decayed else None
    move = (flip_close - flip_now) if (flip_now is not None and flip_close is not None) else None
    return {"flip_now": flip_now, "flip_close": flip_close, "move": move,
            "seconds": secs_to_close}


def find_walls(profile, spot=None):
    """Call wall = largest CALL-side gamma AT OR ABOVE spot (resistance / pin).
    Put wall  = largest PUT-side gamma AT OR BELOW spot (support / accelerant).

    TWO rules, both load-bearing:

    (1) PER SIDE, not from net GEX. Net at a strike is (call_gex - put_gex), so a
        strike with huge call OI AND huge put OI nets to ~zero and would never be
        a wall -- even though it carries enormous gross gamma and acts as a real
        pin. Each wall measures its own side's size.

    (2) ON THE CORRECT SIDE OF SPOT. The labels assert direction: a call wall is
        resistance, a put wall is support. An unrestricted argmax can put the
        "resistance" BELOW spot, where it is not resistance at all -- observed
        live: QQQ spot 717.51 with the largest call-side gamma at 700, which also
        held the largest put-side gamma, so BOTH walls reported 700. Restricting
        each side makes the reported level mean what its label says.

    When the dominant strike overall sits on the far side of spot it is still
    real information (a magnet that has already been breached), so it is reported
    separately as 'call_dominant' / 'put_dominant' rather than silently dropped.

    Sign convention within each side still applies: call GEX is positive under
    the standard convention (dealers long calls), put GEX negative (short puts).
    So the call wall is MAX call_gex and the put wall is MIN (most negative)
    put_gex. Passing spot=None keeps the unrestricted behaviour.
    """
    strikes = profile["strikes"]
    call_gex = profile["call_gex"]
    put_gex = profile["put_gex"]
    empty = {"call_wall": None, "call_wall_gex": None,
             "put_wall": None, "put_wall_gex": None,
             "call_dominant": None, "put_dominant": None}
    if strikes.size == 0:
        return empty

    # Unrestricted extremes (what the strike structure says overall).
    i_call_all = int(np.argmax(call_gex))
    i_put_all = int(np.argmin(put_gex))

    if spot is None:
        i_call, i_put = i_call_all, i_put_all
    else:
        above = np.flatnonzero(strikes >= spot)
        below = np.flatnonzero(strikes <= spot)
        # Fall back to the unrestricted extreme if a side has no strikes at all
        # (possible on a very thin chain); never invent a level.
        i_call = int(above[np.argmax(call_gex[above])]) if above.size else i_call_all
        i_put = int(below[np.argmin(put_gex[below])]) if below.size else i_put_all

    return {
        "call_wall": float(strikes[i_call]),
        "call_wall_gex": float(call_gex[i_call]),
        "put_wall": float(strikes[i_put]),
        "put_wall_gex": float(put_gex[i_put]),
        # Dominant strike ignoring the spot restriction; None when it is the wall
        # itself, so callers only see it when it adds information.
        "call_dominant": (float(strikes[i_call_all])
                          if i_call_all != i_call else None),
        "put_dominant": (float(strikes[i_put_all])
                         if i_put_all != i_put else None),
    }


# ===========================================================================
# Time-to-expiry helpers
# ===========================================================================
ET = ZoneInfo("America/New_York")


def now_et():
    return datetime.now(tz=ET)


def seconds_to_expiry(expiry, now):
    """Seconds from `now` to 16:00 ET on the expiration date (can be negative)."""
    expiry_dt = datetime(expiry.year, expiry.month, expiry.day,
                         EXPIRY_HOUR_ET, 0, 0, tzinfo=ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
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
# Charles Schwab Trader API -- Market Data (option chains) via the schwab_hub
# ===========================================================================
# The hub (../schwab_hub/run.sh) owns the app credentials and the single token, so
# nothing here logs in or refreshes. One get_option_chain() call returns the whole
# chain plus the underlying spot, OI and IV; Schwab market data is free.

SCHWAB_INDEX_SYMBOLS = {  # cash indices take a "$" prefix on Schwab
    "SPX": "$SPX", "NDX": "$NDX", "RUT": "$RUT", "VIX": "$VIX", "DJI": "$DJI",
}


# Futures roots that COLLIDE with unrelated equity tickers on this endpoint.
# Verified live: --ticker ES returns EVERSOURCE ENERGY (~$71), not S&P futures,
# and computes a confident, meaningless gamma profile from the wrong instrument.
# A slash-prefixed futures symbol (/ES) is rejected outright by /chains with a
# 400, so futures options are simply not available from this data source.
FUTURES_ROOT_COLLISIONS = {
    "ES": "S&P 500 futures (Schwab returns EVERSOURCE ENERGY equity)",
    "NQ": "Nasdaq-100 futures", "RTY": "Russell 2000 futures",
    "YM": "Dow futures", "CL": "Crude oil futures (returns COLGATE equity)",
    "GC": "Gold futures", "ZB": "T-bond futures", "ZN": "10-year note futures",
    "MES": "Micro S&P futures", "MNQ": "Micro Nasdaq futures",
}


def check_futures_symbol(ticker):
    """Warn (or refuse) when a ticker looks like a futures request.

    Returns an error string for slash-prefixed futures, a warning string for an
    equity ticker that collides with a futures root, else None.
    """
    t = ticker.upper().strip()
    if t.startswith("/"):
        return ("ERROR", "{} is a FUTURES symbol. Schwab's /chains endpoint does not "
                         "serve futures options (it returns HTTP 400), so futures GEX "
                         "is not computable from this data source.".format(t))
    if t in FUTURES_ROOT_COLLISIONS:
        return ("WARNING", "'{}' is an EQUITY ticker here, not {}. Schwab has no "
                           "futures options on this endpoint -- you will get a gamma "
                           "profile for the wrong instrument.".format(
                               t, FUTURES_ROOT_COLLISIONS[t]))
    return None


def to_schwab_symbol(ticker):
    """Map a friendly ticker to Schwab's symbol ('SPX' -> '$SPX'; 'SPY' stays 'SPY')."""
    t = ticker.upper().strip()
    if t.startswith("$"):
        return t
    return SCHWAB_INDEX_SYMBOLS.get(t, t)


def get_schwab_client(app_key=None, app_secret=None, token_path=None):
    """Client for Schwab market data, served by the central schwab_hub.

    The hub owns the credentials and the single token, so the arguments are ignored
    (kept so existing call sites still work). Raises RuntimeError if the client
    package is missing: `pip install -e ../schwab_hub`.
    """
    try:
        from schwab_hub_client import HubClient
    except ImportError as exc:
        raise RuntimeError(
            "schwab_hub_client is required for live data: pip install -e ../schwab_hub "
            "(and start the hub with ../schwab_hub/run.sh).") from exc
    return HubClient()


def fetch_chain_schwab(client, symbol, from_date=None, to_date=None, strike_count=None,
                       max_retries=3, retry_wait=5.0):
    """One schwab-py get_option_chain() call -> full chain JSON (spot + OI + IV).

    `client` is a schwab-py client (see get_schwab_client). `from_date`/`to_date`
    are datetime.date objects. contractType defaults to ALL server-side, so both
    callExpDateMap and putExpDateMap come back in one un-paginated payload.

    TRANSIENT failures (connection resets, read timeouts, 5xx) are retried up to
    `max_retries` times with linear backoff -- scheduled morning runs were dying
    on single network hiccups. NON-transient failures (4xx, OAuth/auth errors)
    are raised immediately: retrying an expired refresh token cannot help, only
    a re-login can (../schwab_hub/run.sh login).
    """
    kwargs = {"include_underlying_quote": True, "from_date": from_date,
              "to_date": to_date, "strike_count": strike_count}
    last = None
    for attempt in range(max_retries):
        try:
            resp = client.get_option_chain(symbol, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            # Auth errors: raise now (needs re-login, not a retry).
            if "OAuth" in type(e).__name__ or "token" in str(e).lower():
                raise
            # HTTP 4xx: client-side problem, retrying cannot heal it.
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code is not None and 400 <= code < 500:
                raise
            last = e
            if attempt < max_retries - 1:
                time.sleep(retry_wait * (attempt + 1))   # e.g. 5s, then 10s
    raise last


def _f(x):
    """float(x), or None when x is missing or not numeric."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_schwab_chain(data):
    """Parse a Schwab /chains response into (contracts, spot, ts_ns, dropped, status).

    Schwab specifics handled here:
      * callExpDateMap / putExpDateMap are keyed "YYYY-MM-DD:DTE" -> strike -> [opt].
        We take the expiration date from the map key (most reliable).
      * `volatility` is a PERCENT (e.g. 18.42) and uses -999.0 / non-finite as the
        "no IV" sentinel -> convert to a decimal and drop sentinels.
      * spot comes straight from `underlyingPrice` (fallback underlying.mark/last).
      * Skips contracts with no/zero OI or no/sentinel IV, counting the drops.
      * Quote-quality filters: crossed quotes (bid > ask) dropped anywhere;
        DEEP-ITM contracts (depth > ITM_DEPTH_FILTER) with no bid or a relative
        spread > MAX_ITM_REL_SPREAD dropped (their IV is noise -- garbage IV in,
        garbage gamma out). Deep-OTM wings are never quote-filtered: zero-bid
        wings still carry real tail gamma, and per-strike IV must be preserved.
        Contracts with no bid/ask fields at all skip the filter (can't judge).
    """
    contracts = []
    dropped = {"no_oi": 0, "no_iv": 0, "malformed": 0, "crossed": 0, "itm_bad_quote": 0}
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

                    # ---- quote-quality filters (see docstring) ----
                    bidf, askf = _f(o.get("bid")), _f(o.get("ask"))
                    # Crossed market (bid > ask) is stale/locked data anywhere.
                    # The ask>0 guard previously let a crossed-to-ZERO quote
                    # (bid > ask == 0, common pre-open) slip through; bid > ask
                    # is crossed regardless of whether ask is positive.
                    if bidf is not None and askf is not None and bidf > askf:
                        dropped["crossed"] += 1          # crossed market: stale data
                        continue
                    if spot and (bidf is not None or askf is not None):
                        deep_itm = ((cp == "call" and strike < spot * (1 - ITM_DEPTH_FILTER))
                                    or (cp == "put" and strike > spot * (1 + ITM_DEPTH_FILTER)))
                        if deep_itm:
                            bad = bidf is None or bidf <= 0    # no bid on a deep-ITM = stale
                            if not bad and askf is not None and askf > bidf:
                                mid = 0.5 * (askf + bidf)
                                bad = (askf - bidf) / mid > MAX_ITM_REL_SPREAD
                            if bad:
                                dropped["itm_bad_quote"] += 1
                                continue

                    contracts.append(Contract(strike, exp_date, cp,
                                              float(oi), ivf / 100.0,
                                              volume=_f(o.get("totalVolume")) or 0.0,
                                              bid=bidf, ask=askf, last=_f(o.get("last"))))
    return contracts, spot, ts_ns, dropped, status


# ---------------------------------------------------------------------------
# Chain persistence (build your own open-interest history)
# ---------------------------------------------------------------------------
# Open interest is published once daily and is NEVER backfillable: Schwab serves
# only the current snapshot, so a day not captured is a day lost forever. Saving
# every live fetch accumulates a private time series that supports:
#   * dOI per strike per day -- where positioning is actually BUILDING vs. stale
#     OI that has sat unchanged for weeks (the headline number cannot tell you).
#   * A crude aggressor read: OI rising while prints sit near the ask suggests
#     customers bought / dealers sold, i.e. dealers SHORT that strike's gamma.
#   * Empirical calibration of the dealer sign convention -- the assumption that
#     drives the LOW CONFIDENCE warning -- against realized outcomes.
# Rows are written RAW (pre-filter, including zero-OI strikes and the -999 IV
# sentinel): the GEX filters are a modeling choice, but the archive should be a
# faithful record. Format is gzipped CSV -- stdlib only, ~10x smaller than JSON,
# and directly loadable with pandas.read_csv().
CHAIN_DIR = "chains"
CHAIN_COLUMNS = ["snapshot_ts_et", "oi_date", "ticker", "spot", "expiry", "cp",
                 "strike", "oi", "iv_pct", "bid", "ask", "last", "volume"]


def chain_snapshot_rows(data, ticker, now=None):
    """Flatten a raw Schwab chain payload into archive rows (no filtering).

    iv_pct is the vendor's PERCENT value verbatim (including the -999 'no IV'
    sentinel) so the archive loses nothing; divide by 100 for a decimal vol.
    """
    now = now_et() if now is None else now
    under = data.get("underlying") or {}
    spot = data.get("underlyingPrice")
    if spot is None:
        spot = under.get("mark") or under.get("last")
    ts = now.strftime("%Y-%m-%dT%H:%M:%S%z")
    oi_date = prior_trading_session(now.date()).isoformat()

    rows = []
    for map_key, cp in (("callExpDateMap", "call"), ("putExpDateMap", "put")):
        for exp_key, by_strike in (data.get(map_key) or {}).items():
            exp = str(exp_key).split(":")[0]
            for strike_str, opts in by_strike.items():
                for o in opts:
                    rows.append([
                        ts, oi_date, ticker, spot, exp, cp,
                        o.get("strikePrice", strike_str), o.get("openInterest"),
                        o.get("volatility"), o.get("bid"), o.get("ask"),
                        o.get("last"), o.get("totalVolume"),
                    ])
    return rows


def save_chain_snapshot(data, ticker, chain_dir=CHAIN_DIR, now=None):
    """Persist one chain fetch to chains/<TICKER>/<YYYY-MM-DD>.csv.gz.

    Same-day re-runs overwrite: a later snapshot carries more complete volume,
    and OI is a once-daily figure so nothing is lost. Never raises -- archiving
    must not break a live run.
    """
    import csv
    import gzip
    try:
        now = now_et() if now is None else now
        rows = chain_snapshot_rows(data, ticker, now=now)
        if not rows:
            return None
        d = os.path.join(chain_dir, ticker.upper().lstrip("$"))
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, now.date().isoformat() + ".csv.gz")
        with gzip.open(path, "wt", newline="") as f:
            w = csv.writer(f)
            w.writerow(CHAIN_COLUMNS)
            w.writerows(rows)
        return path
    except Exception as e:                      # archiving is best-effort
        print("  NOTE: chain snapshot not saved ({})".format(e), file=sys.stderr)
        return None


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
def compute_view(contracts, spot, cfg, now=None):
    """Run the full pipeline for one slice of the chain (0DTE / all / a date).

    `now` (defaults to now_et()) anchors the flip time-decay projection: the
    flip is recomputed at the 16:00 ET close so the output shows where the level
    migrates as T decays, not just where it sits at the snapshot instant.
    """
    if not contracts:
        return {"empty": True, "n": 0}
    now = now_et() if now is None else now
    profile = compute_gex_profile(contracts, spot, cfg.convention, cfg)
    walls = find_walls(profile, spot)
    flip_std = find_flip_level(contracts, spot, cfg.convention, cfg)
    # Spec-required literal check: flip the put sign (puts -> +). This makes every
    # contribution positive, so the flip typically VANISHES -- an honest sign that
    # the flip's existence rests on the short-put assumption.
    flipped = replace(cfg.convention, put_sign=-cfg.convention.put_sign)
    flip_flp = find_flip_level(contracts, spot, flipped, cfg)
    # Graded sensitivity: vary the short-put MAGNITUDE +/-50% so we get a real
    # "how far does it move" number (the binary flip alone never crosses zero).
    base_put = cfg.convention.put_sign
    flip_band = {}
    if base_put != 0:
        for scale in (0.5, 1.5):
            flip_band[scale] = _flip_with_put_sign(contracts, spot, cfg, base_put * scale)
    flip = flip_std["flip"]
    band_vals = [v for v in flip_band.values() if v is not None]
    band_move = max(abs(v - flip) for v in band_vals) if (flip is not None and band_vals) else None
    low_conf = flip is None or (band_move is not None and band_move > MATERIAL_FLIP_MOVE * spot)
    gross = gross_dollar_gamma(contracts, spot, cfg)
    decay = flip_time_decay(contracts, spot, cfg, now)
    return {
        "empty": False,
        "n": len(contracts),
        "profile": profile,
        "walls": walls,
        "flip_std": flip_std,
        "flip_flipped": flip_flp,
        "flip_band": flip_band,
        "band_move": band_move,
        "low_confidence": low_conf,
        "flip_decay": decay,
        "total": profile["total"],
        "gross": gross,
    }


def explain_empty_view(label, today, now=None):
    """Explain WHY a view has no contracts, instead of printing a bare '(empty)'.

    The common case is not an error: after 16:00 ET the day's 0DTE options have
    settled, so enrich_and_filter_time correctly drops them and the 0DTE view is
    legitimately empty. Saying so beats leaving the user to wonder whether the
    tool broke.
    """
    now = now_et() if now is None else now
    if "0DTE" in label.upper():
        if today.weekday() >= 5:
            return "no expiration today (weekend)"
        secs = seconds_to_expiry(today, now)
        if secs <= 0:
            ago = -secs
            h, m = int(ago // 3600), int((ago % 3600) // 60)
            return ("today's 0DTE settled at 16:00 ET ({}h {:02d}m ago) -- expired, "
                    "not missing".format(h, m))
        return "no 0DTE contracts with usable OI and IV in the chain"
    if label.upper().startswith("EXPIRY"):
        return "that expiration has settled, or has no contracts with usable OI and IV"
    return "no contracts with usable OI and IV in this slice"


def regime_word(spot, flip, total_at_spot=None):
    """Regime label. The sign of net GEX AT SPOT is the ground truth; the
    spot-vs-flip comparison is only the fallback when that sign is unavailable.

    Why not just spot vs flip: "spot above the flip = dealers long gamma" assumes
    the TYPICAL chain orientation (call gamma above, put gamma below). When the
    book is inverted -- heavy put gamma ABOVE spot, as in stressed markets -- the
    curve crosses zero with the opposite slope and spot > flip is actually SHORT
    gamma. Reading the sign of the total at spot is correct either way.
    """
    if total_at_spot is not None:
        if total_at_spot > 0:
            return "LONG gamma"
        if total_at_spot < 0:
            return "SHORT gamma"
        return "NEUTRAL (net gamma ~0 at spot)"
    if flip is None:
        return "UNDETERMINED"
    return "LONG gamma" if spot > flip else "SHORT gamma"


def interpretation_line(spot, flip, total):
    """Narrative for the current regime. `total` is the signed net GEX AT SPOT
    (flip_std['total_at_spot']), and its SIGN -- not the spot-vs-flip relation --
    decides long vs short: on an inverted chain (put gamma above spot) the curve
    crosses zero with the opposite slope and spot > flip is SHORT gamma.
    """
    if flip is None:
        if total > 0:
            return ("No zero-gamma crossing in range and total GEX is POSITIVE: "
                    "model says dealers are net long gamma throughout -> expect "
                    "vol-dampening / mean reversion. (Flip likely sits below the search window.)")
        return ("No zero-gamma crossing in range and total GEX is NEGATIVE: "
                "model says dealers are net short gamma throughout -> expect "
                "vol-amplification / trend risk. (Flip likely sits above the search window.)")
    dist = abs(spot - flip) / spot * 100.0
    side = "ABOVE" if spot > flip else "BELOW"
    if total > 0:
        return ("Net GEX at spot is POSITIVE (spot {:.2f}% {} the flip) -> dealers net "
                "LONG gamma: they sell rallies / buy dips, dampening vol. Bias: "
                "range-bound, fade extremes, watch for a pin near the call wall. "
                "Losing the flip flips the regime.").format(dist, side)
    return ("Net GEX at spot is NEGATIVE (spot {:.2f}% {} the flip) -> dealers net "
            "SHORT gamma: they buy rallies / sell dips, amplifying vol. Bias: "
            "momentum/trend, wider ranges; a break of the put wall can accelerate "
            "lower. Reclaiming the flip calms it.").format(dist, side)


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
    print("  IV handling ......... PER-STRIKE vendor IV; the smile is never flattened to ATM")
    print("  Exercise style ...... European (BSM) gamma for ALL contracts; American early-")
    print("                        exercise premium (SPY/QQQ) ignored -- small, short-dated")
    print("  Time clock .......... CALENDAR ACT/365 (matches the vendor IV clock; trading-")
    print("                        time T would break the sigma^2*T pairing)")
    print("  Quote filters ....... crossed dropped; deep-ITM (>{:.0%}) with no bid or rel.".format(ITM_DEPTH_FILTER))
    print("                        spread >{:.0%} dropped; OTM wings NEVER quote-filtered".format(MAX_ITM_REL_SPREAD))
    print("  Dealer convention ... {}".format(cfg.convention.label))
    print("                        call_sign={:+.0f}  put_sign={:+.0f}  (flippable)"
          .format(cfg.convention.call_sign, cfg.convention.put_sign))
    print("  Risk-free rate r .... {:.4f}{}".format(
        cfg.rate, "   <-- DEFAULT; set --rate to your current value" if rate_is_default else ""))
    print("  Dividend yield q .... {:.4f}   (source: {}; override with --div-yield)"
          .format(cfg.div_yield, cfg.div_src))
    print("  Multiplier .......... {}".format(cfg.multiplier))
    print("  Day count ........... ACT/{:.0f}, time-to-expiry to {:02d}:00 ET (PM settle)"
          .format(DAY_COUNT, EXPIRY_HOUR_ET))
    print("  Flip search ......... total net GEX repriced over +/-{:.0%} in {} steps"
          .format(cfg.price_range, cfg.steps))
    print("                        (sticky-strike: per-strike IV held fixed across the grid)")
    print("  GEX formula ......... gamma * OI * {} * spot^2 * 0.01   ($ per 1% move)"
          .format(cfg.multiplier))
    print("  Unit warning ........ per-1% convention; per-POINT = value / (0.01 * spot).")
    print("                        (SqueezeMetrics publishes per-point -- do not compare raw.)")
    print("  Net GEX ............. SUM(call GEX) - SUM(put GEX) under the convention above")
    print()


def print_data_health(spot, ts_ns, dropped, dropped_expired, floored, n_kept, today, prior_session):
    print("-" * 78)
    print("DATA HEALTH & STALENESS")
    print("-" * 78)
    print("  Spot used ........... {}".format(fmt_px(spot)))
    if ts_ns:
        try:
            t = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).astimezone(ET)
            age = (now_et() - t).total_seconds()
            print("  Underlying snapshot . {} ET  (age {:.0f}s)".format(t.strftime("%Y-%m-%d %H:%M:%S"), age))
        except Exception:
            print("  Underlying snapshot . {} (ns)".format(ts_ns))
    print("  Contracts used ...... {}".format(n_kept))
    print("  Dropped: no OI={}  no IV={}  crossed={}  deep-ITM bad quote={}  malformed={}  expired={}".format(
        dropped["no_oi"], dropped["no_iv"], dropped.get("crossed", 0),
        dropped.get("itm_bad_quote", 0), dropped["malformed"], dropped_expired))
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


def print_side_by_side(views, today=None):
    """Totals table across the computed views (e.g. 0DTE vs ALL).

    Empty columns print a short reason (e.g. 'expired 16:00') rather than a bare
    '(empty)', with the full explanation footnoted under the table.
    """
    labels = [lbl for lbl, _ in views]
    print("-" * 78)
    print("NET DEALER GEX  ($ per 1% move)   [#1 KEY OUTPUT]")
    print("-" * 78)
    header = "  {:<26}".format("") + "".join("{:>22}".format(l) for l in labels)
    print(header)

    def row(name, fn):
        cells = ""
        for lbl, v in views:
            cells += "{:>22}".format("(none)" if v.get("empty") else fn(v))
        print("  {:<26}{}".format(name, cells))

    row("Total net GEX", lambda v: fmt_bn(v["total"]))
    row("Total net GEX ($)", lambda v: fmt_usd(v["total"]))
    row("Gross |gamma| ($/1%)", lambda v: fmt_bn(v["gross"]))
    row("Contracts", lambda v: str(v["n"]))
    if today is not None:
        for lbl, v in views:
            if v.get("empty"):
                print("  * {}: {}".format(lbl, explain_empty_view(lbl, today)))
    print()


def cross_quote(ticker, value, spy_ratio):
    """Cross-quote a level between SPX and SPY. Returns (other_label, value) or None."""
    t = ticker.upper().lstrip("$")
    if t == "SPX":
        return ("SPY", value / spy_ratio)   # SPX -> SPY (divide by ~10)
    if t == "SPY":
        return ("SPX", value * spy_ratio)   # SPY -> SPX (multiply by ~10)
    return None


def print_view_detail(label, view, spot, spy_ratio, cfg, today):
    print("-" * 78)
    print("VIEW: {}".format(label))
    print("-" * 78)
    if view.get("empty"):
        print("  No levels: {}.".format(explain_empty_view(label, today)))
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
        print("    {}  {:,.2f}   (as of NOW)".format(cfg.ticker, flip))
        eq = cross_quote(cfg.ticker, flip, spy_ratio)
        if eq:
            print("    {}-equiv  {:,.2f}   (SPX/SPY ratio {:.3f})".format(eq[0], eq[1], spy_ratio))
        if view["flip_std"]["crossings"].size > 1:
            extra = ", ".join("{:,.0f}".format(c) for c in view["flip_std"]["crossings"])
            print("    NOTE: {} crossings in range [{}]; reporting the one nearest spot."
                  .format(view["flip_std"]["crossings"].size, extra))

    # ---- Flip time-decay projection (#2a): the flip is NOT static ----
    decay = view.get("flip_decay") or {}
    fc, mv, secs = decay.get("flip_close"), decay.get("move"), decay.get("seconds", 0)
    if secs and secs > 0:
        h, m = int(secs // 3600), int((secs % 3600) // 60)
        if fc is not None and mv is not None:
            print("    Flip at 16:00 close (T-decayed): {:,.2f}   (migrates {:+.2f}, {:+.2f}%, in {}h{:02d}m)"
                  .format(fc, mv, mv / spot * 100.0, h, m))
            print("      => the flip is NOT static: it drifts as T decays; plan around the path,")
            print("         not just the snapshot level.")
        elif flip is not None:
            print("    Flip at 16:00 close (T-decayed): no crossing by the close (decays away).")
    # After the close there is no decay projection to show (0DTE has settled).

    # 0DTE OI-staleness escalation, adjacent to the flip it undermines.
    if _is_0dte_label(label) and flip is not None:
        print("    *** 0DTE STALENESS: flip built on LAST NIGHT's OI; today's intraday 0DTE")
        print("        positioning (often most of the day's gamma) is NOT reflected. ***")

    # ---- Walls (#3, #4): per-side (call wall from call gamma, put wall from put gamma) ----
    print("  Call wall [#3] (resistance/pin): {} {}   call-side GEX {}".format(
        cfg.ticker, fmt_px(walls["call_wall"]), fmt_bn(walls["call_wall_gex"])))
    print("  Put wall  [#4] (support/accel):  {} {}   put-side GEX {}".format(
        cfg.ticker, fmt_px(walls["put_wall"]), fmt_bn(walls["put_wall_gex"])))
    # A dominant strike on the far side of spot is a magnet that has ALREADY been
    # breached -- real information, but not resistance/support, so it is named
    # separately rather than reported as the wall.
    if walls.get("call_dominant") is not None:
        print("    note: largest call-side gamma overall sits at {} (below spot -- "
              "breached magnet, not resistance)".format(fmt_px(walls["call_dominant"])))
    if walls.get("put_dominant") is not None:
        print("    note: largest put-side gamma overall sits at {} (above spot -- "
              "breached magnet, not support)".format(fmt_px(walls["put_dominant"])))

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

    move = view["band_move"]
    if move is not None:
        allv = [v for v in band.values() if v is not None] + [flip]
        print("    Short-put magnitude +/-50% ................. flip in [{:,.2f} .. {:,.2f}]"
              .format(min(allv), max(allv)))
        print("                                                 max move {:,.2f} SPX = {:.2f}% of spot"
              .format(move, move / spot * 100.0))

    if view["low_confidence"]:
        print("    *** LOW CONFIDENCE: the flip level is materially sensitive to the")
        print("        (assumed) dealer put positioning. Treat the regime as directional")
        print("        context, not a precise level. ***")
    elif move is not None:
        print("    OK: flip is robust to a +/-50% change in the short-put magnitude")
        print("        (< {:.0%} of spot), though it still hinges on dealers being short puts."
              .format(MATERIAL_FLIP_MOVE))
    print()


def _is_0dte_label(label):
    return "0DTE" in label.upper()


def render_summary(label, view, spot, spy_ratio, cfg, today):
    """The required plain-text bias block (#6)."""
    print("#" * 78)
    print("# BIAS SUMMARY  --  {}".format(label))
    print("#" * 78)
    if view.get("empty"):
        print("  No bias: {}.".format(explain_empty_view(label, today)))
        if _is_0dte_label(label):
            print("  Use the ALL EXPIRIES view for tonight; fresh 0DTE appears after tomorrow's open.")
        print("#" * 78)
        print()
        return
    flip = view["flip_std"]["flip"]
    walls = view["walls"]
    total_at_spot = view["flip_std"].get("total_at_spot")
    reg = regime_word(spot, flip, total_at_spot)
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
    print("  Interpretation .... " + interpretation_line(spot, flip, total_at_spot))
    # 0DTE OI-staleness escalation: the 0DTE flip is built on end-of-prior-session
    # OI that EXCLUDES everything opened intraday today -- on a big 0DTE day that
    # is most of the gamma. Surface this at the headline level, not just in the
    # data-health block, so the precise-looking flip number is not over-trusted.
    if _is_0dte_label(label) and flip is not None:
        print("  *** 0DTE CAVEAT ... this flip uses LAST NIGHT's OI; today's intraday")
        print("      0DTE positioning (often most of the day's gamma) is NOT in it.")
        print("      Treat the 0DTE flip/regime as a stale lower bound, not a live level.")
    print("#" * 78)
    print()


# ===========================================================================
# Plot
# ===========================================================================
def plot_profile(label, view, spot, flip, walls, ticker, outpath,
                 window_frac=PLOT_WINDOW_FRAC):
    """Per-strike net-GEX bar chart with spot / flip / walls marked. Saved to file."""
    import matplotlib
    matplotlib.use("Agg")  # headless / no display needed
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator

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

    ax.xaxis.set_major_locator(MaxNLocator(nbins=25, steps=[1, 2, 2.5, 5, 10]))
    ax.tick_params(axis="x", labelrotation=45)

    ax.set_title("{} dealer GEX profile - {}  (green=long gamma, red=short gamma)"
                 .format(ticker, label))
    ax.set_xlabel("strike / price level")
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
def _norm_cdf(x):
    """Standard normal CDF via math.erf (stdlib; scipy is not a dependency)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S, K, T, sigma, r=0.0, q=0.0, cp="call"):
    """Black-Scholes-Merton option price. Used to give the offline demo chain
    realistic quotes so the implied-move path is exercisable without network."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(0.0, (S - K) if cp == "call" else (K - S))
    vt = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / vt
    d2 = d1 - vt
    df_q, df_r = math.exp(-q * T), math.exp(-r * T)
    if cp == "call":
        return S * df_q * _norm_cdf(d1) - K * df_r * _norm_cdf(d2)
    return K * df_r * _norm_cdf(-d2) - S * df_q * _norm_cdf(-d1)


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
            # Approximate T so the synthetic quotes are realistic; a 1-cent-wide
            # market keeps mid == theoretical value.
            dte = max((exp - today).days, 0)
            T = max((dte + 0.25) / DAY_COUNT, 1.0 / DAY_COUNT / 24)
            for cp, oi in (("call", call_oi), ("put", put_oi)):
                px = bs_price(spot, float(K), T, iv, cp=cp)
                contracts.append(Contract(
                    float(K), exp, cp, round(oi * scale), iv,
                    bid=max(0.0, px - 0.05), ask=px + 0.05, last=px))
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
                   help="'0dte' | 'week' | 'all' | YYYY-MM-DD. 'week' pools all expiries in the "
                        "current Mon-Fri trading week (next week's if run on a weekend). "
                        "Default: compute BOTH 0DTE and all expiries.")
    p.add_argument("--rate", type=float, default=DEFAULT_RATE, help="risk-free rate (annual, decimal).")
    p.add_argument("--div-yield", type=float, default=None,
                   help="dividend yield (annual, decimal). Default: built-in per-ticker map "
                        "(SPY 0.012, QQQ 0.006, ...), else 0.")
    p.add_argument("--multiplier", type=int, default=DEFAULT_MULTIPLIER, help="contract multiplier.")
    p.add_argument("--price-range", type=float, default=DEFAULT_PRICE_RANGE, help="+/- fraction for flip search.")
    p.add_argument("--steps", type=int, default=DEFAULT_GRID_STEPS, help="grid steps for flip search.")
    p.add_argument("--put-sign", type=float, default=None, help="override dealer put sign (+1/-1).")
    p.add_argument("--all-days", type=int, default=45,
                   help="for 'all'/default: fetch expiries from today out this many days. "
                        "Kept modest because Schwab 502s on very large chains (e.g. a full "
                        "year of SPX); raise it for more coverage at the risk of a 502.")
    p.add_argument("--out-prefix", default=None, help="output chart filename prefix.")
    p.add_argument("--no-plot", action="store_true", help="skip chart generation.")
    p.add_argument("--expiry-risk", nargs="?", const="auto", default=None, metavar="DATE",
                   help="gamma roll-off (profile with and WITHOUT contracts expiring through "
                        "DATE), charm/vanna hedging flow into the close, and pin candidates. "
                        "DATE defaults to this week's Friday.")
    p.add_argument("--profiles", action="store_true",
                   help="show the INTRADAY (front-week + next OpEx, OI blended with "
                        "today's volume) and STRUCTURAL (all expiries, OI only) profiles.")
    p.add_argument("--levels-only", action="store_true",
                   help="print only a compact levels block (for notifications / quick pulls).")
    p.add_argument("--no-save-chain", action="store_true",
                   help="do NOT archive this chain fetch. Archiving is ON by default: open "
                        "interest is never backfillable, so an unsaved day is lost forever.")
    p.add_argument("--chain-dir", default=os.environ.get("GEX_CHAIN_DIR", CHAIN_DIR),
                   help="directory for the chain archive.")
    p.add_argument("--demo", action="store_true", help="run on an offline synthetic chain (no credentials).")
    return p.parse_args(argv)


def div_yield_for(ticker, override=None):
    """(q, source): explicit override wins; else the built-in per-ticker map; else 0."""
    base = ticker.upper().lstrip("$")
    if override is not None:
        return override, "explicit override"
    if base in TICKER_DIV_YIELDS:
        return TICKER_DIV_YIELDS[base], "built-in map for {} (approx.)".format(base)
    return 0.0, "default 0 (no map entry for {})".format(base)


def build_config(args):
    conv = CONV_STANDARD
    if args.put_sign is not None:
        conv = DealerConvention(conv.call_sign, args.put_sign,
                                "custom (put_sign={:+.0f})".format(args.put_sign))
    q, q_src = div_yield_for(args.ticker, args.div_yield)
    return Config(ticker=args.ticker, multiplier=args.multiplier, rate=args.rate,
                  div_yield=q, div_src=q_src, price_range=args.price_range, steps=args.steps,
                  convention=conv)


def prior_trading_session(today):
    """Most recent weekday strictly before `today` (holidays not modeled)."""
    d = today - timedelta(days=1)
    while d.weekday() >= 5:  # Sat=5, Sun=6
        d -= timedelta(days=1)
    return d


def third_friday(year, month):
    """Third Friday of the month (monthly OpEx; triple witching in Mar/Jun/Sep/Dec)."""
    d = date(year, month, 15)                 # third Friday falls on the 15th..21st
    return d + timedelta(days=(4 - d.weekday()) % 7)


def next_monthly_opex(today):
    """Next monthly OpEx (3rd Friday) on or after `today`."""
    tf = third_friday(today.year, today.month)
    if today > tf:
        y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        tf = third_friday(y, m)
    return tf


def gamma_expiry_buckets(contracts, spot, cfg, today):
    """Decompose gamma by expiry bucket -- weight by HEDGING URGENCY, not magnitude.

    The governing rule: match the expiry set to the holding period. An all-expiry
    total mixes 0DTE gamma (rehedged hour-by-hour, gone at 16:00) with OI months
    out that will NOT be rebalanced today. First-match bucket assignment.
    Returns [{"label", "gross", "share", "net"}, ...] (dollars, share of gross).
    """
    opex = next_monthly_opex(today)
    buckets = [
        ("0DTE (evaporates 16:00 ET)", lambda d: d == today),
        ("<= 1 week",                  lambda d: d <= today + timedelta(days=7)),
        ("<= monthly OpEx {}".format(opex.strftime("%b %d")), lambda d: d <= opex),
        ("beyond OpEx (slow money)",   lambda d: True),
    ]
    K, T, iv, oi, sign, iscall = _to_arrays(contracts, cfg.convention)
    signed = _signed_dollar_gex(K, T, iv, oi, sign, spot, cfg)
    gross_total = float(np.abs(signed).sum())

    idx = np.empty(len(contracts), dtype=int)
    for i, c in enumerate(contracts):
        for b, (_, match) in enumerate(buckets):
            if match(c.expiry):
                idx[i] = b
                break

    out = []
    for b, (label, _) in enumerate(buckets):
        m = idx == b
        gross_b = float(np.abs(signed[m]).sum())
        out.append({
            "label": label,
            "gross": gross_b,
            "share": gross_b / gross_total if gross_total > 0 else 0.0,
            "net": float(signed[m].sum()),
        })
    return out


def print_implied_move(all_contracts, spot, cfg, today, spy_ratio=None):
    """Render the ATM straddle implied move for the 0DTE (or nearest) expiry."""
    exp = today
    zero = [c for c in all_contracts if c.expiry == today]
    label = "0DTE"
    if not zero:
        # After 16:00 ET today's contracts are settled and filtered out; fall back
        # to the nearest live expiry rather than printing nothing, and say so.
        future = sorted({c.expiry for c in all_contracts})
        if not future:
            return None
        exp = future[0]
        label = "nearest expiry {} ({}DTE)".format(exp.isoformat(), (exp - today).days)
    m = atm_straddle_move(all_contracts, spot, expiry=exp)
    if not m:
        return None

    print("-" * 78)
    print("IMPLIED MOVE  (ATM straddle -- the market's own priced-in move)")
    print("-" * 78)
    print("  basis ........... {} | ATM strike {:g} ({:+.2f} from spot {:.2f})".format(
        label, m["strike"], m["distance_from_spot"], spot))
    print("  ATM call {:>8.2f}  + ATM put {:>8.2f}  = straddle {:>8.2f}".format(
        m["call_px"], m["put_px"], m["straddle"]))
    print()
    print("  Breakeven / expected |move|:  +/- {:.2f}%  ({:.2f} {} pts)".format(
        m["pct"] * 100.0, m["points"], cfg.ticker))
    print("  1-SD equivalent ...........:  +/- {:.2f}%  ({:.2f} pts)   <-- compare to VIX/16".format(
        m["sd_pct"] * 100.0, m["sd_points"]))
    if m["iv_sd_pct"]:
        print("  ATM IV cross-check ........:  +/- {:.2f}%   (IV {:.1f}% x sqrt(T))".format(
            m["iv_sd_pct"] * 100.0, m["iv_atm"] * 100.0))
        # The straddle and the vendor IV should imply the SAME 1-SD move. They
        # diverge MECHANICALLY outside regular hours: the quote is frozen at the
        # last close while T keeps counting down, and implied vol = move / sqrt(T)
        # rises as the denominator shrinks. Verified live on a Sunday: straddle
        # 3.40 on SPY 765.72 backed out 10.4% against a 1.04-day T, but 6.1%
        # against Friday's 3.00-day T -- matching the vendor's 6.5%. Same price,
        # same vendor IV, no stale quote (spread was 0.7% wide, mid == last).
        ratio = m["sd_pct"] / m["iv_sd_pct"] if m["iv_sd_pct"] > 0 else None
        if ratio and (ratio > 1.25 or ratio < 0.80):
            straddle_vol = (m["sd_pct"] / math.sqrt(m["T"])) if m.get("T") else 0.0
            print("    NOTE: quote-implied vol {:.1f}% vs vendor IV {:.1f}% "
                  "({:.0f}% apart).".format(straddle_vol * 100.0,
                                            m["iv_atm"] * 100.0, abs(ratio - 1) * 100.0))
            if not is_rth():
                print("    Expected outside regular hours: the quote is FROZEN at the last")
                print("    close while T keeps decaying, so straddle vol drifts up on a")
                print("    stale numerator. It resolves at the open.")
                print("    The move figures above are price ratios and are NOT affected;")
                print("    only this IV cross-check line is.")
            else:
                print("    During RTH this is a genuine data-quality flag: suspect a stale")
                print("    or wide ATM quote, or a vendor IV on a different day-count.")
                print("    Trust the straddle (a live two-sided market) over the IV field.")
    # Express in SPX terms when running the SPY proxy, since the rule of thumb
    # people compare against is quoted on the index.
    eq = cross_quote(cfg.ticker, spot, spy_ratio) if spy_ratio else None
    if eq:
        print("  In {} terms ..............:  +/- {:.0f} pts breakeven, "
              "+/- {:.0f} pts 1-SD (level {:,.0f})".format(
                  eq[0], m["pct"] * eq[1], m["sd_pct"] * eq[1], eq[1]))
    print()
    print("  The straddle is what you PAY to own the move, so straddle/spot is the")
    print("  breakeven (= expected absolute move). VIX/16 estimates a 1-STANDARD-")
    print("  DEVIATION move, which is the larger figure above (x{:.4f}); comparing"
          .format(STRADDLE_TO_SD))
    print("  the breakeven directly to VIX/16 understates the band by ~20%.")
    print()
    return m


# ---------------------------------------------------------------------------
# Hedging flow: translate dollar gamma into the venue where it is EXECUTED
# ---------------------------------------------------------------------------
# Dealers hedging index gamma do not buy 500 stocks -- they trade ES futures
# (deepest book, cheapest execution, best margin), SPY shares for smaller clips.
# So GEX expressed in dollars understates what it means operationally. Converting
# to CONTRACTS makes the number physical: "a 1% move forces dealers to trade N ES
# contracts", which can then be compared to what ES actually trades in a day.
#
#     contracts per 1% move = GEX_dollars / (multiplier * reference price)
#
# ES ~ SPX to within the carry basis (<1%), which is immaterial for a flow
# estimate, so the index level is used directly rather than an ES quote -- this
# data source carries no futures prices at all (verified: /ES returns EVERSOURCE
# ENERGY on both the chains and quotes endpoints; /MES and /ESZ26 404).
HEDGE_VENUES = {
    # multiplier, label, typical daily volume for a sense of scale
    "ES":  (50.0, "ES futures", 1_500_000),
    "SPY": (1.0, "SPY shares", 75_000_000),
}


def hedging_flow(gex_dollars, index_level, venue="ES"):
    """Contracts (or shares) dealers must trade per 1% move to stay hedged.

    gex_dollars is signed dollar GEX per 1% move; index_level is the S&P level
    (ES-equivalent) for futures venues, or the ETF price for SPY shares.
    """
    if venue not in HEDGE_VENUES or not index_level:
        return None
    mult, label, adv = HEDGE_VENUES[venue]
    notional = mult * index_level
    if notional <= 0:
        return None
    contracts = abs(gex_dollars) / notional
    return {
        "venue": venue, "label": label, "multiplier": mult,
        "notional_per_unit": notional,
        "contracts": contracts,
        "adv": adv,
        "pct_of_adv": contracts / adv if adv else None,
        # Short gamma => dealers trade WITH the move (destabilizing); long gamma
        # => against it (stabilizing). The direction is what matters for impact.
        "direction": ("SELL into a drop / BUY into a rally (amplifying)"
                      if gex_dollars < 0 else
                      "BUY a drop / SELL a rally (dampening)"),
    }


def print_hedging_flow(view, spot, cfg, spy_ratio=None, venues=("ES", "SPY")):
    """Show the dealer hedging requirement in the venue where it executes."""
    if view.get("empty"):
        return None
    total = view["total"]
    # Futures/index venues are priced off the S&P level; SPY shares off SPY.
    base = cfg.ticker.upper().lstrip("$")
    if base == "SPX":
        index_level, spy_px = spot, (spot / spy_ratio if spy_ratio else None)
    else:
        eq = cross_quote(cfg.ticker, spot, spy_ratio) if spy_ratio else None
        index_level = eq[1] if (eq and eq[0] == "SPX") else None
        spy_px = spot if base == "SPY" else None

    print("-" * 78)
    print("HEDGING FLOW  (where this gamma is actually EXECUTED)")
    print("-" * 78)
    print("  Dealers hedge index gamma in ES futures, not in 500 single stocks.")
    print("  Net GEX {} per 1% move means, per 1% move, dealers must trade:".format(
        fmt_bn(total)))
    print()
    shown = False
    for v in venues:
        ref = spy_px if v == "SPY" else index_level
        h = hedging_flow(total, ref, v)
        if not h:
            continue
        shown = True
        line = "  {:<18} {:>12,.0f} {}".format(
            h["label"], h["contracts"], "shares" if v == "SPY" else "contracts")
        if h["pct_of_adv"] is not None:
            line += "   ({:.2f}% of a typical day)".format(h["pct_of_adv"] * 100)
        print(line)
    if not shown:
        print("  (no S&P reference level for this ticker)")
        print()
        return None
    print()
    print("  Direction: {}".format(hedging_flow(total, index_level or spy_px,
                                                "ES")["direction"]))
    print("  This is the flow your levels describe -- it lands in ES, and index")
    print("  arbitrage carries it into SPX cash and SPY. Scope caveat: computed")
    print("  from ONE underlying's chain, so it understates the full complex.")
    print()
    return True


# ---------------------------------------------------------------------------
# Expiry mechanics: roll-off, charm/vanna into the close, pinning
# ---------------------------------------------------------------------------
def is_quarterly_opex(d):
    """Third Friday of Mar/Jun/Sep/Dec -- triple witching."""
    return d.month in (3, 6, 9, 12) and d == third_friday(d.year, d.month)


def expiry_mechanics(day, ticker="SPY"):
    """Settlement / rebalance gotchas for an expiration date, as warning lines.

    These are mechanical facts about the session, not model output -- they say
    when the tape itself will misbehave in ways that break the tool's own
    assumptions (notably flow classification).
    """
    out = []
    base = ticker.upper().lstrip("$")
    if is_quarterly_opex(day):
        out.append("QUARTERLY OpEx {} (triple witching).".format(day))
        out.append("  AM vs PM settlement: SPX quarterlies settle on the Friday OPENING "
                   "print (SET), built from each component's opening trade -- SET can "
                   "differ materially from the SPX open drawn on a chart. {} is "
                   "PM-settled at 16:00, so the two books die at OPPOSITE ends of the "
                   "same session.".format(base))
        out.append("  S&P quarterly rebalance prints at that Friday's CLOSE: expect an "
                   "outsized MOC imbalance and a volume spike. Volume-profile and the "
                   "flow.py aggressor classification are unreliable that session.")
    elif day == third_friday(day.year, day.month):
        out.append("MONTHLY OpEx {}: the bulk of index OI rolls off here; {} is "
                   "PM-settled at 16:00.".format(day, base))
    return out


def rolloff_cutoff(contracts, today):
    """Default roll-off cutoff: this week's Friday, else the nearest expiry."""
    friday = week_bounds(today)[1]
    if any(c.expiry <= friday for c in contracts):
        return friday
    future = sorted({c.expiry for c in contracts})
    return future[0] if future else friday


def gamma_rolloff(contracts, spot, cfg, cutoff, now=None):
    """The board BEFORE and AFTER everything through `cutoff` expires.

    A board pinned by positive gamma today can flip negative with no price
    change at all, purely because the gamma holding it there expired. The
    post-expiry profile is the one that describes next week.
    """
    survivors = [c for c in contracts if c.expiry > cutoff]
    expiring = [c for c in contracts if c.expiry <= cutoff]
    if not contracts:
        return None
    before = compute_view(contracts, spot, cfg, now=now)
    after = compute_view(survivors, spot, cfg, now=now) if survivors else {"empty": True, "n": 0}
    gross_all = gross_dollar_gamma(contracts, spot, cfg)
    return {
        "cutoff": cutoff, "before": before, "after": after,
        "n_expiring": len(expiring), "n_surviving": len(survivors),
        "expiring_share": (gross_dollar_gamma(expiring, spot, cfg) / gross_all
                           if gross_all > 0 else None),
    }


def dealer_delta_shares(contracts, spot, cfg, iv_shift=0.0):
    """Aggregate dealer delta, in SHARES of the underlying.

    delta_call = e^{-qT} N(d1);  delta_put = e^{-qT} (N(d1) - 1), signed by the
    dealer convention and scaled by size * multiplier. Scalar loop on purpose:
    this runs a handful of times per report, not inside the 1000-node flip grid.
    """
    total = 0.0
    r, q, mult = cfg.rate, cfg.div_yield, cfg.multiplier
    for c in contracts:
        sigma = c.iv + iv_shift
        if c.T <= 0 or sigma <= 0 or spot <= 0 or c.strike <= 0:
            continue
        vt = sigma * math.sqrt(c.T)
        d1 = (math.log(spot / c.strike) + (r - q + 0.5 * sigma ** 2) * c.T) / vt
        dfq = math.exp(-q * c.T)
        delta = dfq * _norm_cdf(d1) if c.cp == "call" else dfq * (_norm_cdf(d1) - 1.0)
        sign = cfg.convention.call_sign if c.cp == "call" else cfg.convention.put_sign
        size = c.oi if c.size is None else c.size
        total += sign * delta * size * mult
    return total


def charm_vanna_flow(contracts, spot, cfg, now=None):
    """Mechanical hedging flow into the 16:00 close, holding SPOT FIXED.

    Charm is the delta decay itself: as strikes resolve toward 0 or 1 delta the
    dealer book's delta moves even if price does not, and the hedge has to move
    with it. Dealers hold -D against a book delta of D, so when the book goes
    D -> D' they must TRADE -(D' - D) shares. Positive = they must buy.

    This is measured by repricing at the decayed T (the same trick as the flip
    projection), not from a charm closed form -- one code path, one set of
    assumptions. Vanna is the same question asked of a +1 vol point shift.
    """
    now = now_et() if now is None else now
    secs = seconds_to_expiry(now.date(), now)
    if secs <= 0 or not contracts:
        return None
    d_now = dealer_delta_shares(contracts, spot, cfg)
    # Decay to JUST BEFORE the bell (T_FLOOR), not through it. _decayed_contracts
    # DROPS anything that reaches expiry, and counting a vanished contract's whole
    # delta as flow would be wrong: at settlement that delta is extinguished by
    # exercise/assignment, not traded on the tape. What does hit the tape is the
    # migration of deltas toward 0/1 while the contracts are still alive.
    decayed = _decayed_contracts(contracts, max(0.0, secs - T_FLOOR_SECONDS))
    d_close = dealer_delta_shares(decayed, spot, cfg) if decayed else 0.0
    return {
        "seconds": secs,
        "delta_now": d_now,
        "delta_close": d_close,
        "charm_shares": -(d_close - d_now),
        "vanna_shares": -(dealer_delta_shares(contracts, spot, cfg, iv_shift=0.01) - d_now),
        "n_expiring_today": sum(1 for c in contracts if c.expiry == now.date()),
    }


def pin_candidates(contracts, spot, cfg, day, top=5):
    """Strikes with the largest dealer gamma CONCENTRATION expiring on `day`.

    Ranked by raw gross dollar gamma per strike -- not max pain, and no
    proximity fudge factor. Distance from spot is reported so you can judge
    reachability yourself.
    """
    same_day = [c for c in contracts if c.expiry == day]
    if not same_day:
        return []
    prof = compute_gex_profile(same_day, spot, cfg.convention, cfg)
    rows = []
    for i, K in enumerate(prof["strikes"]):
        gross = abs(float(prof["call_gex"][i])) + abs(float(prof["put_gex"][i]))
        if gross > 0:
            rows.append({"strike": float(K), "gross": gross,
                         "net": float(prof["net"][i]),
                         "dist_pct": (float(K) - spot) / spot})
    rows.sort(key=lambda r: -r["gross"])
    return rows[:top]


def print_expiry_risk(all_contracts, spot, cfg, today, spy_ratio=None,
                      cutoff=None, now=None, oi_date=None):
    """Roll-off + charm/vanna + pinning, i.e. what changes when contracts die."""
    now = now_et() if now is None else now
    cutoff = cutoff or rolloff_cutoff(all_contracts, today)

    print("=" * 78)
    print("EXPIRY RISK   (roll-off through {} / charm into the close / pinning)"
          .format(cutoff))
    print("=" * 78)
    for line in expiry_mechanics(cutoff, cfg.ticker):
        print("  ** " + line)
    if is_quarterly_opex(cutoff) or cutoff == third_friday(cutoff.year, cutoff.month):
        print()

    # ---- 1. gamma roll-off ----------------------------------------------
    ro = gamma_rolloff(all_contracts, spot, cfg, cutoff, now=now)
    if ro:
        b, a = ro["before"], ro["after"]
        print("  GAMMA ROLL-OFF   what the board looks like once {} expires".format(cutoff))
        print("  {:<22}{:>18}{:>18}".format("", "NOW", "POST-EXPIRY"))
        print("  {:<22}{:>18}{:>18}".format(
            "contracts", b.get("n", 0), a.get("n", 0)))
        if not a.get("empty"):
            print("  {:<22}{:>18}{:>18}".format(
                "net GEX", fmt_bn(b["total"]), fmt_bn(a["total"])))
            print("  {:<22}{:>18}{:>18}".format(
                "gross |gamma|", fmt_bn(b["gross"]), fmt_bn(a["gross"])))
            fb, fa = b["flip_std"]["flip"], a["flip_std"]["flip"]
            print("  {:<22}{:>18}{:>18}".format("flip", fmt_px(fb), fmt_px(fa)))
            print("  {:<22}{:>18}{:>18}".format(
                "regime at spot", regime_word(spot, fb), regime_word(spot, fa)))
            print("  {:<22}{:>18}{:>18}".format(
                "call wall", fmt_px(b["walls"]["call_wall"]), fmt_px(a["walls"]["call_wall"])))
            print("  {:<22}{:>18}{:>18}".format(
                "put wall", fmt_px(b["walls"]["put_wall"]), fmt_px(a["walls"]["put_wall"])))
            if ro["expiring_share"] is not None:
                print("\n  {:.0%} of gross gamma expires at {}.".format(
                    ro["expiring_share"], cutoff))
            # The headline: does the REGIME survive the expiry?
            if fb is not None and fa is not None:
                d = fa - fb
                print("  Flip moves {:+.2f} ({:+.2f}% of spot) on expiry alone -- no price "
                      "change required.".format(d, d / spot * 100))
                if (spot > fb) != (spot > fa):
                    print("  *** REGIME FLIPS ON EXPIRY: {} today -> {} after, at an "
                          "unchanged spot.".format(regime_word(spot, fb), regime_word(spot, fa)))
                elif abs(spot - fa) < abs(spot - fb):
                    print("  Flip moves TOWARD spot: the gamma holding this level is the "
                          "gamma that dies.")
        else:
            print("  (nothing survives {} in the fetched window -- widen --all-days)"
                  .format(cutoff))
        print()

    # ---- 2. charm / vanna into the close ---------------------------------
    cv = charm_vanna_flow(all_contracts, spot, cfg, now=now)
    if cv:
        hrs = cv["seconds"] / 3600.0
        mult, _lbl, _adv = HEDGE_VENUES["ES"]
        eq = cross_quote(cfg.ticker, spot, spy_ratio) if spy_ratio else None
        idx = eq[1] if (eq and eq[0] == "SPX") else spot
        es = abs(cv["charm_shares"]) * spot / (mult * idx)
        print("  CHARM INTO THE CLOSE   ({:.1f}h left; spot held FIXED)".format(hrs))
        print("  dealer delta now ....... {:>16,.0f} shares".format(cv["delta_now"]))
        print("  dealer delta at close .. {:>16,.0f} shares".format(cv["delta_close"]))
        print("  -> mechanical hedge .... {:>16,.0f} shares  ({:,.0f} ES) to {}".format(
            cv["charm_shares"], es, "BUY" if cv["charm_shares"] > 0 else "SELL"))
        print("  vanna (+1 vol pt) ...... {:>16,.0f} shares".format(cv["vanna_shares"]))
        print("  Delta decay accelerates in the last two hours as strikes resolve to 0 or")
        print("  1 delta. This flow is mechanical -- it carries no information.")
        if cv["n_expiring_today"] == 0:
            print("  (no contracts expiring today: this is decay in the surviving book)")
        print()
    else:
        print("  CHARM INTO THE CLOSE")
        print("  Today's session has settled (past 16:00 ET) -- no decay left to hedge.")
        print("  Run during RTH to see the mechanical flow; it accelerates in the last 2h.")
        print()

    # ---- 3. pinning -------------------------------------------------------
    pins = pin_candidates(all_contracts, spot, cfg, cutoff)
    if not pins:
        print("  PINNING   no contracts expiring {} in the fetched window "
              "(widen --all-days, or pick --expiry-risk DATE).".format(cutoff))
        print()
    if pins:
        secs_left = seconds_to_expiry(cutoff, now)
        print("  PINNING   largest dealer gamma concentration expiring {}".format(cutoff))
        print("  {:>9}{:>16}{:>12}   {}".format("strike", "gross gamma", "vs spot", "net"))
        for p in pins:
            print("  {:>9,.0f}{:>16}{:>11.2f}%   {}".format(
                p["strike"], fmt_bn(p["gross"]), p["dist_pct"] * 100, fmt_bn(p["net"])))
        if secs_left > 0:
            print("  {:.1f}h of pin pressure left; it decays through the session and is "
                  "ZERO at the bell.".format(secs_left / 3600.0))
        else:
            print("  That expiry has settled -- no pin pressure remains.")
        print("  Ranked by raw gamma concentration, NOT max pain. Note ETF pins "
              "({}) are".format(cfg.ticker))
        print("  weaker than single-name pins; index-level pinning shows up in SPX.")
        print()

    if oi_date:
        print("  OI is OCC T+1: this is the {} close, so nothing opened today is in it."
              .format(oi_date))
        print()
    return ro


def print_gamma_buckets(contracts, spot, cfg, today):
    """Render gamma_expiry_buckets() as the console table."""
    rows = gamma_expiry_buckets(contracts, spot, cfg, today)
    print("-" * 78)
    print("GAMMA BY EXPIRY  (weight by hedging urgency, not magnitude alone)")
    print("-" * 78)
    print("  {:<28}{:>15}{:>9}{:>16}".format("bucket", "gross |GEX|", "share", "net GEX"))
    for r in rows:
        print("  {:<28}{:>15}{:>8.1%}{:>16}".format(
            r["label"], fmt_bn(r["gross"]).replace("+", ""), r["share"], fmt_bn(r["net"])))
    print()
    print("  Read: 0DTE gamma is enormous intraday and gone at the close (and its OI")
    print("  is a day stale); 'slow money' will not be rebalanced today. Charm/vanna")
    print("  dominate into monthly OpEx roll-off and are NOT modeled here.")
    print()


def print_profiles(all_contracts, spot, spy_ratio, cfg, today, now=None):
    """Render the INTRADAY and STRUCTURAL profiles side by side.

    Same contract set, two different questions -- see the section header above
    select_profile_contracts() for why they must not be conflated.
    """
    now = now_et() if now is None else now
    horizon = intraday_horizon(today)
    specs = [
        ("INTRADAY  (trading layer)", "intraday",
         "0DTE .. {} (front week + next OpEx) | OI blended with today's volume"
         .format(horizon.isoformat())),
        ("STRUCTURAL  (overview layer)", "structural",
         "all expiries in the fetched window | open interest only"),
    ]
    out = []
    for title, key, desc in specs:
        cs = select_profile_contracts(all_contracts, key, today)
        view = compute_view(cs, spot, cfg) if cs else {"empty": True, "n": 0}
        out.append((title, key, desc, cs, view))

    print("=" * 78)
    print("DUAL PROFILE   (same chain, two questions)")
    print("=" * 78)
    for title, key, desc, cs, view in out:
        print("\n{}".format(title))
        print("  scope: {}".format(desc))
        if view.get("empty"):
            print("  no usable contracts in this profile.")
            continue
        flip = view["flip_std"]["flip"]
        w = view["walls"]
        eq = cross_quote(cfg.ticker, flip, spy_ratio) if flip is not None else None
        print("  contracts {:<6} net GEX {:>14}   gross {:>14}".format(
            view["n"], fmt_bn(view["total"]), fmt_bn(view["gross"])))
        print("  regime .... {}".format(
            regime_word(spot, flip, view["flip_std"].get("total_at_spot"))))
        print("  flip ...... {}{}".format(
            fmt_px(flip), "   ({} {})".format(eq[0], fmt_px(eq[1])) if eq else ""))
        print("  call wall . {:<12} put wall . {}".format(
            fmt_px(w["call_wall"]), fmt_px(w["put_wall"])))
        if key == "intraday":
            # Volume actually blended in -- shows how much of this profile is
            # live flow that prior-close OI would have missed entirely.
            blended = gross_dollar_gamma(cs, spot, cfg)
            base = gross_dollar_gamma([replace(c, size=None) for c in cs], spot, cfg)
            if base > 0:
                print("  volume uplift: {:+.1%} vs OI-only  (today's flow that "
                      "stale OI misses)".format(blended / base - 1.0))
            cliff = zero_dte_cliff(cs, spot, cfg, today, now)
            if cliff:
                print("  *** 0DTE DECAY CLIFF: {:.0%} of this profile ({} contracts) "
                      "expires".format(cliff["share"], cliff["n"]))
                print("      at 16:00 ET -- {} left. These levels are valid only "
                      "until then.".format(cliff["hhmm"]))

    # Agreement check between the layers.
    fi = out[0][4].get("flip_std", {}).get("flip") if not out[0][4].get("empty") else None
    fs = out[1][4].get("flip_std", {}).get("flip") if not out[1][4].get("empty") else None
    print()
    if fi is not None and fs is not None:
        gap = abs(fi - fs)
        print("  layer agreement: intraday flip {:.2f} vs structural {:.2f} "
              "({:.2f} apart = {:.2f}% of spot)".format(fi, fs, gap, gap / spot * 100))
        if gap > 0.01 * spot:
            print("  -> layers DISAGREE by >1% of spot: the near-dated book is "
                  "pulling the flip away from")
            print("     the standing structure. Trade the intraday level, but "
                  "expect it to snap back toward")
            print("     the structural one once the front-dated gamma expires.")
        else:
            print("  -> layers agree: the intraday flip is anchored by durable "
                  "structure (higher confidence).")
    print()
    return out


def week_bounds(today):
    """Monday..Friday of the current trading week (or, on a weekend, the upcoming one)."""
    monday = today - timedelta(days=today.weekday())
    if today.weekday() >= 5:  # Sat/Sun -> next week's Mon-Fri
        monday += timedelta(days=7)
    return monday, monday + timedelta(days=4)


def fetch_window(expiry_arg, today, all_days):
    """(from_date, to_date) to fetch for an --expiry value (None = 0DTE + all).

    from_date is never before today: Schwab's /chains rejects a past fromDate
    with HTTP 400 (verified live: --expiry week on a Thursday sent Monday).
    Earlier days of the week have settled anyway, so nothing is lost.
    """
    e = (expiry_arg or "all").lower()
    if e == "0dte":
        return today, today
    if e == "week":
        monday, friday = week_bounds(today)
        return max(monday, today), friday
    if e == "all":
        return today, today + timedelta(days=all_days)
    d = date.fromisoformat(expiry_arg)
    return d, d


def select_views(all_contracts, expiry_arg, today):
    """Return list of (label, contracts) per the --expiry selection."""
    by_0dte = [c for c in all_contracts if c.expiry == today]
    if expiry_arg is None:
        return [("0DTE", by_0dte), ("ALL EXPIRIES", all_contracts)]
    if expiry_arg.lower() == "0dte":
        return [("0DTE", by_0dte)]
    if expiry_arg.lower() == "week":
        monday, friday = week_bounds(today)
        by_week = [c for c in all_contracts if monday <= c.expiry <= friday]
        label = "WEEK {} - {}".format(monday.isoformat(), friday.isoformat())
        return [(label, by_week)]
    if expiry_arg.lower() == "all":
        return [("ALL EXPIRIES", all_contracts)]
    # explicit date
    d = date.fromisoformat(expiry_arg)
    return [("EXPIRY {}".format(expiry_arg), [c for c in all_contracts if c.expiry == d])]


def render_levels_compact(label, view, spot, spy_ratio, cfg, today):
    """Compact, stable levels block for --levels-only (notifications / quick pulls)."""
    oi_date = prior_trading_session(today).isoformat()
    if view.get("empty"):
        print("{} | {}: {}".format(cfg.ticker, label, explain_empty_view(label, today)))
        print()
        return
    flip = view["flip_std"]["flip"]
    walls = view["walls"]
    print("{} | {} | spot {} | OI {}".format(cfg.ticker, label, fmt_px(spot), oi_date))
    print("  regime .... {}".format(
        regime_word(spot, flip, view["flip_std"].get("total_at_spot"))))
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

    if args.profiles:
        print_profiles(all_contracts, spot, spy_ratio, cfg, today)
    if args.levels_only:
        for lbl, view in computed:
            render_levels_compact(lbl, view, spot, spy_ratio, cfg, today)
    else:
        print_assumptions(cfg, rate_is_default)
        print_data_health(spot, ts_ns, dropped, dropped_expired, floored,
                           len(all_contracts), today, prior_trading_session(today))
        print_side_by_side(computed, today)
        for _d in sorted({today, next_monthly_opex(today)}):
            if (_d - today).days <= 7:
                for _line in expiry_mechanics(_d, cfg.ticker):
                    print("  ** " + _line)

        # Hedging-urgency decomposition of the all-expiries population (the
        # governing rule: match the expiry set to the holding period).
        all_cs = next((cs for lbl, cs in views_raw if lbl == "ALL EXPIRIES" and cs), None)
        if all_cs:
            print_gamma_buckets(all_cs, spot, cfg, today)
        # Implied move from the ATM straddle -- priced off the same 0DTE chain.
        print_implied_move(all_contracts, spot, cfg, today, spy_ratio)
        # Translate the headline gamma into the venue where dealers execute it.
        main_view = next((v for lbl, v in computed if lbl == "ALL EXPIRIES"), None)
        if main_view is None and computed:
            main_view = computed[0][1]
        if main_view:
            print_hedging_flow(main_view, spot, cfg, spy_ratio)
        if getattr(args, "expiry_risk", None):
            cut = (None if args.expiry_risk == "auto"
                   else date.fromisoformat(args.expiry_risk))
            print_expiry_risk(all_contracts, spot, cfg, today, spy_ratio, cutoff=cut,
                              oi_date=prior_trading_session(today).isoformat())

        for lbl, view in computed:
            print_view_detail(lbl, view, spot, spy_ratio, cfg, today)
        for lbl, view in computed:
            render_summary(lbl, view, spot, spy_ratio, cfg, today)

    if not args.no_plot:
        prefix = args.out_prefix or "gex_{}_{}".format(cfg.ticker.replace(":", ""), today.isoformat())
        for lbl, view in computed:
            if view.get("empty"):
                continue
            safe = lbl.lower().replace(" ", "_")
            outpath = "{}_{}.png".format(prefix, safe)
            plot_profile(lbl, view, spot, view["flip_std"]["flip"], view["walls"],
                         cfg.ticker, outpath)
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
    if args.expiry and args.expiry.lower() not in ("0dte", "week", "all"):
        try:
            exp_d = date.fromisoformat(args.expiry)
        except ValueError:
            print("ERROR: --expiry must be '0dte', 'week', 'all', or YYYY-MM-DD.", file=sys.stderr)
            return 2
        if exp_d < today:   # also a guaranteed HTTP 400 from Schwab (past fromDate)
            print("ERROR: --expiry {} is in the past; those contracts have settled.".format(
                args.expiry), file=sys.stderr)
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
        session_close = datetime(today.year, today.month, today.day, 15, 55, tzinfo=ET)
        demo_now = real if real < session_close else \
            datetime(today.year, today.month, today.day, 13, 0, tzinfo=ET)
        demo = make_demo_chain(spot, today, today + timedelta(days=30))
        all_contracts, dropped_expired, floored = enrich_and_filter_time(demo, demo_now)
        ts_ns = int(demo_now.timestamp() * 1e9)
        dropped = {"no_oi": 0, "no_iv": 0, "malformed": 0, "crossed": 0, "itm_bad_quote": 0}
        spy_ratio = SPY_RATIO_FALLBACK
        run(cfg, args, all_contracts, spot, spy_ratio, today, ts_ns, dropped,
            dropped_expired, floored, rate_is_default)
        print("runtime: {:.2f}s".format(time.time() - t_start))
        return 0

    try:
        client = get_schwab_client()
    except RuntimeError as e:
        print("ERROR: {}".format(e), file=sys.stderr)
        print("       Or run `python3 gex.py --demo` for an offline synthetic example.",
              file=sys.stderr)
        return 2

    from_date, to_date = fetch_window(args.expiry, today, args.all_days)
    if args.expiry_risk:   # roll-off is meaningless without the post-expiry book
        to_date = max(to_date, today + timedelta(days=args.all_days))

    warn = check_futures_symbol(cfg.ticker)
    if warn and warn[0] == "ERROR":
        print("ERROR: " + warn[1], file=sys.stderr)
        return 2
    if warn:
        print("  *** WARNING: {}".format(warn[1]))

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

    # Archive the RAW payload before any filtering (see save_chain_snapshot).
    if not args.no_save_chain:
        saved = save_chain_snapshot(data, cfg.ticker, chain_dir=args.chain_dir)
        if saved:
            print("  chain archived: {}".format(saved))

    contracts, spot, ts_ns, dropped, status = parse_schwab_chain(data)
    if status and status != "SUCCESS":
        print("  WARNING: Schwab chains status = {}".format(status))
    print("  {} raw contracts parsed.".format(len(contracts) + sum(dropped.values())))
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
