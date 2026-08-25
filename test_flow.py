# -*- coding: utf-8 -*-
"""Tests for flow.py -- intraday signed order-flow tracking. No network."""
import gzip
import csv
import datetime
import os

import pytest

import flow


# --------------------------------------------------------------------------
# Lee-Ready classification
# --------------------------------------------------------------------------
def test_quote_rule_classifies_by_side_of_mid():
    # Above the mid = the aggressor lifted the offer = buyer-initiated.
    assert flow.classify(2.08, 2.00, 2.10)[0] == 1
    assert flow.classify(2.02, 2.00, 2.10)[0] == -1
    # Exactly at the mid is ambiguous and must NOT be guessed by the quote rule.
    sign, rule = flow.classify(2.05, 2.00, 2.10)
    assert sign == 0 and rule == "at-mid"


def test_tick_rule_resolves_at_mid_trades():
    assert flow.classify(2.05, 2.00, 2.10, prev_last=2.03) == (1, "tick")
    assert flow.classify(2.05, 2.00, 2.10, prev_last=2.07) == (-1, "tick")
    # Unchanged price gives no information either way.
    assert flow.classify(2.05, 2.00, 2.10, prev_last=2.05)[0] == 0


def test_classify_never_crashes_on_bad_inputs():
    for args in ((None, 2.0, 2.1), (2.05, None, 2.1), (2.05, 2.0, None),
                 (2.05, 2.10, 2.00)):               # crossed
        sign, rule = flow.classify(*args)
        assert sign == 0 and isinstance(rule, str)
    # Locked market (bid == ask) falls through to the tick rule.
    assert flow.classify(2.0, 2.0, 2.0, prev_last=1.9) == (1, "tick")
    assert flow.classify(2.0, 2.0, 2.0)[0] == 0


# --------------------------------------------------------------------------
# Window differencing
# --------------------------------------------------------------------------
def _snap(vol, last, bid=2.00, ask=2.10):
    return {("2026-08-24", "put", 765.0):
            {"volume": vol, "last": last, "bid": bid, "ask": ask,
             "bid_size": 10, "ask_size": 10}}


def test_window_flow_counts_only_traded_volume():
    rows = flow.window_flow(_snap(1000, 2.05), _snap(1250, 2.08))
    assert len(rows) == 1
    assert rows[0]["dvolume"] == 250
    assert rows[0]["signed"] == 250          # printed at the ask -> buyer-initiated

    # No trade -> no row at all (the file must not fill with zero-volume noise).
    assert flow.window_flow(_snap(1000, 2.05), _snap(1000, 2.05)) == []


def test_window_flow_ignores_volume_resets_and_unknown_contracts():
    # A DROP in cumulative volume means a new session, not negative trading.
    assert flow.window_flow(_snap(5000, 2.05), _snap(10, 2.05)) == []
    # A contract absent from the previous snapshot has no baseline to difference.
    assert flow.window_flow({}, _snap(100, 2.05)) == []


def test_unclassified_trades_contribute_zero_not_a_guess():
    # At-mid with no prior print: volume is recorded, direction is not invented.
    rows = flow.window_flow(_snap(1000, 2.05), _snap(1100, 2.05))
    assert rows[0]["dvolume"] == 100
    assert rows[0]["sign"] == 0 and rows[0]["signed"] == 0


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------
def test_persistence_round_trip(tmp_path):
    p = str(tmp_path / "SPY" / "2026-08-24.csv.gz")
    flow.append_rows(p, flow.window_flow(_snap(1000, 2.05), _snap(1250, 2.08)), "T1")
    flow.append_rows(p, flow.window_flow(_snap(1250, 2.08), _snap(1400, 2.01)), "T2")
    back = flow.load_flow(p)
    assert len(back) == 2                                    # header written once
    assert set(back[0]) == set(flow.FLOW_COLUMNS)
    assert back[0]["sign"] == "1" and back[1]["sign"] == "-1"
    assert flow.load_flow(str(tmp_path / "nope.csv.gz")) == []


def test_flow_path_layout(tmp_path):
    p = flow.flow_path("$SPX", datetime.date(2026, 8, 24), str(tmp_path))
    assert p.endswith(os.path.join("SPX", "2026-08-24.csv.gz"))   # '$' stripped


# --------------------------------------------------------------------------
# The payoff: empirical dealer sign
# --------------------------------------------------------------------------
def _write_session(tmp_path, cp, near_ask, n=30):
    """Synthesize a session where customers consistently lift (or hit)."""
    day = datetime.date(2026, 8, 24)
    p = flow.flow_path("SPY", day, str(tmp_path))
    bid, ask = 2.00, 2.10
    last = ask if near_ask else bid
    sign, rule = flow.classify(last, bid, ask)
    for i in range(n):
        flow.append_rows(p, [{
            "expiry": "2026-08-24", "cp": cp, "strike": 765.0, "dvolume": 100,
            "sign": sign, "signed": 100 * sign, "last": last, "bid": bid,
            "ask": ask, "mid": 2.05, "rule": rule, "bid_size": 5, "ask_size": 5,
        }], "T{}".format(i))
    return day


def test_dealer_sign_supported_when_customers_buy_puts(tmp_path, capsys):
    # Customers lifting puts -> dealers SHORT puts -> put_sign -1 (as assumed).
    day = _write_session(tmp_path, "put", near_ask=True)
    assert flow.report("SPY", str(tmp_path), day) == 0
    out = capsys.readouterr().out
    assert "customers net BUYING" in out
    assert "dealers SHORT" in out
    assert "AGREES with" in out
    assert "SUPPORTED" in out


def test_dealer_sign_contradicted_when_customers_sell_puts(tmp_path, capsys):
    # Customers hitting the bid -> dealers LONG puts -> put_sign +1 (assumption fails).
    day = _write_session(tmp_path, "put", near_ask=False)
    assert flow.report("SPY", str(tmp_path), day) == 0
    out = capsys.readouterr().out
    assert "customers net SELLING" in out
    assert "dealers LONG" in out
    assert "CONTRADICTS" in out
    assert "--put-sign +1" in out       # tells the user what to actually do


def test_report_handles_missing_session(tmp_path, capsys):
    assert flow.report("SPY", str(tmp_path), datetime.date(2026, 1, 2)) == 1
    assert "No flow recorded" in capsys.readouterr().out


def test_balanced_session_is_inconclusive_not_a_contradiction(tmp_path, capsys):
    # signed == 0 carries no directional information; reporting "dealers LONG"
    # (the old else-branch) invented a verdict the data does not support.
    day = datetime.date(2026, 8, 24)
    p = flow.flow_path("SPY", day, str(tmp_path))
    rows = [
        {"expiry": "2026-08-24", "cp": "put", "strike": 765.0, "dvolume": 100,
         "sign": 1, "signed": 100, "last": 2.10, "bid": 2.0, "ask": 2.10,
         "mid": 2.05, "rule": "quote", "bid_size": 5, "ask_size": 5},
        {"expiry": "2026-08-24", "cp": "put", "strike": 765.0, "dvolume": 100,
         "sign": -1, "signed": -100, "last": 2.00, "bid": 2.0, "ask": 2.10,
         "mid": 2.05, "rule": "quote", "bid_size": 5, "ask_size": 5},
    ]
    flow.append_rows(p, rows, "T0")
    assert flow.report("SPY", str(tmp_path), day) == 0
    out = capsys.readouterr().out
    assert "no directional read" in out
    assert "CONTRADICTS" not in out and "AGREES with" not in out
