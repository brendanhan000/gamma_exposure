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

  Empirical dealer sign (--open-close)
       Ingest a CBOE Open-Close CSV (customer opening buy/sell per strike) and
       compute the EMPIRICAL dealer put/call sign: net customer put buying means
       dealers are SHORT those puts. This directly tests the model's put_sign=-1
       assumption instead of taking it on faith.

Usage:
    python3 scripts/oi_history.py                      # inventory of the archive
    python3 scripts/oi_history.py --ticker SPY         # latest dOI report
    python3 scripts/oi_history.py --ticker QQQ --top 15
    python3 scripts/oi_history.py --ticker SPY --expiry 2026-08-21
    python3 scripts/oi_history.py --ticker SPY --open-close cboe_oc.csv
"""
from __future__ import annotations

import argparse
import glob
import gzip
import csv
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gex  # noqa: E402


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load_day(path):
    """Load one archived snapshot -> {(expiry, cp, strike): row_dict}."""
    out = {}
    with gzip.open(path, "rt", newline="") as f:
        for r in csv.DictReader(f):
            key = (r["expiry"], r["cp"], _f(r["strike"]))
            out[key] = r
    return out


def archive_days(chain_dir, ticker):
    files = sorted(glob.glob(os.path.join(chain_dir, ticker.upper(), "*.csv.gz")))
    return files


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


# ---------------------------------------------------------------------------
# CBOE open-close ingestion -> empirical dealer put sign
# ---------------------------------------------------------------------------
# The chain archive says WHERE open interest sits; it cannot say WHO is long it.
# CBOE Open-Close data can: it splits each day's volume into opening/closing x
# buy/sell x origin (customer / firm / market-maker). Net CUSTOMER opening put
# buying means dealers took the other side -> dealers are SHORT that strike's
# put gamma. That is the empirical test of the model's put_sign = -1 assumption.
#
# Expected CSV (one row per option, flexible column names -- see _oc_col):
#   underlying, expiry, strike, cp,
#   cust_open_buy, cust_open_sell, mm_open_buy, mm_open_sell, ...
# Only the CUSTOMER opening columns are required; the rest are optional. Sign of
# the dealer put position is inferred from net customer opening (customers and
# dealers are on opposite sides of opening customer flow).

# Accepted spellings for each required column (lowercased, punctuation stripped).
_OC_FIELDS = {
    "underlying":   ["underlying", "symbol", "ticker", "root"],
    "expiry":       ["expiry", "expiration", "expirationdate", "expdate", "exp"],
    "strike":       ["strike", "strikeprice", "strikepx"],
    "cp":           ["cp", "callput", "putcall", "optiontype", "type", "right"],
    "cust_open_buy":  ["custopenbuy", "customeropenbuy", "custbuyopen",
                       "customerbuyopen", "openbuy customer", "buyopenqtycust"],
    "cust_open_sell": ["custopensell", "customeropensell", "custsellopen",
                       "customersellopen", "opensellcustomer", "sellopenqtycust"],
}


def _norm_col(c):
    return "".join(ch for ch in c.lower() if ch.isalnum())


def _oc_col(fieldnames, canonical):
    """Find the actual CSV column matching a canonical field, or None."""
    norm = {_norm_col(f): f for f in fieldnames}
    for spelling in _OC_FIELDS.get(canonical, []):
        if _norm_col(spelling) in norm:
            return norm[_norm_col(spelling)]
    return None


def load_open_close(path):
    """Load a CBOE open-close CSV -> list of dicts with canonical keys.

    Raises ValueError listing the missing columns if any required field is absent.
    """
    with open(path, newline="") as f:
        rdr = csv.DictReader(f)
        fields = rdr.fieldnames or []
        cols = {k: _oc_col(fields, k) for k in _OC_FIELDS}
        missing = [k for k, v in cols.items() if v is None]
        if missing:
            raise ValueError(
                "open-close CSV is missing required column(s): {}. "
                "Accepted spellings: {}".format(
                    ", ".join(missing),
                    {k: _OC_FIELDS[k] for k in missing}))
        out = []
        for row in rdr:
            cp_raw = (row.get(cols["cp"]) or "").strip().upper()
            cp = "call" if cp_raw.startswith("C") else "put" if cp_raw.startswith("P") else None
            if cp is None:
                continue
            out.append({
                "underlying": (row.get(cols["underlying"]) or "").strip().upper().lstrip("$"),
                "expiry": (row.get(cols["expiry"]) or "").strip().split(" ")[0],
                "strike": _f(row.get(cols["strike"])),
                "cp": cp,
                "cust_open_buy": _f(row.get(cols["cust_open_buy"])) or 0.0,
                "cust_open_sell": _f(row.get(cols["cust_open_sell"])) or 0.0,
            })
    return out


def dealer_sign_report(open_close_rows, ticker, top, spot=None):
    """Empirical dealer put/call sign per strike from customer opening flow.

    Net customer opening = cust_open_buy - cust_open_sell. Customers and dealers
    sit on opposite sides of opening customer flow, so:
        net customer put BUYING  > 0  -> dealers SHORT those puts  (put_sign -1, as assumed)
        net customer put SELLING > 0  -> dealers LONG those puts   (put_sign +1, assumption FAILS)
    Returns (rows, summary) where rows are sorted by |net customer opening|.
    """
    agg = defaultdict(lambda: {"cob": 0.0, "cos": 0.0})
    und_t = ticker.upper().lstrip("$")
    for r in open_close_rows:
        if r["underlying"] != und_t or r["strike"] is None:
            continue
        k = (r["expiry"], r["cp"], r["strike"])
        agg[k]["cob"] += r["cust_open_buy"]
        agg[k]["cos"] += r["cust_open_sell"]

    rows = []
    for (exp, cp, strike), v in agg.items():
        net = v["cob"] - v["cos"]                 # net customer opening (contracts)
        if net == 0:
            continue
        # Dealer sign is the OPPOSITE of the customer opening side.
        dealer_sign = -1.0 if net > 0 else 1.0    # dealers short if customers net-bought
        rows.append((abs(net), exp, cp, strike, net, dealer_sign))
    rows.sort(reverse=True)

    # Summarize the put side separately -- that is the assumption under test.
    put_rows = [r for r in rows if r[2] == "put"]
    n_short = sum(1 for r in put_rows if r[5] < 0)
    n_long = sum(1 for r in put_rows if r[5] > 0)
    net_put_flow = sum(r[4] for r in put_rows)    # >0 = customers net-bought puts
    summary = {
        "n_put_strikes": len(put_rows),
        "n_put_dealer_short": n_short,
        "n_put_dealer_long": n_long,
        "net_customer_put_opening": net_put_flow,
        # Assumption holds if customers net-bought puts (dealers short) overall.
        "assumption_holds": net_put_flow > 0,
    }
    return rows, summary


def print_dealer_sign_report(open_close_path, ticker, top):
    """Render the empirical dealer-sign report for one underlying."""
    try:
        rows_raw = load_open_close(open_close_path)
    except (OSError, ValueError) as e:
        print("ERROR: {}".format(e))
        return 2
    rows, s = dealer_sign_report(rows_raw, ticker, top)
    if not rows:
        print("No open-close rows matched {}.".format(ticker.upper()))
        return 1

    print("=" * 78)
    print("EMPIRICAL DEALER SIGN  {}   (from CBOE open-close: {})".format(
        ticker.upper(), os.path.basename(open_close_path)))
    print("=" * 78)
    print("  Sign convention: customers and dealers are on OPPOSITE sides of opening")
    print("  customer flow. Net customer put BUYING -> dealers SHORT those puts.")
    print()
    print("  {:<12}{:<6}{:>10}{:>18}   {}".format(
        "expiry", "type", "strike", "net cust opening", "empirical dealer sign"))
    for _, exp, cp, strike, net, dsign in rows[:top]:
        side = "SHORT gamma" if dsign < 0 else "LONG gamma"
        print("  {:<12}{:<6}{:>10,.0f}{:>+18,.0f}   dealer {}".format(
            exp, cp, strike, net, side))

    print("-" * 78)
    print("  PUT SIDE (the put_sign = -1 assumption under test):")
    print("    put strikes with dealers SHORT: {}   LONG: {}".format(
        s["n_put_dealer_short"], s["n_put_dealer_long"]))
    print("    net customer put opening: {:+,.0f} contracts".format(s["net_customer_put_opening"]))
    if s["assumption_holds"]:
        print("    => customers NET-BOUGHT puts: dealers are net SHORT put gamma.")
        print("       The standard put_sign = -1 assumption is SUPPORTED by this data.")
    else:
        print("    => customers NET-SOLD puts: dealers are net LONG put gamma here.")
        print("       The standard put_sign = -1 assumption FAILS on this snapshot --")
        print("       consider --put-sign +1 or treat the flip as LOW confidence.")
    print()
    print("  Caveat: CBOE captures only its own exchanges' share of volume (multi-")
    print("  listed options trade on up to 17 venues). Directionally informative,")
    print("  not the whole market.")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Analyze the gex.py chain archive (dOI history).")
    p.add_argument("--ticker", default=None, help="SPY, QQQ, ... (omit for an inventory)")
    p.add_argument("--chain-dir", default=os.environ.get("GEX_CHAIN_DIR", gex.CHAIN_DIR))
    p.add_argument("--top", type=int, default=20, help="rows to show")
    p.add_argument("--expiry", default=None, help="restrict to one expiration (YYYY-MM-DD)")
    p.add_argument("--open-close", default=None, metavar="CSV",
                   help="CBOE open-close CSV: compute the EMPIRICAL dealer put/call "
                        "sign per strike (tests the put_sign = -1 assumption).")
    a = p.parse_args(argv)
    if a.open_close:
        if not a.ticker:
            print("ERROR: --open-close requires --ticker.")
            return 2
        return print_dealer_sign_report(a.open_close, a.ticker.upper().lstrip("$"), a.top)
    if not a.ticker:
        return inventory(a.chain_dir)
    return delta_report(a.chain_dir, a.ticker.upper().lstrip("$"), a.top, a.expiry)


if __name__ == "__main__":
    raise SystemExit(main())
