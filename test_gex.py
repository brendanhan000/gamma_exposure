# -*- coding: utf-8 -*-
"""
Tests for gex.py.

Two things are worth testing hard because everything else rides on them:
  1. compute_gamma_bsm  -- against known reference values (a clean closed-form
     case and Hull's textbook example).
  2. find_flip_level    -- on a synthetic chain whose zero-gamma crossing we
     derive in closed form by hand (see derivation below).

No network is touched.
"""
import math
from datetime import date

import numpy as np
import pytest

from gex import (
    Contract,
    Config,
    CONV_STANDARD,
    CONV_FLIPPED,
    compute_gamma_bsm,
    find_flip_level,
    compute_gex_profile,
    find_walls,
    parse_schwab_chain,
    to_schwab_symbol,
    get_schwab_client,
    cross_quote,
)


# ---------------------------------------------------------------------------
# BSM gamma
# ---------------------------------------------------------------------------
def test_gamma_clean_atm_case():
    # S=K=100, T=1, sigma=0.2, r=q=0:
    #   d1 = (ln(1) + (0 + 0.5*0.04)*1) / (0.2*1) = 0.02/0.2 = 0.1
    #   phi(0.1) = 0.3989423 * exp(-0.005) = 0.39695255
    #   gamma = 0.39695255 / (100 * 0.2 * 1) = 0.0198476275
    g = float(compute_gamma_bsm(100.0, 100.0, 1.0, 0.2, r=0.0, q=0.0))
    assert g == pytest.approx(0.0198476275, abs=1e-9)


def test_gamma_dividend_yield_discount():
    # Merton gamma carries an explicit exp(-q*T) factor. Chosen so d1 = 0 exactly:
    # S=K=100, T=1, sigma=0.2, r=0, q=0.02 -> (r - q + sigma^2/2) = 0 -> d1 = 0.
    #   gamma = e^{-0.02} * phi(0) / (100 * 0.2),  phi(0) = 0.3989422804014327
    expected = math.exp(-0.02) * 0.3989422804014327 / 20.0
    g = float(compute_gamma_bsm(100.0, 100.0, 1.0, 0.2, r=0.0, q=0.02))
    assert g == pytest.approx(expected, rel=1e-12)


def test_gamma_hull_textbook_example():
    # Hull, "Options, Futures, and Other Derivatives": S0=49, K=50, r=0.05,
    # sigma=0.20, T=20/52 (~0.3846). Hull reports gamma ~= 0.066.
    g = float(compute_gamma_bsm(49.0, 50.0, 20.0 / 52.0, 0.20, r=0.05, q=0.0))
    assert g == pytest.approx(0.0656, abs=5e-4)


def test_gamma_call_put_identical_and_independent_of_type():
    # BSM gamma has no call/put dependence: the function takes no type, by design.
    g = float(compute_gamma_bsm(4500.0, 4500.0, 0.1, 0.15, r=0.04, q=0.0))
    assert g > 0


def test_gamma_zero_for_degenerate_inputs():
    # T<=0, sigma<=0, S<=0 must return 0 (never NaN/inf).
    assert float(compute_gamma_bsm(100.0, 100.0, 0.0, 0.2)) == 0.0
    assert float(compute_gamma_bsm(100.0, 100.0, 1.0, 0.0)) == 0.0
    assert float(compute_gamma_bsm(0.0, 100.0, 1.0, 0.2)) == 0.0


def test_gamma_vectorized_shape():
    K = np.array([90.0, 100.0, 110.0])
    g = compute_gamma_bsm(100.0, K, 1.0, 0.2, r=0.0)
    assert g.shape == (3,)
    assert np.all(g >= 0)
    # ATM gamma is the largest of the three here.
    assert np.argmax(g) == 1


# ---------------------------------------------------------------------------
# Flip level on a hand-computed synthetic chain
# ---------------------------------------------------------------------------
def _cfg():
    # r=q=0 so the closed-form crossing is clean; multiplier irrelevant to the zero.
    return Config(rate=0.0, div_yield=0.0, multiplier=100,
                  price_range=0.05, steps=2000,
                  convention=CONV_STANDARD, flipped_convention=CONV_FLIPPED)


def test_find_flip_closed_form_crossing():
    # One call @ K_c=90 and one put @ K_p=110, equal OI, same sigma=0.2, T=1, r=q=0.
    # Standard convention => total ~ gamma_call(S) - gamma_put(S). Both gammas have
    # the common 1/(S*sigma*sqrt(T)) factor and the common dollar S^2 factor, so the
    # zero is where phi(d1_call) = phi(d1_put), i.e. d1_call = -d1_put. Solving:
    #
    #   ln(S/Kc) + cT = -(ln(S/Kp) + cT),   c = r - q + sigma^2/2
    #   => S^2 = Kc*Kp * exp(-2 c T)
    #   => S*  = sqrt(Kc*Kp) * exp(-c T)
    #
    # With Kc=90, Kp=110, sigma=0.2, T=1, r=q=0: c = 0.02,
    #   S* = sqrt(9900) * exp(-0.02) = 99.498744 * 0.980199 = 97.5288
    contracts = [
        Contract(90.0, date(2027, 1, 1), "call", 1.0, 0.2, T=1.0),
        Contract(110.0, date(2027, 1, 1), "put", 1.0, 0.2, T=1.0),
    ]
    expected = math.sqrt(90.0 * 110.0) * math.exp(-0.02)  # 97.5288...
    res = find_flip_level(contracts, spot=100.0, convention=CONV_STANDARD, cfg=_cfg())
    assert res["flip"] is not None
    assert res["flip"] == pytest.approx(expected, abs=0.05)


def test_find_flip_no_crossing_when_all_gamma_positive():
    # Under the flipped put sign both contributions are positive -> total never
    # crosses zero -> flip is None (graceful, no crash). This is exactly the
    # degeneracy the sensitivity guardrail is meant to surface.
    contracts = [
        Contract(90.0, date(2027, 1, 1), "call", 1.0, 0.2, T=1.0),
        Contract(110.0, date(2027, 1, 1), "put", 1.0, 0.2, T=1.0),
    ]
    res = find_flip_level(contracts, spot=100.0, convention=CONV_FLIPPED, cfg=_cfg())
    assert res["flip"] is None
    assert np.all(res["curve"] > 0)


def test_flip_invariant_to_multiplier_and_oi_scaling():
    # The crossing depends only on the gamma*OI balance, not on the common
    # multiplier/dollar factors; doubling the multiplier must not move the flip.
    contracts = [
        Contract(90.0, date(2027, 1, 1), "call", 1.0, 0.2, T=1.0),
        Contract(110.0, date(2027, 1, 1), "put", 1.0, 0.2, T=1.0),
    ]
    c1 = _cfg()
    c2 = Config(rate=0.0, div_yield=0.0, multiplier=1000, price_range=0.05, steps=2000,
                convention=CONV_STANDARD, flipped_convention=CONV_FLIPPED)
    r1 = find_flip_level(contracts, 100.0, CONV_STANDARD, c1)["flip"]
    r2 = find_flip_level(contracts, 100.0, CONV_STANDARD, c2)["flip"]
    assert r1 == pytest.approx(r2, abs=1e-6)


# ---------------------------------------------------------------------------
# Profile / walls
# ---------------------------------------------------------------------------
def test_walls_pick_extreme_net_strikes():
    # Big long-gamma block at 110 (calls) and big short-gamma block at 90 (puts).
    cfg = _cfg()
    contracts = [
        Contract(110.0, date(2027, 1, 1), "call", 5000.0, 0.2, T=1.0),
        Contract(90.0, date(2027, 1, 1), "put", 5000.0, 0.2, T=1.0),
        Contract(100.0, date(2027, 1, 1), "call", 10.0, 0.2, T=1.0),
    ]
    profile = compute_gex_profile(contracts, 100.0, CONV_STANDARD, cfg)
    walls = find_walls(profile)
    assert walls["call_wall"] == 110.0      # most positive net GEX
    assert walls["put_wall"] == 90.0        # most negative net GEX
    assert walls["call_wall_gex"] > 0
    assert walls["put_wall_gex"] < 0


def test_net_total_sign_flips_with_convention():
    cfg = _cfg()
    contracts = [
        Contract(100.0, date(2027, 1, 1), "put", 1000.0, 0.2, T=1.0),
    ]
    std = compute_gex_profile(contracts, 100.0, CONV_STANDARD, cfg)["total"]
    flp = compute_gex_profile(contracts, 100.0, CONV_FLIPPED, cfg)["total"]
    assert std < 0 < flp            # short puts (std) negative, long puts (flipped) positive
    assert std == pytest.approx(-flp, rel=1e-9)


# ---------------------------------------------------------------------------
# Schwab data layer (the riskiest new code; no network touched)
# ---------------------------------------------------------------------------
def test_cross_quote_both_directions_and_none():
    # SPX -> SPY divides by the ratio; SPY -> SPX multiplies; others: no cross-quote.
    assert cross_quote("SPX", 5900.0, 10.0) == ("SPY", 590.0)
    assert cross_quote("$SPX", 5900.0, 10.0) == ("SPY", 590.0)
    assert cross_quote("SPY", 590.0, 10.0) == ("SPX", 5900.0)
    assert cross_quote("QQQ", 500.0, 10.0) is None


def test_to_schwab_symbol():
    assert to_schwab_symbol("SPX") == "$SPX"
    assert to_schwab_symbol("spx") == "$SPX"
    assert to_schwab_symbol("SPY") == "SPY"          # ETF passes through unchanged
    assert to_schwab_symbol("$SPX.X") == "$SPX.X"    # already-prefixed passes through


# A hand-built response in Schwab's exact shape (callExpDateMap/putExpDateMap,
# volatility as a percent, -999.0 sentinel, OI=0 line). No network needed.
SCHWAB_SAMPLE = {
    "status": "SUCCESS",
    "underlyingPrice": 5900.0,
    "underlying": {"quoteTime": 1_718_040_000_000, "last": 5899.5, "mark": 5900.5},
    "callExpDateMap": {
        "2026-06-10:0": {
            "5900.0": [{"putCall": "CALL", "strikePrice": 5900.0,
                        "openInterest": 1000, "volatility": 18.42}],   # valid
            "5905.0": [{"putCall": "CALL", "strikePrice": 5905.0,
                        "openInterest": 0, "volatility": 18.0}],        # dropped: OI=0
        }
    },
    "putExpDateMap": {
        "2026-06-10:0": {
            "5900.0": [{"putCall": "PUT", "strikePrice": 5900.0,
                        "openInterest": 1200, "volatility": -999.0}],   # dropped: IV sentinel
            "5895.0": [{"putCall": "PUT", "strikePrice": 5895.0,
                        "openInterest": 800, "volatility": 20.0}],      # valid
        }
    },
}


def test_parse_schwab_chain():
    contracts, spot, ts_ns, dropped, status = parse_schwab_chain(SCHWAB_SAMPLE)
    assert status == "SUCCESS"
    assert spot == 5900.0
    assert ts_ns == 1_718_040_000_000 * 1_000_000     # ms-epoch -> ns
    assert len(contracts) == 2                          # 1 call + 1 put survive
    assert dropped["no_oi"] == 1
    assert dropped["no_iv"] == 1

    by_type = {c.cp: c for c in contracts}
    assert set(by_type) == {"call", "put"}
    assert by_type["call"].iv == pytest.approx(0.1842)  # percent -> decimal
    assert by_type["call"].strike == 5900.0
    assert by_type["put"].iv == pytest.approx(0.20)
    assert by_type["put"].strike == 5895.0
    assert by_type["call"].expiry == date(2026, 6, 10)  # date from the map key


def test_parse_schwab_chain_empty_is_graceful():
    contracts, spot, ts_ns, dropped, status = parse_schwab_chain(
        {"status": "SUCCESS", "underlyingPrice": 100.0})
    assert contracts == []
    assert spot == 100.0
    assert ts_ns is None


def test_get_schwab_client_errors_without_creds_or_token(tmp_path):
    # get_schwab_client raises a clear RuntimeError BEFORE importing schwab-py,
    # so these checks pass on Python 3.9 (where schwab-py can't be installed).
    import gex

    with pytest.raises(RuntimeError):                       # missing credentials
        gex.get_schwab_client(None, None, str(tmp_path / "t.json"))
    with pytest.raises(RuntimeError):                       # missing token file
        gex.get_schwab_client("KEY", "SECRET", str(tmp_path / "missing.json"))


def test_schwab_setup_script_importable():
    # Importing scripts/schwab_setup.py catches syntax/import regressions. The
    # schwab-py import is lazy (inside main), so this works without schwab-py.
    import importlib.util
    import os

    path = os.path.join(os.path.dirname(__file__), "scripts", "schwab_setup.py")
    spec = importlib.util.spec_from_file_location("schwab_setup", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert callable(mod.main)
    assert isinstance(mod.CALLBACK, str) and mod.CALLBACK
    assert mod.TOKEN_PATH  # default token path is defined
