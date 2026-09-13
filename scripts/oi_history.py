#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/oi_history.py -- analyze the chain archive written by gex.py.

The archive (chains/<TICKER>/<date>.csv.gz) is the one dataset no free vendor
sells you: a per-strike open-interest time series for your own underlyings.
Schwab serves only the current snapshot and OI is published once daily, so this
history can never be backfilled -- it only accrues from the day you start.

What this gives you that a single snapshot cannot:

  dOI  (change in open interest, day over day)
       Distinguishes positioning that is BUILDING RIGHT NOW from OI that has sat
       unchanged for weeks. A wall backed by fresh OI is a live battle; a wall
       backed by stale OI may already be hedged and inert.

  Aggressor lean (crude)
       Where OI ROSE and the last print sat near the ASK, customers were likely
       buying -> dealers sold -> dealers are SHORT that strike's gamma. Near the
       BID implies the reverse. This is a weak, one-snapshot-per-day proxy for
       paid open-close data, NOT a substitute for it -- treat it as a hint.
       The lean is graded by the OPENING RATIO dOI/volume: high means the day's
       flow mostly opened (trustworthy); low means mostly churn (suppressed).

Usage:
    python3 scripts/oi_history.py                      # inventory of the archive
    python3 scripts/oi_history.py --ticker SPY         # latest dOI report
    python3 scripts/oi_history.py --ticker QQQ --top 15
    python3 scripts/oi_history.py --ticker SPY --expiry 2026-08-21
"""
from __future__ import annotations

import argparse
import glob
import gzip
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gex  # noqa: E402


_f = gex._f


def load_day(path):
    """Load one archived snapshot -> {(expiry, cp, strike): row_dict}."""
    out = {}
    with gzip.open(path, "rt", newline="") as f:
        for r in csv.DictReader(f):
            key = (r["expiry"], r["cp"], _f(r["strike"]))
            out[key] = r
    return out


def archive_days(chain_dir, ticker):
    return sorted(glob.glob(os.path.join(chain_dir, ticker.upper(), "*.csv.gz")))


def inventory(chain_dir):
    tickers = sorted(d for d in os.listdir(chain_dir)
                     if os.path.isdir(os.path.join(chain_dir, d))) if os.path.isdir(chain_dir) else []
    if not tickers:
        print("No archive yet at {!r}.".format(chain_dir))
        print("It fills automatically on every live run (archiving is on by default):")
        print("    python3 gex.py --ticker SPY")
        return 1
    print("=" * 70)
    print("CHAIN ARCHIVE  ({})".format(os.path.abspath(chain_dir)))
    print("=" * 70)
    total = 0
    for t in tickers:
        files = archive_days(chain_dir, t)
        size = sum(os.path.getsize(f) for f in files) / 1e6
        total += size
        if files:
            print("  {:<6} {:>3} day(s)   {} .. {}   {:.1f} MB".format(
                t, len(files),
                os.path.basename(files[0])[:10], os.path.basename(files[-1])[:10], size))
    print("  {:<6} {:.1f} MB total".format("", total))
    print("\n  dOI needs >= 2 days per ticker. Keep the daily job running;")
    print("  this history cannot be backfilled from any source.")
    return 0


def delta_report(chain_dir, ticker, top, expiry_filter):
    files = archive_days(chain_dir, ticker)
    if len(files) < 2:
        print("Need at least 2 archived days for {} (have {}).".format(ticker, len(files)))
        print("Archive grows one file per day the tool runs live.")
        return 1

    prev_path, cur_path = files[-2], files[-1]
    prev, cur = load_day(prev_path), load_day(cur_path)
    d_prev = os.path.basename(prev_path)[:10]
    d_cur = os.path.basename(cur_path)[:10]

    spot = None
    for r in cur.values():
        spot = _f(r.get("spot"))
        if spot:
            break

    rows = []
    for key, r in cur.items():
        exp, cp, strike = key
        if expiry_filter and exp != expiry_filter:
            continue
        oi_now = _f(r.get("oi")) or 0.0
        oi_old = _f((prev.get(key) or {}).get("oi")) or 0.0
        d = oi_now - oi_old
        if d == 0:
            continue
        bid, ask, last = _f(r.get("bid")), _f(r.get("ask")), _f(r.get("last"))
        vol = _f(r.get("volume"))
        # Opening ratio: dOI / volume. OI can rise by AT MOST the opening volume,
        # so dOI ~= vol means nearly all flow was opening (high signal); dOI << vol
        # means mostly churn/closing (the aggressor lean is noise on that strike).
        # Only meaningful on OI increases (d > 0) with a usable volume print.
        open_ratio = (d / vol) if (d > 0 and vol and vol > 0) else None
        lean = ""
        if d > 0 and bid is not None and ask is not None and last is not None and ask > bid:
            pos = (last - bid) / (ask - bid)          # 0 = at bid, 1 = at ask
            if pos >= 0.65:
                lean = "cust BUY -> dealer SHORT gamma"
            elif pos <= 0.35:
                lean = "cust SELL -> dealer LONG gamma"
            # Grade the lean by how much of the day's flow actually opened.
            if lean and open_ratio is not None:
                if open_ratio >= 0.6:
                    lean += "  [HIGH conf: {:.0%} opened]".format(open_ratio)
                elif open_ratio >= 0.25:
                    lean += "  [med conf: {:.0%} opened]".format(open_ratio)
                else:
                    lean = ""                         # mostly churn -> suppress the lean
        rows.append((abs(d), d, exp, cp, strike, oi_now, lean, open_ratio))

    if not rows:
        print("No open-interest changes between {} and {}{}.".format(
            d_prev, d_cur, " for " + expiry_filter if expiry_filter else ""))
        return 0

    print("=" * 78)
    print("OPEN-INTEREST CHANGE  {}   {} -> {}".format(ticker.upper(), d_prev, d_cur))
    if spot:
        print("spot at latest snapshot: {:.2f}".format(spot))
    print("=" * 78)
    print("  {:<12}{:<6}{:>9}{:>12}{:>11}{:>8}   {}".format(
        "expiry", "type", "strike", "dOI", "OI now", "open%", "aggressor lean (crude)"))
    rows.sort(reverse=True)
    for _, d, exp, cp, strike, oi_now, lean, open_ratio in rows[:top]:
        orat = "{:>7.0%}".format(open_ratio) if open_ratio is not None else "{:>7}".format("-")
        print("  {:<12}{:<6}{:>9,.0f}{:>+12,.0f}{:>11,.0f}{}   {}".format(
            exp, cp, strike, d, oi_now, orat, lean))

    built = sum(r[1] for r in rows if r[1] > 0)
    cut = sum(r[1] for r in rows if r[1] < 0)
    print("-" * 78)
    print("  positions opened: {:+,.0f} contracts | closed: {:+,.0f} | net: {:+,.0f}"
          .format(built, cut, built + cut))
    print()
    print("  Fresh dOI marks live positioning; strikes with big OI but no dOI are")
    print("  stale inventory that may already be hedged. open% = dOI/volume: high")
    print("  means the day's flow mostly OPENED (lean is trustworthy); low means")
    print("  mostly churn, so the aggressor lean is suppressed as noise. The lean")
    print("  itself is a one-snapshot-per-day proxy -- a hint, not open-close data.")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Analyze the gex.py chain archive (dOI history).")
    p.add_argument("--ticker", default=None, help="SPY, QQQ, ... (omit for an inventory)")
    p.add_argument("--chain-dir", default=os.environ.get("GEX_CHAIN_DIR", gex.CHAIN_DIR))
    p.add_argument("--top", type=int, default=20, help="rows to show")
    p.add_argument("--expiry", default=None, help="restrict to one expiration (YYYY-MM-DD)")
    a = p.parse_args(argv)
    if not a.ticker:
        return inventory(a.chain_dir)
    return delta_report(a.chain_dir, a.ticker.upper().lstrip("$"), a.top, a.expiry)


if __name__ == "__main__":
    raise SystemExit(main())
