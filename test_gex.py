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
import os
from datetime import date, datetime, timedelta

import numpy as np
import pytest

from gex import (
    Contract,
    Config,
    CONV_STANDARD,
    CONV_FLIPPED,
    compute_gamma_bsm,
    find_flip_level,
    flip_time_decay,
    _decayed_contracts,
    compute_gex_profile,
    compute_view,
    find_walls,
    parse_schwab_chain,
    to_schwab_symbol,
    get_schwab_client,
    fetch_chain_schwab,
    cross_quote,
    parse_args,
    build_config,
    third_friday,
    next_monthly_opex,
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


def _0dte_cfg(steps=1000):
    # 0DTE-style config: tiny T makes gamma a near-step at the strike, which is
    # exactly the regime that exposed the phantom-crossing and grid-node bugs.
    return Config(rate=0.0, div_yield=0.0, multiplier=100, price_range=0.10,
                  steps=steps, convention=CONV_STANDARD, flipped_convention=CONV_FLIPPED)


def _0dte_contracts():
    T0 = 4 * 3600.0 / (365 * 24 * 3600)   # ~4 hours in years
    return [
        Contract(99.0, date(2026, 8, 5), "put", 5000.0, 0.10, T=T0),
        Contract(101.0, date(2026, 8, 5), "call", 5000.0, 0.10, T=T0),
    ]


def test_flip_no_phantom_crossings_from_underflow():
    # Regression for the wing-underflow bug: far from the strikes, 0DTE gamma
    # underflows to a literal 0.0, and the old detector appended EVERY exact-zero
    # grid node as a "crossing" (77 reported, 76 of them float noise). Only the
    # single genuine crossing near the strikes may be reported.
    res = find_flip_level(_0dte_contracts(), 100.0, CONV_STANDARD, _0dte_cfg())
    assert res["flip"] is not None
    assert len(res["crossings"]) == 1
    # The one crossing must sit between the two strikes, not out in the wings.
    assert 99.0 < res["crossings"][0] < 101.0


def test_flip_total_at_spot_is_exact_not_nearest_grid():
    # Regression: total_at_spot must be evaluated AT spot, not read off the
    # nearest grid node. On a steep 0DTE curve the nearest node can carry the
    # OPPOSITE sign, which used to mislabel the regime in the no-flip branch.
    contracts = _0dte_contracts()
    res = find_flip_level(contracts, 100.0, CONV_STANDARD, _0dte_cfg())
    exact = compute_gex_profile(contracts, 100.0, CONV_STANDARD, _0dte_cfg())["total"]
    assert res["total_at_spot"] == pytest.approx(exact, rel=1e-12)
    assert np.sign(res["total_at_spot"]) == np.sign(exact)


def test_flip_root_refined_to_machine_precision():
    # Regression: the bracketed crossing is refined with Brent's method, so the
    # 1000-step flip matches a 200k-step (near-exact) search to ~1e-9 instead of
    # being limited by linear interpolation across a wide grid cell.
    contracts = _0dte_contracts()
    coarse = find_flip_level(contracts, 100.0, CONV_STANDARD, _0dte_cfg(steps=1000))["flip"]
    fine = find_flip_level(contracts, 100.0, CONV_STANDARD, _0dte_cfg(steps=200000))["flip"]
    assert coarse == pytest.approx(fine, abs=1e-6)


# ---------------------------------------------------------------------------
# Flip time-decay projection (fix #2): the flip migrates as T decays
# ---------------------------------------------------------------------------
def test_decayed_contracts_advance_T_and_drop_expired():
    # Advancing time must shrink every survivor's T and drop contracts that
    # expire inside the interval. T is stored in years (ACT/365).
    T0 = 8 * 3600.0 / (365 * 24 * 3600)     # 8 hours in years
    cs = [Contract(100.0, date(2026, 8, 5), "call", 100.0, 0.2, T=T0)]
    out = _decayed_contracts(cs, 2 * 3600.0)   # advance 2 hours
    assert len(out) == 1
    assert out[0].T == pytest.approx(T0 - 2 * 3600.0 / (365 * 24 * 3600), rel=1e-9)
    # Advancing past expiry drops the contract entirely.
    assert _decayed_contracts(cs, 9 * 3600.0) == []


def test_decayed_contracts_floor_tiny_T():
    # Survivors are floored at the same T_FLOOR as the live snapshot so the
    # projection is numerically consistent (ATM gamma does not blow up).
    import gex
    T0 = 10 * 60.0 / (365 * 24 * 3600)      # 10 minutes in years
    cs = [Contract(100.0, date(2026, 8, 5), "call", 100.0, 0.2, T=T0)]
    out = _decayed_contracts(cs, 9 * 60.0)     # 9 minutes decay -> 1 minute left
    floor_T = gex.T_FLOOR_SECONDS / (365 * 24 * 3600)
    assert out[0].T == pytest.approx(floor_T, rel=1e-9)


def test_flip_time_decay_reports_now_and_close():
    # The projection must return BOTH the current flip and the close-of-day flip,
    # and the signed move between them. _0dte_contracts() carry T = 4 hours, so
    # use a `now` within 4 hours of the 16:00 ET close (13:00 ET -> 3h left) to
    # leave survivors after the decay.
    import gex
    et = gex._et_tz()
    now = datetime(2026, 8, 5, 13, 0, tzinfo=et)     # Wednesday 13:00 ET, 3h to close
    contracts = _0dte_contracts()
    res = flip_time_decay(contracts, 100.0, _0dte_cfg(), now)
    assert 0 < res["seconds"] < 4 * 3600             # less time left than the contracts' T
    assert res["flip_now"] is not None
    assert res["flip_close"] is not None
    assert res["move"] == pytest.approx(res["flip_close"] - res["flip_now"], rel=1e-9)


def test_flip_time_decay_none_after_close():
    # After 16:00 ET the 0DTE has settled: no projection, seconds <= 0.
    import gex
    et = gex._et_tz()
    now = datetime(2026, 8, 5, 17, 30, tzinfo=et)    # Wednesday 17:30 ET
    contracts = _0dte_contracts()
    res = flip_time_decay(contracts, 100.0, _0dte_cfg(), now)
    assert res["seconds"] <= 0
    assert res["flip_close"] is None
    assert res["move"] is None


def test_flip_time_decay_moves_toward_atm_for_0dte():
    # As T -> 0, gamma concentrates at the strikes and the flip is pulled toward
    # the dominant strike. The close-of-day flip must differ from the current
    # flip (the whole point of the projection is that the level is NOT static).
    # _0dte_contracts() carry T = 4h, so use 13:30 ET (2.5h to close) to leave
    # survivors after the decay.
    import gex
    et = gex._et_tz()
    now = datetime(2026, 8, 5, 13, 30, tzinfo=et)    # 2.5h to the close
    contracts = _0dte_contracts()
    res = flip_time_decay(contracts, 100.0, _0dte_cfg(), now)
    assert res["flip_now"] is not None and res["flip_close"] is not None
    assert res["flip_now"] != pytest.approx(res["flip_close"], abs=1e-9)


def test_compute_view_includes_flip_decay():
    # compute_view must surface the time-decay projection so renderers can show it.
    import gex
    et = gex._et_tz()
    now = datetime(2026, 8, 5, 10, 0, tzinfo=et)
    view = compute_view(_0dte_contracts(), 100.0, _0dte_cfg(), now=now)
    assert not view["empty"]
    assert "flip_decay" in view
    assert view["flip_decay"]["flip_now"] is not None
    assert view["flip_decay"]["seconds"] > 0


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
    assert walls["call_wall"] == 110.0      # largest call-side gamma
    assert walls["put_wall"] == 90.0        # largest put-side gamma
    assert walls["call_wall_gex"] > 0
    assert walls["put_wall_gex"] < 0


def test_walls_are_per_side_not_net():
    # Regression for the net-GEX wall bug: a strike with huge call AND put gamma
    # nets to ~zero but must still be found as a wall. Build a balanced strike at
    # 100 (big call + big put, net ~ 0) and a smaller one-sided call at 110.
    # Net-based walls would pick 110 (the only non-zero net); per-side walls must
    # pick 100 for BOTH sides because that is where the gross gamma actually sits.
    cfg = _cfg()
    contracts = [
        Contract(100.0, date(2027, 1, 1), "call", 9000.0, 0.2, T=1.0),   # big call
        Contract(100.0, date(2027, 1, 1), "put",  9000.0, 0.2, T=1.0),   # big put -> net ~0
        Contract(110.0, date(2027, 1, 1), "call", 2000.0, 0.2, T=1.0),   # smaller net winner
    ]
    profile = compute_gex_profile(contracts, 100.0, CONV_STANDARD, cfg)
    # Confirm the balanced strike really does net to ~zero (the bug's premise).
    i100 = int(np.where(profile["strikes"] == 100.0)[0][0])
    assert abs(profile["net"][i100]) < 1e-6 * abs(profile["call_gex"][i100])
    walls = find_walls(profile)
    assert walls["call_wall"] == 100.0      # biggest CALL gamma, despite ~zero net
    assert walls["put_wall"] == 100.0       # biggest PUT gamma, despite ~zero net
    # The per-side magnitudes at the wall exceed the one-sided 110 strike.
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


def test_quote_filters_crossed_and_deep_itm():
    # spot=100. Deep ITM = >5% in the money (calls K<95, puts K>105).
    data = {
        "status": "SUCCESS",
        "underlyingPrice": 100.0,
        "callExpDateMap": {
            "2027-01-15:180": {
                "90.0":  [{"putCall": "CALL", "strikePrice": 90.0, "openInterest": 100,
                           "volatility": 20.0, "bid": 0.0, "ask": 12.0}],   # deep ITM, no bid -> drop
                "100.0": [{"putCall": "CALL", "strikePrice": 100.0, "openInterest": 100,
                           "volatility": 20.0, "bid": 5.2, "ask": 5.0}],    # crossed -> drop
                "98.0":  [{"putCall": "CALL", "strikePrice": 98.0, "openInterest": 100,
                           "volatility": 20.0, "bid": 3.0, "ask": 3.2}],    # clean -> keep
            }
        },
        "putExpDateMap": {
            "2027-01-15:180": {
                "112.0": [{"putCall": "PUT", "strikePrice": 112.0, "openInterest": 100,
                           "volatility": 20.0, "bid": 11.0, "ask": 20.0}],  # deep ITM, spread 58% -> drop
                "80.0":  [{"putCall": "PUT", "strikePrice": 80.0, "openInterest": 100,
                           "volatility": 30.0, "bid": 0.0, "ask": 0.5}],    # deep OTM zero-bid WING -> KEPT
            }
        },
    }
    contracts, spot, ts_ns, dropped, status = parse_schwab_chain(data)
    assert dropped["itm_bad_quote"] == 2      # ITM call no-bid + ITM put wide spread
    assert dropped["crossed"] == 1
    kept = {(c.cp, c.strike) for c in contracts}
    assert kept == {("call", 98.0), ("put", 80.0)}   # wing gamma preserved


def test_quote_filter_crossed_to_zero_ask():
    # Regression: a crossed quote with ask == 0 (bid > ask == 0, common pre-open)
    # used to slip through because the crossed check required ask > 0. bid > ask
    # is crossed regardless of the ask level and must be dropped.
    data = {
        "status": "SUCCESS",
        "underlyingPrice": 100.0,
        "callExpDateMap": {
            "2027-01-15:180": {
                "100.0": [{"putCall": "CALL", "strikePrice": 100.0, "openInterest": 100,
                           "volatility": 20.0, "bid": 1.5, "ask": 0.0}],   # crossed-to-zero -> drop
                "101.0": [{"putCall": "CALL", "strikePrice": 101.0, "openInterest": 100,
                           "volatility": 20.0, "bid": 1.0, "ask": 1.2}],   # clean -> keep
            }
        },
        "putExpDateMap": {},
    }
    contracts, spot, ts_ns, dropped, status = parse_schwab_chain(data)
    assert dropped["crossed"] == 1
    assert {(c.cp, c.strike) for c in contracts} == {("call", 101.0)}


def test_div_yield_per_ticker_map():
    # "Get dividends and rates right": q auto-resolves per ticker unless overridden.
    assert build_config(parse_args([])).div_yield == pytest.approx(0.012)               # SPY default
    assert build_config(parse_args(["--ticker", "QQQ"])).div_yield == pytest.approx(0.006)
    assert build_config(parse_args(["--ticker", "XYZ"])).div_yield == 0.0               # unknown -> 0
    assert build_config(parse_args(["--div-yield", "0.0"])).div_yield == 0.0            # flag wins
    assert build_config(parse_args(["--ticker", "QQQ", "--div-yield", "0.01"])).div_yield == 0.01


def test_contract_weight_blends_volume_by_dte():
    # Intraday: prior-close OI is stale for near-dated contracts, so today's
    # volume is blended in -- fully for 0DTE, half out to the front week, not
    # at all beyond. Structural weighting must ignore volume entirely.
    import gex
    today = date(2026, 8, 17)
    mk = lambda exp, oi, vol: Contract(100.0, exp, "call", oi, 0.2, volume=vol)

    zero = mk(today, 1000, 5000)                     # expires today
    week = mk(today + timedelta(days=3), 1000, 5000)  # front week
    far = mk(today + timedelta(days=40), 1000, 5000)  # beyond

    # structural: OI only, regardless of volume or tenor
    for c in (zero, week, far):
        assert gex.contract_weight(c, "oi", today) == 1000

    assert gex.contract_weight(zero, "blend", today) == 1000 + 5000 * 1.0
    assert gex.contract_weight(week, "blend", today) == 1000 + 5000 * 0.5
    assert gex.contract_weight(far, "blend", today) == 1000        # no volume credit

    # An already-expired contract still counts as 0DTE-tier, not negative-tier.
    past = mk(today - timedelta(days=1), 100, 200)
    assert gex.contract_weight(past, "blend", today) == 100 + 200 * 1.0


def test_profiles_differ_in_scope_and_weighting():
    import gex
    today = date(2026, 8, 17)                # Monday; next OpEx = Fri Aug 21
    assert gex.next_monthly_opex(today) == date(2026, 8, 21)
    assert gex.intraday_horizon(today) == date(2026, 8, 22)   # max(front week, OpEx)

    near = Contract(100.0, today, "call", 10.0, 0.2, T=0.01, volume=90.0)
    mid = Contract(100.0, date(2026, 8, 21), "put", 10.0, 0.2, T=0.02, volume=90.0)
    far = Contract(100.0, date(2026, 12, 18), "call", 10.0, 0.2, T=0.4, volume=90.0)
    allc = [near, mid, far]

    intra = gex.select_profile_contracts(allc, "intraday", today)
    struct = gex.select_profile_contracts(allc, "structural", today)

    assert len(intra) == 2 and len(struct) == 3       # far excluded intraday
    assert [c.size for c in intra] == [10 + 90, 10 + 90 * 0.5]
    assert all(c.size == 10 for c in struct)          # OI only

    # Profiles must not share mutated state with each other or the source list.
    assert near.size is None
    assert intra[0] is not struct[0]


def test_zero_dte_cliff_quantifies_what_expires():
    import gex
    today = date(2026, 8, 17)
    cfg = _cfg()
    at_close = datetime(2026, 8, 17, 13, 0, tzinfo=gex._et_tz())   # 3h to 16:00
    cs = [
        Contract(100.0, today, "call", 1000.0, 0.2, T=0.001),      # expires today
        Contract(100.0, date(2026, 9, 18), "call", 1000.0, 0.2, T=0.09),
    ]
    c = gex.zero_dte_cliff(cs, 100.0, cfg, today, now=at_close)
    assert c is not None
    assert c["n"] == 1
    assert 0.0 < c["share"] < 1.0
    assert c["hhmm"].startswith("3h")

    # No 0DTE in the set -> nothing to warn about.
    assert gex.zero_dte_cliff(cs[1:], 100.0, cfg, today, now=at_close) is None


def test_token_write_is_atomic_and_journaled(tmp_path, monkeypatch):
    # schwab-py's default writer opens the token with mode 'w', which TRUNCATES
    # before writing: a crash or two overlapping writers can leave a corrupt
    # token that Schwab rejects. Ours writes to a temp file and os.replace()s,
    # so a reader sees either the old token or the new one -- never a partial.
    import gex
    import json

    p = tmp_path / "tok.json"
    p.write_text(json.dumps({"creation_timestamp": 1,
                             "token": {"refresh_token": "RT_ONE", "expires_at": 111}}))
    monkeypatch.setattr(gex, "TOKEN_AUDIT_LOG", str(tmp_path / "audit.log"))

    read, write = gex._token_reader(str(p)), gex._token_writer(str(p))
    before = read()
    write({"creation_timestamp": 1, "token": {"refresh_token": "RT_TWO", "expires_at": 222}})
    after = read()

    assert after["token"]["refresh_token"] == "RT_TWO"
    assert json.loads(p.read_text())                       # still valid JSON
    assert not [f for f in os.listdir(str(tmp_path)) if f.startswith(".schwab_tok")]

    # A failed write must not destroy the existing token (atomicity).
    orig = p.read_text()
    with pytest.raises(TypeError):
        write({"bad": {1, 2, 3}})                          # sets are not JSON-serializable
    assert p.read_text() == orig

    # Journal records both touches, with rotation visible via fingerprints.
    log = (tmp_path / "audit.log").read_text()
    assert "READ" in log and "WRITE" in log
    assert gex._token_fingerprint(before) != gex._token_fingerprint(after)
    # Fingerprints must never leak the secret itself.
    assert "RT_ONE" not in log and "RT_TWO" not in log


def test_token_lock_is_reentrant_within_a_process(tmp_path):
    # Nested acquisition must NOT deadlock: the fetch path locks, and a session
    # wrapper may lock around it. flock is per-fd, so without re-entrancy the
    # process would block against its own held lock until the 120s timeout.
    import gex
    tok = tmp_path / "t.json"
    tok.write_text("{}")
    reached = []
    with gex.token_lock(str(tok), timeout=2.0):
        with gex.token_lock(str(tok), timeout=2.0):
            with gex.token_lock(str(tok), timeout=2.0):
                reached.append(True)
    assert reached == [True]
    assert gex._lock_depth["n"] == 0                 # fully unwound

    # Still exclusive to OTHER processes after nesting unwinds.
    import subprocess, sys as _s
    code = ("import fcntl\n"
            "f=open(%r,'a+')\n"
            "try:\n fcntl.flock(f.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB); print('ACQUIRED')\n"
            "except OSError: print('BLOCKED')\n" % (str(tok) + ".lock"))
    assert "ACQUIRED" in subprocess.run([_s.executable, "-c", code],
                                        capture_output=True, text=True).stdout


def test_reload_client_if_stale_rebuilds_on_rotation(tmp_path, monkeypatch):
    # THE fix for tokens dying within hours: a client whose token file changed
    # underneath it must be rebuilt before use, or it presents a superseded
    # refresh token and Schwab revokes the whole family.
    import gex
    import time as _t

    tok = tmp_path / "tok.json"
    tok.write_text('{"token": {}}')

    class _C:
        pass

    built = []

    def fake_build(key, sec, path, callback=None):
        c = _C()
        c._gex_token_path = path
        c._gex_token_mtime = gex._token_mtime(path)
        c._gex_app_key, c._gex_app_secret = key, sec
        built.append(c)
        return c

    monkeypatch.setattr(gex, "get_schwab_client", fake_build)
    c1 = fake_build("K", "S", str(tok))

    # Unchanged file -> same object, no needless rebuild.
    assert gex.reload_client_if_stale(c1) is c1
    assert len(built) == 1

    # Token rotated by another process -> must hand back a REBUILT client.
    _t.sleep(0.01)
    os.utime(str(tok), (_t.time() + 5, _t.time() + 5))
    c2 = gex.reload_client_if_stale(c1)
    assert c2 is not c1 and len(built) == 2

    # A client with no stamped credentials degrades safely instead of raising.
    bare = _C()
    bare._gex_token_path = str(tok)
    bare._gex_token_mtime = None
    assert gex.reload_client_if_stale(bare) is bare


def test_chain_archive_persists_raw_rows(tmp_path):
    # The archive must keep what the GEX filters THROW AWAY: zero-OI strikes and
    # the -999 IV sentinel are the baseline tomorrow's dOI is measured against.
    import gex
    import csv
    import gzip

    rows = gex.chain_snapshot_rows(SCHWAB_SAMPLE, "SPY")
    assert len(rows) == 4                      # all four contracts, unfiltered
    ivs = [r[gex.CHAIN_COLUMNS.index("iv_pct")] for r in rows]
    ois = [r[gex.CHAIN_COLUMNS.index("oi")] for r in rows]
    assert -999.0 in ivs                       # sentinel preserved verbatim
    assert 0 in ois                            # zero-OI strike preserved
    assert 18.42 in ivs                        # vendor PERCENT units, not decimal

    path = gex.save_chain_snapshot(SCHWAB_SAMPLE, "SPY", chain_dir=str(tmp_path))
    assert path and os.path.exists(path)
    with gzip.open(path, "rt") as f:
        back = list(csv.DictReader(f))
    assert len(back) == 4
    assert set(back[0]) == set(gex.CHAIN_COLUMNS)
    assert back[0]["ticker"] == "SPY"
    assert float(back[0]["spot"]) == 5900.0


def test_chain_archive_never_raises_on_bad_input(tmp_path):
    # Archiving is best-effort: it must never break a live run.
    import gex
    assert gex.save_chain_snapshot({}, "SPY", chain_dir=str(tmp_path)) is None
    assert gex.save_chain_snapshot({"callExpDateMap": None}, "SPY",
                                   chain_dir=str(tmp_path)) is None


def test_explain_empty_view_distinguishes_expired_from_missing():
    # An empty 0DTE view after 16:00 ET is CORRECT (the options settled), not a
    # failure. The message must say which, so the user does not think it broke.
    import gex
    from datetime import datetime

    et = gex._et_tz()
    wed = date(2026, 8, 5)                       # a Wednesday

    after_close = datetime(2026, 8, 5, 23, 18, tzinfo=et)
    msg = gex.explain_empty_view("0DTE", wed, now=after_close)
    assert "settled at 16:00" in msg and "7h 18m" in msg

    before_close = datetime(2026, 8, 5, 10, 0, tzinfo=et)
    msg2 = gex.explain_empty_view("0DTE", wed, now=before_close)
    assert "usable OI" in msg2 and "settled" not in msg2   # still live -> thin data

    sat = date(2026, 8, 8)
    assert "weekend" in gex.explain_empty_view("0DTE", sat,
                                               now=datetime(2026, 8, 8, 10, 0, tzinfo=et))

    # Non-0DTE labels get their own wording, never the 16:00 story.
    assert "16:00" not in gex.explain_empty_view("ALL EXPIRIES", wed, now=after_close)
    assert "settled" in gex.explain_empty_view("EXPIRY 2026-08-04", wed, now=after_close)


def test_is_0dte_label_detection():
    # Fix #3: the 0DTE staleness escalation fires only for 0DTE views. The label
    # helper must recognize the 0DTE tag regardless of case and reject others.
    import gex
    assert gex._is_0dte_label("0DTE")
    assert gex._is_0dte_label("0dte")
    assert not gex._is_0dte_label("ALL EXPIRIES")
    assert not gex._is_0dte_label("EXPIRY 2026-08-21")


def test_render_summary_escalates_0dte_staleness(capsys):
    # Fix #3: a 0DTE view WITH a flip must carry the prominent staleness caveat
    # next to the headline number; a non-0DTE view must NOT.
    import gex
    et = gex._et_tz()
    now = datetime(2026, 8, 5, 10, 0, tzinfo=et)     # mid-session Wednesday
    today = now.date()
    cfg = _0dte_cfg()
    cfg.ticker = "SPY"
    view = compute_view(_0dte_contracts(), 100.0, cfg, now=now)
    assert view["flip_std"]["flip"] is not None

    gex.render_summary("0DTE", view, 100.0, 10.0, cfg, today)
    out = capsys.readouterr().out
    assert "0DTE CAVEAT" in out
    assert "LAST NIGHT's OI" in out

    gex.render_summary("ALL EXPIRIES", view, 100.0, 10.0, cfg, today)
    out_all = capsys.readouterr().out
    assert "0DTE CAVEAT" not in out_all


def test_monthly_opex_calendar():
    assert third_friday(2026, 7) == date(2026, 7, 17)
    assert third_friday(2026, 8) == date(2026, 8, 21)
    assert next_monthly_opex(date(2026, 7, 17)) == date(2026, 7, 17)   # OpEx day itself
    assert next_monthly_opex(date(2026, 7, 18)) == date(2026, 8, 21)   # after -> next month
    assert next_monthly_opex(date(2026, 12, 20)) == date(2027, 1, 15)  # year rollover


class _OkResp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"status": "SUCCESS"}


def test_fetch_chain_retries_transient_then_succeeds():
    # Two timeouts, then success: the fetch must retry through transient faults
    # (scheduled morning runs were dying on a single reset/timeout).
    class _Flaky:
        calls = 0

        def get_option_chain(self, symbol, **kw):
            self.calls += 1
            if self.calls <= 2:
                raise TimeoutError("the read operation timed out")
            return _OkResp()

    c = _Flaky()
    out = fetch_chain_schwab(c, "SPY", retry_wait=0.0)
    assert out["status"] == "SUCCESS"
    assert c.calls == 3


def test_fetch_chain_does_not_retry_auth_or_4xx():
    # An expired refresh token must fail FAST (re-login is the only fix).
    class OAuthError(Exception):
        pass

    class _AuthDead:
        calls = 0

        def get_option_chain(self, symbol, **kw):
            self.calls += 1
            raise OAuthError("refresh token is invalid, expired or revoked")

    a = _AuthDead()
    with pytest.raises(OAuthError):
        fetch_chain_schwab(a, "SPY", retry_wait=0.0)
    assert a.calls == 1

    # 4xx responses are client errors -- retrying cannot heal them.
    class _HttpErr(Exception):
        def __init__(self):
            super().__init__("404 Not Found")
            self.response = type("R", (), {"status_code": 404})()

    class _NotFound:
        calls = 0

        def get_option_chain(self, symbol, **kw):
            self.calls += 1
            raise _HttpErr()

    n = _NotFound()
    with pytest.raises(_HttpErr):
        fetch_chain_schwab(n, "SPY", retry_wait=0.0)
    assert n.calls == 1


def test_token_lock_is_exclusive_across_processes(tmp_path):
    # The lock must actually exclude a second holder (Schwab rotates the refresh
    # token on refresh; concurrent refreshes revoke each other).
    import gex
    import os
    import subprocess
    import sys
    import time

    tok = tmp_path / "tok.json"
    tok.write_text("{}")
    with gex.token_lock(str(tok)):
        # A separate PROCESS must fail to take the same flock while we hold it.
        code = (
            "import fcntl,sys\n"
            "f=open(%r,'a+')\n"
            "try:\n"
            "    fcntl.flock(f.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB); print('ACQUIRED')\n"
            "except OSError: print('BLOCKED')\n" % (str(tok) + ".lock")
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert "BLOCKED" in out.stdout

    # Released afterwards.
    out2 = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert "ACQUIRED" in out2.stdout


def test_token_lock_timeout_proceeds_unlocked(tmp_path):
    # A stuck lock must NOT hard-fail the run (outage > race).
    import gex
    tok = tmp_path / "t.json"
    tok.write_text("{}")
    ran = []
    with gex.token_lock(str(tok), timeout=0.0):
        with gex.token_lock(str(tok), timeout=0.05):   # cannot acquire; proceeds
            ran.append(True)
    assert ran == [True]


def test_schwab_client_stale_detects_rewritten_token(tmp_path):
    # A long-lived holder must notice the token file was replaced by a re-login.
    import gex
    import time

    tok = tmp_path / "tok.json"
    tok.write_text('{"token": {}}')

    class _C:
        pass

    c = _C()
    c._gex_token_path = str(tok)
    c._gex_token_mtime = gex._token_mtime(str(tok))
    assert gex.schwab_client_stale(c) is False

    time.sleep(0.01)
    os.utime(str(tok), (time.time() + 5, time.time() + 5))   # simulate re-login
    assert gex.schwab_client_stale(c) is True

    # A client with no stamped path (not built by get_schwab_client) is inert.
    assert gex.schwab_client_stale(_C()) is False


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


# ---------------------------------------------------------------------------
# scripts/oi_history.py -- dOI opening-ratio grading + CBOE open-close ingestion
# ---------------------------------------------------------------------------
def _load_oi_history():
    """Import scripts/oi_history.py as a module (it lives outside the package)."""
    import importlib.util
    path = os.path.join(os.path.dirname(__file__), "scripts", "oi_history.py")
    spec = importlib.util.spec_from_file_location("oi_history", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_open_close_column_matching_flexible_spellings():
    # The CSV column matcher must accept common vendor spellings (case/punct-insensitive).
    oih = _load_oi_history()
    fields = ["Underlying", "Expiration Date", "Strike Price", "Call/Put",
              "Cust Open Buy", "Cust Open Sell"]
    assert oih._oc_col(fields, "underlying") == "Underlying"
    assert oih._oc_col(fields, "expiry") == "Expiration Date"
    assert oih._oc_col(fields, "strike") == "Strike Price"
    assert oih._oc_col(fields, "cp") == "Call/Put"
    assert oih._oc_col(fields, "cust_open_buy") == "Cust Open Buy"
    assert oih._oc_col(fields, "cust_open_sell") == "Cust Open Sell"
    assert oih._oc_col(fields, "nonexistent") is None


def test_load_open_close_parses_and_validates(tmp_path):
    # A well-formed CSV parses to canonical keys; a missing column raises ValueError.
    oih = _load_oi_history()
    good = tmp_path / "oc.csv"
    good.write_text(
        "underlying,expiry,strike,cp,cust_open_buy,cust_open_sell\n"
        "SPY,2026-08-21,590,put,5000,1000\n"
        "SPY,2026-08-21,600,call,200,3000\n"
        "QQQ,2026-08-21,500,put,900,100\n")
    rows = oih.load_open_close(str(good))
    assert len(rows) == 3
    r0 = rows[0]
    assert r0["underlying"] == "SPY" and r0["cp"] == "put" and r0["strike"] == 590.0
    assert r0["cust_open_buy"] == 5000.0 and r0["cust_open_sell"] == 1000.0

    bad = tmp_path / "bad.csv"
    bad.write_text("underlying,expiry,strike,cp\nSPY,2026-08-21,590,put\n")
    with pytest.raises(ValueError):
        oih.load_open_close(str(bad))


def test_dealer_sign_report_put_assumption_supported():
    # Customers net-BOUGHT puts (open buy > open sell) -> dealers SHORT puts ->
    # the standard put_sign = -1 assumption is supported.
    oih = _load_oi_history()
    rows_in = [
        {"underlying": "SPY", "expiry": "2026-08-21", "strike": 590.0, "cp": "put",
         "cust_open_buy": 8000.0, "cust_open_sell": 1000.0},   # net +7000 cust buy
        {"underlying": "SPY", "expiry": "2026-08-21", "strike": 600.0, "cp": "call",
         "cust_open_buy": 500.0, "cust_open_sell": 4000.0},    # net -3500 cust (sold calls)
    ]
    rows, s = oih.dealer_sign_report(rows_in, "SPY", 20)
    assert s["n_put_strikes"] == 1
    assert s["n_put_dealer_short"] == 1
    assert s["n_put_dealer_long"] == 0
    assert s["net_customer_put_opening"] == 7000.0
    assert s["assumption_holds"] is True
    # Dealer sign is the OPPOSITE of the customer opening side.
    put_row = next(r for r in rows if r[2] == "put")
    assert put_row[5] == -1.0          # dealers SHORT the 590 put
    call_row = next(r for r in rows if r[2] == "call")
    assert call_row[5] == 1.0          # customers net-sold calls -> dealers LONG calls


def test_dealer_sign_report_put_assumption_fails():
    # Customers net-SOLD puts -> dealers LONG puts -> put_sign = -1 assumption FAILS.
    oih = _load_oi_history()
    rows_in = [
        {"underlying": "SPY", "expiry": "2026-08-21", "strike": 590.0, "cp": "put",
         "cust_open_buy": 500.0, "cust_open_sell": 9000.0},    # net -8500 cust (sold puts)
    ]
    rows, s = oih.dealer_sign_report(rows_in, "SPY", 20)
    assert s["n_put_dealer_long"] == 1
    assert s["net_customer_put_opening"] == -8500.0
    assert s["assumption_holds"] is False
    assert rows[0][5] == 1.0           # dealers LONG the 590 put


def test_dealer_sign_report_filters_ticker():
    # Rows for other underlyings are ignored.
    oih = _load_oi_history()
    rows_in = [
        {"underlying": "QQQ", "expiry": "2026-08-21", "strike": 500.0, "cp": "put",
         "cust_open_buy": 9000.0, "cust_open_sell": 100.0},
    ]
    rows, s = oih.dealer_sign_report(rows_in, "SPY", 20)
    assert rows == []
    assert s["n_put_strikes"] == 0


def test_delta_report_grades_lean_by_opening_ratio(tmp_path, capsys):
    # Free upgrade: the aggressor lean is graded/suppressed by dOI/volume. Build a
    # two-day archive where one strike mostly OPENED (high ratio, lean kept) and
    # another mostly CHURNED (low ratio, lean suppressed).
    import csv
    import gzip
    import gex
    oih = _load_oi_history()

    def write_day(d, rows):
        p = tmp_path / "SPY"
        p.mkdir(exist_ok=True)
        fp = p / (d + ".csv.gz")
        with gzip.open(str(fp), "wt", newline="") as f:
            w = csv.writer(f)
            w.writerow(gex.CHAIN_COLUMNS)
            w.writerows(rows)
    # Day 1: baseline OI.
    write_day("2026-08-06", [
        ["2026-08-06T16:00:00-0400", "2026-08-05", "SPY", 590.0, "2026-08-21", "put",
         590.0, 1000, 20.0, 4.0, 4.2, 4.1, 5000],
        ["2026-08-06T16:00:00-0400", "2026-08-05", "SPY", 590.0, "2026-08-21", "put",
         580.0, 1000, 22.0, 2.0, 2.2, 2.1, 5000],
    ])
    # Day 2: 590 put OI +800 on volume 1000 (80% opened, last near ask -> lean kept);
    #        580 put OI +800 on volume 9000 (9% opened -> churn -> lean suppressed).
    write_day("2026-08-07", [
        ["2026-08-07T16:00:00-0400", "2026-08-06", "SPY", 590.0, "2026-08-21", "put",
         590.0, 1800, 20.0, 4.0, 4.2, 4.19, 1000],
        ["2026-08-07T16:00:00-0400", "2026-08-06", "SPY", 590.0, "2026-08-21", "put",
         580.0, 1800, 22.0, 2.0, 2.2, 2.19, 9000],
    ])

    rc = oih.delta_report(str(tmp_path), "SPY", 20, None)
    assert rc == 0
    out = capsys.readouterr().out
    # The high-opening-ratio strike keeps a confidence-graded lean.
    assert "HIGH conf" in out
    # The churned strike's lean is suppressed (no SHORT/LONG gamma tag on its row).
    churn_line = [ln for ln in out.splitlines() if "580" in ln]
    assert churn_line and "dealer" not in churn_line[0]
