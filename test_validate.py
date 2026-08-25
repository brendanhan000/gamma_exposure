# -*- coding: utf-8 -*-
"""Tests for scripts/validate.py -- statistics and no-look-ahead joining."""
import datetime
import importlib.util
import os

import pytest

spec = importlib.util.spec_from_file_location(
    "validate", os.path.join(os.path.dirname(__file__), "scripts", "validate.py"))
validate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validate)


def test_welch_t_matches_hand_computation():
    a, b = [1.0, 2.0, 3.0, 4.0], [2.0, 3.0, 4.0, 5.0]
    t, p = validate.welch_t(a, b)
    # Same spread, means differ by exactly 1.0 -> t = -1/sqrt(2*(5/3)/4)
    assert t == pytest.approx(-1.0 / ((2 * (5.0 / 3.0) / 4) ** 0.5), rel=1e-9)
    assert 0.0 < p < 1.0
    # Identical samples -> no difference.
    assert validate.welch_t(a, a)[0] == pytest.approx(0.0)
    # Degenerate inputs decline rather than raise.
    assert validate.welch_t([1.0], [2.0]) == (None, None)
    assert validate.welch_t([], []) == (None, None)
    # Zero variance in both groups -> undefined, not a divide-by-zero.
    assert validate.welch_t([2.0, 2.0], [2.0, 2.0]) == (None, None)


def test_verdict_refuses_to_call_significance_on_small_samples():
    # A large t on 3 observations must NOT be reported as significant.
    assert "n too small" in validate._verdict(9.9, 0.0001, 3, 3, min_n=20)
    assert "SIGNIFICANT" in validate._verdict(3.0, 0.01, 25, 25, min_n=20)
    assert "not significant" in validate._verdict(0.2, 0.8, 25, 25, min_n=20)
    assert "insufficient" in validate._verdict(None, None, 30, 30, min_n=20)


def test_panel_scores_against_the_NEXT_session_only():
    # No look-ahead: a level computed on D is graded on D+1, never on D itself.
    d1, d2, d3 = (datetime.date(2026, 8, 3), datetime.date(2026, 8, 4),
                  datetime.date(2026, 8, 5))
    ohlc = {d1: (100.0, 101.0, 99.0, 100.0),
            d2: (102.0, 105.0, 101.0, 104.0),
            d3: (106.0, 108.0, 105.0, 107.0)}
    levels = [{"date": d1, "spot": 100.0, "flip": 99.0, "call_wall": 105.0,
               "put_wall": 95.0, "net_gex": 1e9, "regime": "LONG gamma"}]
    rows = validate.build_panel(levels, ohlc)
    assert len(rows) == 1
    r = rows[0]
    assert r["next"] == d2                       # graded on the FOLLOWING session
    assert r["open"] == 102.0 and r["close"] == 104.0
    assert r["prev_close"] == 100.0              # close of the level's own day
    # Overnight = prior close -> next open; RTH = next open -> next close.
    assert r["overnight_ret"] == pytest.approx(102.0 / 100.0 - 1)
    assert r["rth_ret"] == pytest.approx(104.0 / 102.0 - 1)
    assert r["rth_range"] == pytest.approx((105.0 - 101.0) / 102.0)

    # The most recent day has no following session yet -> excluded, not guessed.
    assert validate.build_panel(
        [{"date": d3, "spot": 1.0, "flip": None, "call_wall": None,
          "put_wall": None, "net_gex": 0.0, "regime": "LONG gamma"}], ohlc) == []


def test_overnight_vs_rth_needs_both_regimes(capsys):
    d1, d2 = datetime.date(2026, 8, 3), datetime.date(2026, 8, 4)
    only_long = [{"date": d1, "next": d2, "regime": "LONG gamma", "spot": 100.0,
                  "flip": 99.0, "call_wall": None, "put_wall": None,
                  "net_gex": 1e9, "open": 100.0, "high": 101.0, "low": 99.0,
                  "close": 100.5, "prev_close": 100.0, "overnight_ret": 0.0,
                  "rth_ret": 0.005, "rth_range": 0.02}]
    assert validate.test_overnight_vs_rth(only_long) is None
    assert "Need both regimes" in capsys.readouterr().out


def test_levels_from_archive_returns_none_on_junk(tmp_path):
    import gzip
    p = tmp_path / "2026-08-04.csv.gz"
    with gzip.open(str(p), "wt") as f:
        f.write("snapshot_ts_et,oi_date,ticker,spot,expiry,cp,strike,oi,iv_pct,bid,ask,last,volume\n")
        # Every row unusable: zero OI and sentinel IV.
        f.write("x,x,SPY,600,2026-08-21,call,600,0,-999,1,2,1.5,10\n")
    import gex
    assert validate.levels_from_archive(str(p), gex.Config(ticker="SPY")) is None
    assert validate.levels_from_archive(str(tmp_path / "missing.csv.gz"),
                                        gex.Config(ticker="SPY")) is None
