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
    DealerConvention,
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


CONV_FLIPPED = DealerConvention(1.0, 1.0, "flipped put sign")


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
                  convention=CONV_STANDARD)


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


def test_regime_follows_net_gex_sign_not_spot_vs_flip():
    # Regression: on an INVERTED chain (call wall below spot, put wall above --
    # a stressed-market shape) the curve crosses zero with the opposite slope, so
    # spot > flip is SHORT gamma, not long. The regime must come from the sign of
    # net GEX at spot, never from the bare spot-vs-flip comparison.
    import gex
    contracts = [
        Contract(90.0, date(2027, 1, 1), "call", 5000.0, 0.2, T=1.0),   # below spot
        Contract(110.0, date(2027, 1, 1), "put", 5000.0, 0.2, T=1.0),   # above spot
    ]
    cfg = _cfg()
    cfg.price_range = 0.10
    res = find_flip_level(contracts, 100.0, CONV_STANDARD, cfg)
    assert res["flip"] is not None
    assert res["total_at_spot"] < 0                       # net SHORT gamma at spot
    assert 100.0 > res["flip"]                            # ... yet spot is ABOVE the flip
    # The old label said LONG here; the sign-driven label must say SHORT.
    assert gex.regime_word(100.0, res["flip"], res["total_at_spot"]) == "SHORT gamma"
    # Typical orientation still reads LONG above the flip.
    typical = [
        Contract(110.0, date(2027, 1, 1), "call", 5000.0, 0.2, T=1.0),
        Contract(90.0, date(2027, 1, 1), "put", 5000.0, 0.2, T=1.0),
    ]
    res2 = find_flip_level(typical, 100.0, CONV_STANDARD, cfg)
    assert res2["total_at_spot"] > 0
    assert gex.regime_word(100.0, res2["flip"], res2["total_at_spot"]) == "LONG gamma"
    # Fallback path (no total supplied) keeps the old spot-vs-flip behaviour.
    assert gex.regime_word(100.0, 95.0) == "LONG gamma"
    assert gex.regime_word(100.0, None) == "UNDETERMINED"
    # interpretation_line must agree with the sign too.
    assert "SHORT gamma" in gex.interpretation_line(100.0, res["flip"], res["total_at_spot"])


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
                convention=CONV_STANDARD)
    r1 = find_flip_level(contracts, 100.0, CONV_STANDARD, c1)["flip"]
    r2 = find_flip_level(contracts, 100.0, CONV_STANDARD, c2)["flip"]
    assert r1 == pytest.approx(r2, abs=1e-6)


def _0dte_cfg(steps=1000):
    # 0DTE-style config: tiny T makes gamma a near-step at the strike, which is
    # exactly the regime that exposed the phantom-crossing and grid-node bugs.
    return Config(rate=0.0, div_yield=0.0, multiplier=100, price_range=0.10,
                  steps=steps, convention=CONV_STANDARD)


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
    # Regression: the bracketed crossing is refined by bisection, so the
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


def test_decayed_contracts_preserve_blended_size():
    # Regression: the decayed copies must keep the OI+volume blend (`size`), or
    # flip_close is computed on raw OI while flip_now used the blend -- and the
    # projected "migration" partly measures a weighting change, not time decay.
    import gex
    today = date(2026, 8, 24)
    cs = [Contract(100.0, today, "call", 1000.0, 0.2, T=0.005, volume=9000.0)]
    blended = gex.select_profile_contracts(cs, "intraday", today)
    assert blended[0].size == pytest.approx(10000.0)
    out = gex._decayed_contracts(blended, 3600.0)
    assert len(out) == 1
    assert out[0].size == pytest.approx(10000.0)     # blend carried through
    # And the decayed flip uses the blend: the signed total at spot must scale
    # with size, not with raw OI.
    cfg = _0dte_cfg()
    tot_blend = gex._total_net_gex_at(
        100.0, *gex._to_arrays(out, gex.CONV_STANDARD)[:5], cfg)
    raw = gex._decayed_contracts(cs, 3600.0)          # size=None -> raw OI
    tot_raw = gex._total_net_gex_at(
        100.0, *gex._to_arrays(raw, gex.CONV_STANDARD)[:5], cfg)
    assert tot_blend == pytest.approx(tot_raw * 10.0, rel=1e-9)


def test_flip_time_decay_reports_now_and_close():
    # The projection must return BOTH the current flip and the close-of-day flip,
    # and the signed move between them. _0dte_contracts() carry T = 4 hours, so
    # use a `now` within 4 hours of the 16:00 ET close (13:00 ET -> 3h left) to
    # leave survivors after the decay.
    import gex
    et = gex.ET
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
    et = gex.ET
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
    et = gex.ET
    now = datetime(2026, 8, 5, 13, 30, tzinfo=et)    # 2.5h to the close
    contracts = _0dte_contracts()
    res = flip_time_decay(contracts, 100.0, _0dte_cfg(), now)
    assert res["flip_now"] is not None and res["flip_close"] is not None
    assert res["flip_now"] != pytest.approx(res["flip_close"], abs=1e-9)


def test_compute_view_includes_flip_decay():
    # compute_view must surface the time-decay projection so renderers can show it.
    import gex
    et = gex.ET
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


def test_atm_straddle_implied_move_matches_theory():
    # For an ATM straddle, BSM gives straddle ~= 0.7979 * S * sigma * sqrt(T)
    # (= S * sigma * sqrt(T) * sqrt(2/pi), the EXPECTED ABSOLUTE move). So:
    #   straddle/S      -> expected |move|      (breakeven)
    #   straddle/S *1.2533 -> 1-SD move         (the VIX/16-comparable figure)
    # Price a true ATM pair with BSM and assert the round trip recovers sigma.
    import gex
    S, K, T, sigma = 100.0, 100.0, 1.0 / 365.0, 0.16     # 1 day, 16% vol
    c = gex.bs_price(S, K, T, sigma, cp="call")
    p = gex.bs_price(S, K, T, sigma, cp="put")
    cs = [
        Contract(K, date(2026, 8, 20), "call", 100.0, sigma, T=T, bid=c - .01, ask=c + .01),
        Contract(K, date(2026, 8, 20), "put", 100.0, sigma, T=T, bid=p - .01, ask=p + .01),
    ]
    m = gex.atm_straddle_move(cs, S)
    assert m is not None and m["strike"] == K

    expected_abs = sigma * math.sqrt(T) * math.sqrt(2.0 / math.pi)
    assert m["pct"] == pytest.approx(expected_abs, rel=0.01)     # breakeven
    assert m["sd_pct"] == pytest.approx(sigma * math.sqrt(T), rel=0.01)  # 1-SD

    # The 1-SD figure must be the LARGER one; reporting the breakeven as 1-SD
    # understates the band by ~20%.
    assert m["sd_pct"] > m["pct"]
    assert m["sd_pct"] / m["pct"] == pytest.approx(math.sqrt(math.pi / 2), rel=1e-9)

    # Quote-implied and IV-implied agree when both come from the same inputs.
    assert m["iv_sd_pct"] == pytest.approx(m["sd_pct"], rel=0.02)


def test_is_rth_boundaries():
    # The divergence warning branches on this, so the edges matter.
    import gex
    et = gex.ET
    mk = lambda d, h, m: datetime(2026, 8, d, h, m, tzinfo=et)
    assert gex.is_rth(mk(24, 9, 30)) is True      # Monday open (inclusive)
    assert gex.is_rth(mk(24, 15, 59)) is True
    assert gex.is_rth(mk(24, 16, 0)) is False     # close (exclusive)
    assert gex.is_rth(mk(24, 9, 29)) is False     # pre-open
    assert gex.is_rth(mk(23, 12, 0)) is False     # Sunday
    assert gex.is_rth(mk(22, 12, 0)) is False     # Saturday


def test_implied_move_figures_are_independent_of_T():
    # The frozen-quote/decaying-clock effect distorts the IV cross-check but NOT
    # the move itself: breakeven and 1-SD are pure price ratios. Verified live on
    # a Sunday where straddle vol read 10.4% vs a 6.5% vendor IV purely because T
    # had decayed under a Friday-close price.
    import gex
    exp = date(2026, 8, 24)
    def move_with_T(T):
        cs = [Contract(766.0, exp, "call", 10.0, 0.065, T=T, bid=1.50, ask=1.51),
              Contract(766.0, exp, "put", 10.0, 0.065, T=T, bid=1.89, ask=1.90)]
        return gex.atm_straddle_move(cs, 765.72)

    a = move_with_T(3.0 / 365)      # Friday's clock
    b = move_with_T(1.04 / 365)     # Sunday's clock, same frozen quote
    assert a["pct"] == pytest.approx(b["pct"])          # breakeven unchanged
    assert a["sd_pct"] == pytest.approx(b["sd_pct"])    # 1-SD unchanged
    # Only the IV-based cross-check moves with T.
    assert b["iv_sd_pct"] < a["iv_sd_pct"]


def test_atm_straddle_picks_nearest_strike_quoting_both_sides():
    import gex
    exp = date(2026, 8, 20)
    # 105 is nearest spot 103 but has NO put quote -> must fall back to 100,
    # the nearest strike quoting BOTH sides.
    cs = [
        Contract(105.0, exp, "call", 10.0, 0.2, T=0.01, bid=1.0, ask=1.2),
        Contract(100.0, exp, "call", 10.0, 0.2, T=0.01, bid=3.0, ask=3.2),
        Contract(100.0, exp, "put", 10.0, 0.2, T=0.01, bid=1.0, ask=1.2),
    ]
    m = gex.atm_straddle_move(cs, 103.0)
    assert m["strike"] == 100.0
    assert m["call_px"] == pytest.approx(3.1)      # mid, not last
    assert m["put_px"] == pytest.approx(1.1)
    assert m["straddle"] == pytest.approx(4.2)
    assert m["pct"] == pytest.approx(4.2 / 103.0)

    # Only the requested expiry is used.
    other = date(2026, 9, 19)
    cs2 = cs + [Contract(103.0, other, "call", 10.0, 0.2, T=0.1, bid=9.0, ask=9.2),
                Contract(103.0, other, "put", 10.0, 0.2, T=0.1, bid=9.0, ask=9.2)]
    assert gex.atm_straddle_move(cs2, 103.0, expiry=exp)["strike"] == 100.0
    assert gex.atm_straddle_move(cs2, 103.0, expiry=other)["strike"] == 103.0


def test_option_price_prefers_mid_and_degrades_safely():
    import gex
    exp = date(2026, 8, 20)
    mk = lambda **kw: Contract(100.0, exp, "call", 1.0, 0.2, T=0.01, **kw)
    # Mid is preferred over a stale last print.
    assert gex.option_price(mk(bid=1.0, ask=2.0, last=99.0)) == pytest.approx(1.5)
    # No quote -> fall back to last.
    assert gex.option_price(mk(last=4.0)) == pytest.approx(4.0)
    # Crossed / zero / absent -> None rather than a bogus price.
    assert gex.option_price(mk(bid=2.0, ask=1.0)) is None
    assert gex.option_price(mk(bid=0.0, ask=0.0)) is None
    assert gex.option_price(mk()) is None
    # No usable pair anywhere -> the whole calculation declines to guess.
    assert gex.atm_straddle_move([mk()], 100.0) is None
    assert gex.atm_straddle_move([], 100.0) is None


def test_hedging_flow_converts_dollars_to_contracts():
    # Dealers execute index gamma in ES futures, so dollar GEX becomes physical
    # only once expressed in contracts: GEX / (multiplier * index level).
    import gex
    h = gex.hedging_flow(-5e9, 7650.0, "ES")
    assert h["notional_per_unit"] == pytest.approx(50 * 7650.0)      # $382,500
    assert h["contracts"] == pytest.approx(5e9 / (50 * 7650.0))      # ~13,072
    assert h["pct_of_adv"] == pytest.approx(h["contracts"] / h["adv"])

    # SPY shares price off the ETF, not the index level.
    s = gex.hedging_flow(-5e9, 765.0, "SPY")
    assert s["contracts"] == pytest.approx(5e9 / 765.0)

    # Magnitude only -- a long-gamma book trades just as much, in the other
    # direction, so the sign belongs in `direction`, not the contract count.
    assert gex.hedging_flow(+5e9, 7650.0, "ES")["contracts"] == pytest.approx(
        h["contracts"])
    assert "amplifying" in gex.hedging_flow(-5e9, 7650.0, "ES")["direction"]
    assert "dampening" in gex.hedging_flow(+5e9, 7650.0, "ES")["direction"]

    # Degenerate inputs decline rather than divide by zero.
    assert gex.hedging_flow(1e9, 0.0, "ES") is None
    assert gex.hedging_flow(1e9, 7650.0, "NOPE") is None
    assert gex.hedging_flow(1e9, None, "ES") is None


def test_futures_symbols_are_refused_or_flagged():
    # Verified live: Schwab's /chains returns HTTP 400 for "/ES", and plain "ES"
    # silently returns EVERSOURCE ENERGY (~$71) -- a confident gamma profile for
    # entirely the wrong instrument. Both paths must be caught before compute.
    import gex
    lvl, msg = gex.check_futures_symbol("/ES")
    assert lvl == "ERROR" and "does not serve futures options" in msg
    assert gex.check_futures_symbol("/MES")[0] == "ERROR"

    lvl, msg = gex.check_futures_symbol("ES")
    assert lvl == "WARNING" and "EVERSOURCE" in msg
    assert gex.check_futures_symbol("cl")[0] == "WARNING"      # case-insensitive

    # Ordinary underlyings must pass through untouched.
    for t in ("SPY", "QQQ", "IWM", "AAPL", "$SPX"):
        assert gex.check_futures_symbol(t) is None


def test_merton_gamma_reproduces_black76_when_q_equals_r():
    # Futures options price under Black-76, not spot-Merton. Setting q = r makes
    # the Merton form collapse exactly onto Black-76:
    #   d1 drift (r - q + s^2/2) -> s^2/2, and the exp(-q*T) factor -> exp(-r*T).
    # So the existing gamma function is already correct for futures options IF
    # the caller passes --div-yield equal to --rate (and the right multiplier).
    import gex
    F, K, T, sig, r = 5900.0, 5900.0, 0.05, 0.15, 0.0469
    vt = sig * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sig * sig * T) / vt
    black76 = (math.exp(-r * T) * math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi)
               / (F * vt))
    assert float(gex.compute_gamma_bsm(F, K, T, sig, r=r, q=r)) == pytest.approx(
        black76, rel=1e-12)
    # The default q=0 is the SPOT model and is measurably different.
    assert float(gex.compute_gamma_bsm(F, K, T, sig, r=r, q=0.0)) != pytest.approx(
        black76, rel=1e-6)


def test_walls_stay_on_the_correct_side_of_spot():
    # A "call wall" below spot is not resistance, and a "put wall" above spot is
    # not support. Observed live: QQQ spot 717.51 with the largest call-side AND
    # largest put-side gamma both at strike 700, so BOTH walls reported 700.
    import gex
    cfg = _cfg()
    exp = date(2027, 1, 1)
    # Strike 100 carries the biggest gamma on BOTH sides, but sits BELOW spot.
    contracts = [
        Contract(100.0, exp, "call", 9000.0, 0.2, T=1.0),   # huge, but below spot
        Contract(100.0, exp, "put", 9000.0, 0.2, T=1.0),    # huge, below spot
        Contract(115.0, exp, "call", 3000.0, 0.2, T=1.0),   # above spot -> real resistance
        Contract(95.0, exp, "put", 2000.0, 0.2, T=1.0),     # below spot -> real support
    ]
    spot = 110.0
    profile = gex.compute_gex_profile(contracts, spot, CONV_STANDARD, cfg)

    # Unrestricted (spot=None) reproduces the OLD behaviour: both land on 100.
    old = gex.find_walls(profile)
    assert old["call_wall"] == old["put_wall"] == 100.0

    # Spot-aware: each wall must sit on the side where its label is true.
    w = gex.find_walls(profile, spot)
    assert w["call_wall"] >= spot, "call wall must be at or above spot"
    assert w["put_wall"] <= spot, "put wall must be at or below spot"
    assert w["call_wall"] == 115.0
    assert w["put_wall"] == 100.0          # nearest/largest put gamma below spot
    assert w["call_wall"] != w["put_wall"]

    # The breached magnet is reported, not silently dropped.
    assert w["call_dominant"] == 100.0     # bigger call gamma exists, but below spot
    assert w["put_dominant"] is None       # put wall already IS the dominant put strike


def test_walls_boundary_and_thin_chain():
    # Spot exactly ON a strike: that strike is both nearest resistance and
    # nearest support, so inclusive bounds legitimately allow both walls there.
    import gex
    cfg = _cfg()
    exp = date(2027, 1, 1)
    cs = [Contract(100.0, exp, "call", 1000.0, 0.2, T=1.0),
          Contract(100.0, exp, "put", 1000.0, 0.2, T=1.0)]
    p = gex.compute_gex_profile(cs, 100.0, CONV_STANDARD, cfg)
    w = gex.find_walls(p, 100.0)
    assert w["call_wall"] == w["put_wall"] == 100.0

    # Every strike above spot: the put side has no strikes below, so it must
    # fall back to the global extreme rather than invent or crash.
    cs2 = [Contract(120.0, exp, "call", 1000.0, 0.2, T=1.0),
           Contract(125.0, exp, "put", 1000.0, 0.2, T=1.0)]
    p2 = gex.compute_gex_profile(cs2, 100.0, CONV_STANDARD, cfg)
    w2 = gex.find_walls(p2, 100.0)
    assert w2["put_wall"] == 125.0         # fallback, not None
    assert w2["call_wall"] == 120.0

    # Empty profile stays safe.
    empty = gex.compute_gex_profile([], 100.0, CONV_STANDARD, cfg) if False else {
        "strikes": np.array([]), "call_gex": np.array([]), "put_gex": np.array([])}
    assert gex.find_walls(empty, 100.0)["call_wall"] is None


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
    at_close = datetime(2026, 8, 17, 13, 0, tzinfo=gex.ET)   # 3h to 16:00
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

    def fake_build(key, sec, path):
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

    et = gex.ET
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
    et = gex.ET
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
# scripts/oi_history.py -- dOI opening-ratio grading
# ---------------------------------------------------------------------------
def _load_oi_history():
    """Import scripts/oi_history.py as a module (it lives outside the package)."""
    import importlib.util
    path = os.path.join(os.path.dirname(__file__), "scripts", "oi_history.py")
    spec = importlib.util.spec_from_file_location("oi_history", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
