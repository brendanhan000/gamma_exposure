#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/validate.py -- does the model actually predict anything?

Everything else in this project improves the ESTIMATE. This asks whether the
estimate is USEFUL, by replaying the chain archive and scoring the levels
against what price subsequently did.

TWO TESTS
=========

1. OVERNIGHT vs RTH  (the falsifiable one)
   Options hedging happens in regular hours: SPX/SPY are closed overnight while
   the underlying still gaps on news. If the gamma regime is really transmitting
   through dealer hedging, its effect must show up in the RTH session and NOT
   overnight. Overnight is therefore a natural CONTROL GROUP -- the mechanism is
   switched off, but every other driver of volatility is still present.

       effect in RTH but not overnight  -> consistent with the hedging mechanism
       effect in BOTH, equally          -> the levels are probably just proxying
                                           general volatility, not dealer gamma
       effect in NEITHER                -> no measurable predictive content

   This is the test that can actually embarrass the model, which is why it is
   worth running.

2. LEVEL VALIDATION
   * regime vs realized range: are short-gamma sessions genuinely wider?
   * wall containment: does the session high respect the call wall, the low the
     put wall -- more often than the walls' distance alone would imply?

NO LOOK-AHEAD
    A snapshot taken any time on day D is known by the close of D, so every
    level is scored against the NEXT session (D+1). Nothing is graded on data
    that was unavailable when the level was computed.

HONEST STATISTICS
    Sample size is printed with every result and no claim of significance is
    made below MIN_N sessions. With a handful of days this reports "not enough
    data" rather than a p-value that would only be noise.

Usage:
    python3 scripts/validate.py --ticker SPY
    python3 scripts/validate.py --ticker QQQ --min-n 20
"""
from __future__ import annotations

import argparse
import csv
import glob
import gzip
import math
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import NormalDist, fmean, variance

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gex  # noqa: E402

MIN_N = 20          # below this, report descriptives only -- never a verdict


# ---------------------------------------------------------------------------
# Replay: recompute levels from an archived chain snapshot
# ---------------------------------------------------------------------------
def levels_from_archive(path, cfg, now=None):
    """Recompute flip / walls / net GEX from one archived day. None if unusable."""
    contracts, spot = [], None
    try:
        with gzip.open(path, "rt", newline="") as f:
            for r in csv.DictReader(f):
                if spot is None:
                    try:
                        spot = float(r["spot"])
                    except (TypeError, ValueError, KeyError):
                        pass
                try:
                    oi = float(r["oi"] or 0)
                    iv = float(r["iv_pct"] or 0) / 100.0
                    if oi <= 0 or iv <= 0 or not math.isfinite(iv):
                        continue
                    contracts.append(gex.Contract(
                        float(r["strike"]), date.fromisoformat(r["expiry"]),
                        r["cp"], oi, iv,
                        volume=float(r["volume"] or 0)))
                except (TypeError, ValueError, KeyError):
                    continue
    except OSError:
        return None
    if not contracts or not spot:
        return None

    snap_day = date.fromisoformat(os.path.basename(path)[:10])
    # Value T as of the snapshot's own session close, so a replayed day is
    # priced the way it was seen, not as of today.
    asof = datetime(snap_day.year, snap_day.month, snap_day.day, 9, 45,
                    tzinfo=gex.ET)
    usable, _dropped, _floored = gex.enrich_and_filter_time(contracts, asof)
    if not usable:
        return None
    view = gex.compute_view(usable, spot, cfg, now=asof)
    if view.get("empty"):
        return None
    return {
        "date": snap_day, "spot": spot,
        "flip": view["flip_std"]["flip"],
        "call_wall": view["walls"]["call_wall"],
        "put_wall": view["walls"]["put_wall"],
        "net_gex": view["total"],
        # Sign of net GEX at spot decides the regime (correct on inverted chains);
        # spot vs flip is only the fallback when the total is unavailable.
        "regime": gex.regime_word(spot, view["flip_std"]["flip"],
                                  view["flip_std"].get("total_at_spot")),
    }


def load_levels(chain_dir, ticker, cfg):
    out = []
    for p in sorted(glob.glob(os.path.join(chain_dir, ticker.upper(), "*.csv.gz"))):
        lv = levels_from_archive(p, cfg)
        if lv:
            out.append(lv)
    return out


# ---------------------------------------------------------------------------
# Outcomes: daily OHLC for the underlying
# ---------------------------------------------------------------------------
def fetch_ohlc(ticker, start, end):
    """{date: (open, high, low, close)} from Schwab daily candles."""
    client = gex.get_schwab_client()
    resp = client.get_price_history_every_day(
        gex.to_schwab_symbol(ticker),
        start_datetime=datetime(start.year, start.month, start.day),
        end_datetime=datetime(end.year, end.month, end.day),
        need_extended_hours_data=False)
    resp.raise_for_status()
    out = {}
    for c in (resp.json().get("candles") or []):
        d = datetime.fromtimestamp(c["datetime"] / 1000.0).date()
        out[d] = (c["open"], c["high"], c["low"], c["close"])
    return out


def build_panel(levels, ohlc):
    """Join levels on day D to outcomes on the NEXT session (no look-ahead)."""
    days = sorted(ohlc)
    rows = []
    for lv in levels:
        later = [d for d in days if d > lv["date"]]
        if not later or lv["date"] not in ohlc:
            continue
        nxt = later[0]
        o, h, l, c = ohlc[nxt]
        prev_close = ohlc[lv["date"]][3]
        if not (o and prev_close):
            continue
        rows.append({
            **lv, "next": nxt, "open": o, "high": h, "low": l, "close": c,
            "prev_close": prev_close,
            # Overnight: prior close -> next open (options CLOSED, control group)
            "overnight_ret": o / prev_close - 1.0,
            # RTH: open -> close (options OPEN, hedging active)
            "rth_ret": c / o - 1.0,
            "rth_range": (h - l) / o if o else None,
        })
    return rows


# ---------------------------------------------------------------------------
# Statistics (dependency-light; honest about small samples)
# ---------------------------------------------------------------------------
def welch_t(a, b):
    """Welch's t and a normal-approximation two-sided p. None if degenerate."""
    if len(a) < 2 or len(b) < 2:
        return None, None
    se = math.sqrt(variance(a) / len(a) + variance(b) / len(b))
    if not se:
        return None, None
    t = (fmean(a) - fmean(b)) / se
    return t, 2.0 * (1.0 - NormalDist().cdf(abs(t)))


def _verdict(t, p, n_a, n_b, min_n):
    if t is None:
        return "insufficient data"
    if min(n_a, n_b) < min_n:
        return "n too small for a verdict (need {}+ per group)".format(min_n)
    if p < 0.05:
        return "SIGNIFICANT (p={:.3f})".format(p)
    return "not significant (p={:.3f})".format(p)


# ---------------------------------------------------------------------------
# Test 1: overnight vs RTH  (the control-group test)
# ---------------------------------------------------------------------------
def test_overnight_vs_rth(rows, min_n=MIN_N):
    short = [r for r in rows if "SHORT" in r["regime"]]
    long_ = [r for r in rows if "LONG" in r["regime"]]
    print("=" * 78)
    print("TEST 1  OVERNIGHT vs RTH   (options hedging is OFF overnight)")
    print("=" * 78)
    print("  Prediction: if the regime works through dealer hedging, it should move")
    print("  RTH volatility and NOT overnight volatility. Overnight is the control.")
    print()
    print("  sessions: {} SHORT gamma, {} LONG gamma".format(len(short), len(long_)))
    if not short or not long_:
        print("  Need both regimes present to compare. Keep archiving.")
        print()
        return None

    out = {}
    print()
    print("  {:<26}{:>12}{:>12}{:>10}   {}".format(
        "|move| by window", "SHORT", "LONG", "t", "verdict"))
    for label, key in (("OVERNIGHT (control)", "overnight_ret"),
                       ("RTH (mechanism live)", "rth_ret")):
        a = [abs(r[key]) for r in short if r[key] is not None]
        b = [abs(r[key]) for r in long_ if r[key] is not None]
        t, p = welch_t(a, b)
        out[key] = (fmean(a), fmean(b), t, p)
        print("  {:<26}{:>11.3%}{:>12.3%}{:>10}   {}".format(
            label, fmean(a), fmean(b),
            "{:+.2f}".format(t) if t is not None else "-",
            _verdict(t, p, len(a), len(b), min_n)))

    a = [r["rth_range"] for r in short if r["rth_range"] is not None]
    b = [r["rth_range"] for r in long_ if r["rth_range"] is not None]
    t, p = welch_t(a, b)
    out["rth_range"] = (fmean(a), fmean(b), t, p)
    print("  {:<26}{:>11.3%}{:>12.3%}{:>10}   {}".format(
        "RTH range (high-low)", fmean(a), fmean(b),
        "{:+.2f}".format(t) if t is not None else "-",
        _verdict(t, p, len(a), len(b), min_n)))

    print()
    on_t = out["overnight_ret"][2]
    rth_t = out["rth_ret"][2]
    if on_t is not None and rth_t is not None:
        print("  Reading it:")
        if abs(rth_t) > abs(on_t) * 1.5:
            print("    RTH effect exceeds overnight -- CONSISTENT with hedging transmission.")
        elif abs(on_t) > abs(rth_t) * 1.5:
            print("    Overnight effect exceeds RTH -- INCONSISTENT with the hedging story;")
            print("    the regime is likely proxying general volatility, not dealer gamma.")
        else:
            print("    Effects are comparable -- the regime may just be tracking overall vol.")
        if min(len(short), len(long_)) < min_n:
            print("    (Directional only: the sample is far too small to conclude anything.)")
    print()
    return out


# ---------------------------------------------------------------------------
# Test 2: do the levels hold?
# ---------------------------------------------------------------------------
def test_levels(rows, min_n=MIN_N):
    print("=" * 78)
    print("TEST 2  LEVEL VALIDATION   (do walls contain? does the flip mark regime?)")
    print("=" * 78)
    if not rows:
        print("  No joined sessions.")
        return None

    held_c = [r for r in rows if r["call_wall"] and r["high"] <= r["call_wall"]]
    held_p = [r for r in rows if r["put_wall"] and r["low"] >= r["put_wall"]]
    n_c = len([r for r in rows if r["call_wall"]])
    n_p = len([r for r in rows if r["put_wall"]])

    print("  Wall containment (next session's high/low vs the wall):")
    if n_c:
        print("    call wall held : {:>3}/{:<3} ({:.0%})   mean distance at compute: {:.2%}".format(
            len(held_c), n_c, len(held_c) / n_c,
            fmean([(r["call_wall"] - r["spot"]) / r["spot"] for r in rows if r["call_wall"]])))
    if n_p:
        print("    put wall held  : {:>3}/{:<3} ({:.0%})   mean distance at compute: {:.2%}".format(
            len(held_p), n_p, len(held_p) / n_p,
            fmean([(r["put_wall"] - r["spot"]) / r["spot"] for r in rows if r["put_wall"]])))
    print("    NOTE: a far-away wall 'holds' trivially. Containment is only")
    print("    meaningful against the distance shown -- a wall 3% away that holds")
    print("    on a 0.5% day tells you nothing.")
    print()

    # Directional: does spot move toward the flip when it is away from it?
    with_flip = [r for r in rows if r["flip"]]
    if with_flip:
        toward = 0
        for r in with_flip:
            gap = r["flip"] - r["spot"]
            move = r["close"] - r["open"]
            if gap * move > 0:
                toward += 1
        print("  Flip attraction: price closed TOWARD the flip on {}/{} sessions ({:.0%}).".format(
            toward, len(with_flip), toward / len(with_flip)))
        print("    Coin-flip is 50%. {}".format(
            "Too few sessions to distinguish from chance."
            if len(with_flip) < min_n else "Compare against 50%."))
    print()
    return {"call_held": (len(held_c), n_c), "put_held": (len(held_p), n_p)}


def main(argv=None):
    p = argparse.ArgumentParser(description="Validate GEX levels against realized outcomes.")
    p.add_argument("--ticker", default="SPY")
    p.add_argument("--chain-dir", default=os.environ.get("GEX_CHAIN_DIR", gex.CHAIN_DIR))
    p.add_argument("--min-n", type=int, default=MIN_N)
    p.add_argument("--rate", type=float, default=gex.DEFAULT_RATE)
    a = p.parse_args(argv)

    base = a.ticker.upper().lstrip("$")
    cfg = gex.Config(ticker=base, rate=a.rate,
                     div_yield=gex.TICKER_DIV_YIELDS.get(base, 0.0))
    levels = load_levels(a.chain_dir, base, cfg)
    if not levels:
        print("No archived chains for {} in {!r}.".format(base, a.chain_dir))
        print("The archive fills on every live run; validation needs history.")
        return 1
    print("Replayed {} archived session(s): {} .. {}\n".format(
        len(levels), levels[0]["date"], levels[-1]["date"]))

    try:
        ohlc = fetch_ohlc(base, levels[0]["date"] - timedelta(days=5),
                          levels[-1]["date"] + timedelta(days=5))
    except Exception as e:
        print("Could not fetch price history: {}".format(str(e)[:110]), file=sys.stderr)
        if "token" in str(e).lower():
            print("  -> gexauth --manual", file=sys.stderr)
        return 1

    rows = build_panel(levels, ohlc)
    if not rows:
        print("No sessions could be joined to a NEXT-day outcome yet.")
        print("Each archived day is scored against the following session, so the")
        print("most recent day always waits for tomorrow's close.")
        return 1
    print("Scored {} session(s) against the FOLLOWING session (no look-ahead).\n"
          .format(len(rows)))

    test_overnight_vs_rth(rows, a.min_n)
    test_levels(rows, a.min_n)

    print("-" * 78)
    if len(rows) < a.min_n:
        print("SAMPLE TOO SMALL: {} sessions. Nothing here is significant; the".format(len(rows)))
        print("numbers are descriptive only. Keep the daily job running -- this")
        print("becomes a real test at {}+ sessions per regime.".format(a.min_n))
    else:
        print("{} sessions scored. Treat single-ticker results as suggestive;".format(len(rows)))
        print("confirm on the other ticker before believing anything.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
