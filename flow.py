#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
flow.py -- intraday signed order-flow tracker for option contracts.

THE PROBLEM THIS SOLVES
-----------------------
gex.py must ASSUME which side of the book dealers are on (the put_sign = -1
convention), because Schwab carries no dealer-direction data. That assumption is
the model's single largest error source: flip the put sign and the gamma flip
stops existing at all.

A once-daily chain snapshot cannot help. Measured live, one SPY strike traded
39,407 contracts in a session while `lastSize` was 25 -- a single snapshot lets
you classify 0.06% of the day's volume and guess at the other 99.94%.

POLLING FIXES THE SAMPLING PROBLEM
----------------------------------
Between two snapshots, `totalVolume` rises by exactly the contracts traded in
that window. Classifying each window and volume-weighting turns one useless
observation into hundreds of real ones:

    window volume  = totalVolume(t) - totalVolume(t-1)      # what TRADED
    direction      = Lee-Ready on (last, bid, ask)          # who was AGGRESSOR
    signed flow    = window volume * direction              # accumulated

CLASSIFICATION (Lee & Ready 1991)
    quote rule : last ABOVE the mid -> buyer-initiated  (aggressor lifted offer)
                 last BELOW the mid -> seller-initiated (aggressor hit bid)
    tick rule  : exactly at mid is ambiguous -> fall back to the change in last
                 versus the previous window (uptick = buy, downtick = sell).

FROM AGGRESSOR TO DEALER SIGN
    Market makers POST liquidity; customers TAKE it. So a buyer-initiated trade
    is customer-buys / dealer-sells:

        net customer BUYING puts  -> dealers SHORT puts -> put_sign -1 (assumed)
        net customer SELLING puts -> dealers LONG  puts -> put_sign +1 (fails)

    The same logic applies to calls, and it can contradict the standard
    "dealers long calls" leg too -- which is exactly the point of measuring.

WHAT THIS IS NOT
    Sampling, not a tape. Between polls, offsetting trades net out and the
    classification uses the quote at the END of the window. It infers AGGRESSOR
    side, never counterparty identity -- only CBOE open-close data tells you
    whether the taker was a customer or another dealer. Treat the output as a
    measured estimate that replaces a pure guess, not as ground truth.

Usage:
    python3 flow.py --ticker SPY --interval 60          # track a session
    python3 flow.py --ticker SPY --report               # analyse what was tracked
    python3 flow.py --ticker SPY --report --expiry 2026-08-24
"""
from __future__ import annotations

import argparse
import csv
import gzip
import os
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gex  # noqa: E402

FLOW_DIR = "flow"
FLOW_COLUMNS = ["ts_et", "expiry", "cp", "strike", "dvolume", "sign", "signed",
                "last", "bid", "ask", "mid", "rule", "bid_size", "ask_size"]

# A trade within this fraction of the spread from the mid is treated as "at the
# mid" and handed to the tick rule. Guards against float noise on penny-wide
# 0DTE markets where mid is exactly between bid and ask.
MID_EPS = 0.10


def classify(last, bid, ask, prev_last=None):
    """Lee-Ready aggressor classification -> (sign, rule).

    sign: +1 buyer-initiated, -1 seller-initiated, 0 unclassifiable.
    """
    if last is None or bid is None or ask is None or ask < bid:
        return 0, "no-quote"
    spread = ask - bid
    if spread <= 0:
        # Locked market: only the tick rule can say anything.
        if prev_last is not None and last != prev_last:
            return (1, "tick") if last > prev_last else (-1, "tick")
        return 0, "locked"
    mid = 0.5 * (bid + ask)
    pos = (last - mid) / spread          # ~ -0.5 .. +0.5
    if pos > MID_EPS:
        return 1, "quote"
    if pos < -MID_EPS:
        return -1, "quote"
    # At the mid -> tick rule against the previous window's print.
    if prev_last is not None and last != prev_last:
        return (1, "tick") if last > prev_last else (-1, "tick")
    return 0, "at-mid"


def snapshot_rows(data):
    """Flatten a raw Schwab chain into {key: quote-dict} for flow differencing."""
    out = {}
    for mp, cp in (("callExpDateMap", "call"), ("putExpDateMap", "put")):
        for exp_key, by_strike in (data.get(mp) or {}).items():
            exp = str(exp_key).split(":")[0]
            for strike_str, opts in by_strike.items():
                for o in opts:
                    try:
                        K = float(o.get("strikePrice", strike_str))
                    except (TypeError, ValueError):
                        continue
                    f = lambda k: (float(o[k]) if o.get(k) is not None else None)
                    try:
                        out[(exp, cp, K)] = {
                            "volume": float(o.get("totalVolume") or 0.0),
                            "last": f("last"), "bid": f("bid"), "ask": f("ask"),
                            "bid_size": o.get("bidSize"), "ask_size": o.get("askSize"),
                        }
                    except (TypeError, ValueError):
                        continue
    return out


def window_flow(prev, cur):
    """Signed flow for every contract that traded between two snapshots.

    Only contracts whose cumulative volume ROSE are emitted: no trade, no row.
    A volume DROP means the counter reset (new session) and is skipped rather
    than recorded as negative activity.
    """
    rows = []
    for key, c in cur.items():
        p = prev.get(key)
        if not p:
            continue
        dvol = c["volume"] - p["volume"]
        if dvol <= 0:
            continue
        sign, rule = classify(c["last"], c["bid"], c["ask"], p.get("last"))
        mid = (0.5 * (c["bid"] + c["ask"])
               if (c["bid"] is not None and c["ask"] is not None) else None)
        rows.append({
            "expiry": key[0], "cp": key[1], "strike": key[2],
            "dvolume": dvol, "sign": sign, "signed": dvol * sign,
            "last": c["last"], "bid": c["bid"], "ask": c["ask"], "mid": mid,
            "rule": rule, "bid_size": c["bid_size"], "ask_size": c["ask_size"],
        })
    return rows


def append_rows(path, rows, ts):
    """Append one window's rows to the day's gzipped CSV (header on creation)."""
    if not rows:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    new = not os.path.exists(path)
    with gzip.open(path, "at", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(FLOW_COLUMNS)
        for r in rows:
            w.writerow([ts, r["expiry"], r["cp"], "{:g}".format(r["strike"]),
                        "{:.0f}".format(r["dvolume"]), r["sign"],
                        "{:.0f}".format(r["signed"]), r["last"], r["bid"], r["ask"],
                        "" if r["mid"] is None else "{:.4f}".format(r["mid"]),
                        r["rule"], r["bid_size"], r["ask_size"]])


def flow_path(ticker, day=None, flow_dir=FLOW_DIR):
    day = day or gex.now_et().date()
    return os.path.join(flow_dir, ticker.upper().lstrip("$"), day.isoformat() + ".csv.gz")


def track(ticker, interval, flow_dir=FLOW_DIR, all_days=45, max_polls=None):
    """Poll the chain and accumulate signed flow until interrupted."""
    app_key = os.environ.get("SCHWAB_APP_KEY")
    app_secret = os.environ.get("SCHWAB_APP_SECRET")
    token = os.environ.get("SCHWAB_TOKEN_PATH", gex.DEFAULT_TOKEN_PATH)
    client = gex.get_schwab_client(app_key, app_secret, token)
    symbol = gex.to_schwab_symbol(ticker)
    today = gex.now_et().date()
    path = flow_path(ticker, today, flow_dir)

    print("Tracking {} every {}s -> {}".format(symbol, interval, path))
    print("Lee-Ready on window volume. Ctrl-C to stop.\n")
    print("{:<10}{:>9}{:>12}{:>12}{:>9}  {}".format(
        "time", "traded", "net signed", "cum signed", "contracts", "lean"))

    prev, cum, polls = None, 0.0, 0
    try:
        while max_polls is None or polls < max_polls:
            try:
                data = gex.fetch_chain_schwab(client, symbol, from_date=today,
                                              to_date=today + timedelta(days=all_days))
                cur = snapshot_rows(data)
                if prev is not None:
                    rows = window_flow(prev, cur)
                    ts = gex.now_et().strftime("%Y-%m-%dT%H:%M:%S")
                    append_rows(path, rows, ts)
                    traded = sum(r["dvolume"] for r in rows)
                    net = sum(r["signed"] for r in rows)
                    cum += net
                    lean = ("customers BUYING" if net > 0 else
                            "customers SELLING" if net < 0 else "balanced")
                    print("{:<10}{:>9,.0f}{:>+12,.0f}{:>+12,.0f}{:>9}  {}".format(
                        gex.now_et().strftime("%H:%M:%S"), traded, net, cum,
                        len(rows), lean))
                prev = cur
                polls += 1
            except Exception as e:
                print("{}  poll failed: {}".format(
                    gex.now_et().strftime("%H:%M:%S"), str(e)[:70]))
                if "token" in str(e).lower() or "OAuth" in type(e).__name__:
                    print("  -> re-authenticate:  gexauth --manual")
                    return 1
            if max_polls is None or polls < max_polls:
                time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


def load_flow(path):
    if not os.path.exists(path):
        return []
    with gzip.open(path, "rt", newline="") as f:
        return list(csv.DictReader(f))


def report(ticker, flow_dir=FLOW_DIR, day=None, expiry=None, top=15, spot=None):
    """Aggregate a session's signed flow and derive the EMPIRICAL dealer sign."""
    path = flow_path(ticker, day, flow_dir)
    rows = load_flow(path)
    if not rows:
        print("No flow recorded at {!r}.".format(path))
        print("Track a session first:  python3 flow.py --ticker {} --interval 60"
              .format(ticker))
        return 1
    if expiry:
        rows = [r for r in rows if r["expiry"] == expiry]
        if not rows:
            print("No flow for expiry {}.".format(expiry))
            return 1

    per = defaultdict(lambda: {"vol": 0.0, "signed": 0.0})
    by_cp = defaultdict(lambda: {"vol": 0.0, "signed": 0.0})
    rules = defaultdict(int)
    for r in rows:
        k = (r["expiry"], r["cp"], float(r["strike"]))
        d, s = float(r["dvolume"]), float(r["signed"])
        per[k]["vol"] += d
        per[k]["signed"] += s
        by_cp[r["cp"]]["vol"] += d
        by_cp[r["cp"]]["signed"] += s
        rules[r["rule"]] += 1

    total_vol = sum(v["vol"] for v in by_cp.values())
    print("=" * 78)
    print("INTRADAY SIGNED FLOW  {}   {}".format(
        ticker.upper(), os.path.basename(path)[:10]))
    print("=" * 78)
    print("  windows recorded : {:,}   contracts traded: {:,.0f}".format(
        len(rows), total_vol))
    print("  classification   : " + ", ".join(
        "{} {:.0%}".format(k, v / len(rows)) for k, v in sorted(
            rules.items(), key=lambda kv: -kv[1])))
    print()

    print("  {:<6}{:>14}{:>16}{:>10}   {}".format(
        "side", "volume", "net signed", "net %", "customer lean"))
    for cp in ("call", "put"):
        v = by_cp.get(cp)
        if not v or v["vol"] == 0:
            continue
        pct = v["signed"] / v["vol"]
        lean = "BUYING" if v["signed"] > 0 else "SELLING" if v["signed"] < 0 else "flat"
        print("  {:<6}{:>14,.0f}{:>+16,.0f}{:>9.1%}   {}".format(
            cp, v["vol"], v["signed"], pct, lean))
    print()

    # ---- the payoff: empirical dealer sign vs the assumed convention ----
    print("-" * 78)
    print("EMPIRICAL DEALER SIGN  (measured, not assumed)")
    print("-" * 78)
    print("  Market makers post liquidity and customers take it, so a")
    print("  buyer-initiated trade means customer BUYS / dealer SELLS.")
    print()
    verdict = {}
    for cp, assumed in (("call", +1.0), ("put", -1.0)):
        v = by_cp.get(cp)
        if not v or v["vol"] == 0:
            continue
        # Customers net buying -> dealers net short that side -> sign -1.
        empirical = -1.0 if v["signed"] > 0 else +1.0
        verdict[cp] = empirical
        agree = "AGREES with" if empirical == assumed else "CONTRADICTS"
        print("  {:<5} customers net {:<8} -> dealers {:<5} -> {}_sign {:+.0f}"
              "   [{} the assumed {:+.0f}]".format(
                  cp, "BUYING" if v["signed"] > 0 else "SELLING",
                  "SHORT" if empirical < 0 else "LONG", cp, empirical,
                  agree, assumed))
    print()
    if verdict.get("put") == -1.0:
        print("  -> The put_sign = -1 assumption is SUPPORTED by this session's flow.")
    elif verdict.get("put") == 1.0:
        print("  -> The put_sign = -1 assumption is CONTRADICTED. Re-run gex.py with")
        print("     --put-sign +1 and compare the flip; this is exactly the")
        print("     mis-specification the LOW CONFIDENCE band is measuring.")
    print()

    print("  Top contracts by |net signed volume|:")
    print("  {:<12}{:<6}{:>9}{:>12}{:>14}{:>9}".format(
        "expiry", "type", "strike", "volume", "net signed", "net %"))
    ranked = sorted(per.items(), key=lambda kv: -abs(kv[1]["signed"]))[:top]
    for (exp, cp, K), v in ranked:
        print("  {:<12}{:<6}{:>9,.0f}{:>12,.0f}{:>+14,.0f}{:>9.0%}".format(
            exp, cp, K, v["vol"], v["signed"],
            v["signed"] / v["vol"] if v["vol"] else 0))
    print()
    print("  Sampling, not a tape: offsetting trades between polls net out and")
    print("  direction uses the quote at each window's end. This infers AGGRESSOR")
    print("  side, never counterparty identity -- poll faster for a finer estimate.")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Intraday signed option order-flow tracker (empirical dealer sign).")
    p.add_argument("--ticker", default="SPY")
    p.add_argument("--interval", type=int, default=60, help="seconds between polls")
    p.add_argument("--all-days", type=int, default=45)
    p.add_argument("--flow-dir", default=os.environ.get("GEX_FLOW_DIR", FLOW_DIR))
    p.add_argument("--report", action="store_true", help="analyse instead of tracking")
    p.add_argument("--expiry", default=None, help="restrict the report to one expiry")
    p.add_argument("--date", default=None, help="report a past session (YYYY-MM-DD)")
    p.add_argument("--top", type=int, default=15)
    a = p.parse_args(argv)

    day = date.fromisoformat(a.date) if a.date else None
    if a.report:
        return report(a.ticker, a.flow_dir, day, a.expiry, a.top)
    if not (os.environ.get("SCHWAB_APP_KEY") and os.environ.get("SCHWAB_APP_SECRET")):
        print("ERROR: SCHWAB_APP_KEY / SCHWAB_APP_SECRET not set.", file=sys.stderr)
        return 2
    return track(a.ticker, a.interval, a.flow_dir, a.all_days)


if __name__ == "__main__":
    raise SystemExit(main())
