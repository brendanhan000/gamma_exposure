#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/token_audit.py -- diagnose WHY the Schwab token died.

Reads logs/token_audit.log (written by gex.py on every token read/write) and
looks for the specific patterns that revoke a token family early:

  STALE REUSE   A process READ token X, another process rotated to Y, and the
                first process later presented X. Schwab treats a superseded
                refresh token as a compromise and revokes the WHOLE family --
                including the token you just created. This is the classic cause
                of "died within hours" rather than at the documented 7 days.

  CONCURRENCY   Two PIDs touching the token within seconds of each other.

  RAPID ROTATE  Many writes in a short window, which usually means several
                processes are each refreshing independently.

Run this the moment a token dies -- while the evidence is fresh.

Usage:
    python3 scripts/token_audit.py            # verdict on recent activity
    python3 scripts/token_audit.py --all      # full journal
    python3 scripts/token_audit.py --hours 48
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gex  # noqa: E402

LINE = re.compile(
    r"^(?P<ts>\S+ \S+) pid=(?P<pid>\d+)\s+(?P<ev>\w+)\s+rt=(?P<rt>\S+)\s+"
    r"acc_exp=(?P<exp>\S+)\s*(?P<note>.*?)\s*\[(?P<cmd>[^\]]*)\]$")


def parse(path, hours):
    if not os.path.exists(path):
        return None
    cutoff = datetime.now() - timedelta(hours=hours)
    out = []
    with open(path) as f:
        for ln in f:
            m = LINE.match(ln.strip())
            if not m:
                continue
            d = m.groupdict()
            try:
                d["dt"] = datetime.strptime(d["ts"], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if d["dt"] >= cutoff:
                out.append(d)
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="Diagnose Schwab token expiry from the audit journal.")
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--all", action="store_true", help="print every journal line")
    p.add_argument("--log", default=gex.TOKEN_AUDIT_LOG)
    a = p.parse_args(argv)

    ev = parse(a.log, 10**6 if a.all else a.hours)
    if ev is None:
        print("No journal yet at {!r}.".format(a.log))
        print("It is written automatically on every live run. Use the tool once,")
        print("then re-run this after the next token failure.")
        return 1
    if not ev:
        print("No token activity in the last {}h.".format(a.hours))
        return 0

    print("=" * 78)
    print("TOKEN AUDIT   {} events   {} .. {}   (all times ET)".format(
        len(ev), ev[0]["ts"], ev[-1]["ts"]))
    print("=" * 78)
    # Show the CURRENT token in the same timezone as the journal. These were
    # previously printed in different zones (journal ET vs local time), which
    # made a login look like it happened BEFORE the failures it fixed.
    try:
        import json as _j
        tp = os.environ.get("SCHWAB_TOKEN_PATH", gex.DEFAULT_TOKEN_PATH)
        with open(tp) as f:
            cur = _j.load(f)
        ct = datetime.fromtimestamp(cur["creation_timestamp"], tz=gex._et_tz())
        age_h = (gex.now_et() - ct).total_seconds() / 3600.0
        print("  current token      : rt={}  created {} ET ({:.1f}h ago)".format(
            gex._token_fingerprint(cur), ct.strftime("%Y-%m-%d %H:%M:%S"), age_h))
        print("  (journal lines with a DIFFERENT rt= are superseded tokens)")
        print()
    except Exception:
        pass
    if a.all:
        for e in ev:
            print("  {ts}  pid={pid:<7} {ev:<6} rt={rt}  exp={exp:<9} {note} [{cmd}]".format(**e))
        print()

    pids = sorted({e["pid"] for e in ev})
    writes = [e for e in ev if e["ev"] == "WRITE"]
    rotations = []
    seen = None
    for e in ev:
        if e["rt"] != "none" and e["rt"] != seen:
            rotations.append(e)
            seen = e["rt"]

    print("  processes involved : {}  ({})".format(len(pids), ", ".join(pids[:8])))
    print("  token rotations    : {}".format(max(0, len(rotations) - 1)))
    print("  writes             : {}".format(len(writes)))
    print()

    problems = []

    # --- STALE REUSE: a PID uses a fingerprint that another PID already superseded
    first_seen = {}
    for e in ev:
        first_seen.setdefault(e["rt"], e["dt"])
    latest_at = {}
    for e in ev:
        latest_at[e["rt"]] = e["dt"]
    order = [r["rt"] for r in rotations]
    idx = {rt: i for i, rt in enumerate(order)}
    for e in ev:
        i = idx.get(e["rt"])
        if i is None or i >= len(order) - 1:
            continue
        newer_at = first_seen.get(order[i + 1])
        if newer_at and e["dt"] > newer_at + timedelta(seconds=2):
            problems.append(
                "STALE REUSE  pid={} used rt={} at {}, but rt={} had already "
                "superseded it at {}".format(e["pid"], e["rt"], e["ts"],
                                             order[i + 1], newer_at.strftime("%H:%M:%S")))

    # --- CONCURRENCY: only meaningful if a WRITE is involved. Schwab returns the
    # SAME refresh token on refresh (verified in the journal: the fingerprint is
    # unchanged across a WRITE), so two concurrent READS cannot invalidate each
    # other -- there is no rotation to go stale. Flagging those was a false
    # positive that pointed at the wrong cause.
    for i in range(len(ev) - 1):
        a_, b_ = ev[i], ev[i + 1]
        gap = (b_["dt"] - a_["dt"]).total_seconds()
        if (a_["pid"] != b_["pid"] and gap <= 5
                and "WRITE" in (a_["ev"], b_["ev"])):
            problems.append(
                "CONCURRENCY  pid={} and pid={} overlapped within {:.0f}s at {} "
                "WITH a token write".format(a_["pid"], b_["pid"], gap, b_["ts"]))

    # --- RAPID ROTATE
    for i in range(len(writes) - 2):
        span = (writes[i + 2]["dt"] - writes[i]["dt"]).total_seconds()
        if span <= 120:
            problems.append("RAPID ROTATE  3 writes within {:.0f}s around {}".format(
                span, writes[i]["ts"]))
            break

    # --- DIED WHILE IDLE: the signature of a SERVER-SIDE revocation.
    # Find the last successful refresh (a WRITE, or a READ whose access-token
    # expiry advanced), then the first read after it that still shows the OLD
    # expiry -- meaning every refresh since then failed. If no process touched
    # the token in between, no local race can be responsible.
    last_ok, first_stuck = None, None
    for e in ev:
        if e["ev"] == "WRITE":
            last_ok = e
    if last_ok:
        for e in ev:
            if e["dt"] > last_ok["dt"] and e["exp"] == last_ok["exp"]:
                first_stuck = e
                break
    if last_ok and first_stuck:
        gap_h = (first_stuck["dt"] - last_ok["dt"]).total_seconds() / 3600.0
        between = [e for e in ev if last_ok["dt"] < e["dt"] < first_stuck["dt"]]
        if gap_h < 24 * 6.5:            # died well inside the 7-day cap
            problems.append(
                "DIED WHILE IDLE  last successful refresh {} ({}), first failed "
                "refresh {} -- {:.1f}h later, with {} process events in between. "
                "No local race can explain this: SERVER-SIDE revocation."
                .format(last_ok["ts"], "rt=" + last_ok["rt"], first_stuck["ts"],
                        gap_h, len(between)))

    if len(rotations) <= 1 and writes:
        problems.append(
            "NO ROTATION  the refresh token fingerprint never changed across a "
            "write -- Schwab reuses the same refresh token, so 'stale token "
            "reuse' cannot be the cause here.")

    seen_msgs = []
    for m in problems:
        if m.split("  ")[0] not in [s.split("  ")[0] for s in seen_msgs]:
            seen_msgs.append(m)

    if seen_msgs:
        print("  *** FINDINGS ***")
        for m in seen_msgs[:6]:
            print("   - " + m)
        print()
        kinds = {m.split("  ")[0] for m in seen_msgs}
        if "DIED WHILE IDLE" in kinds:
            print("  This is NOT a local process problem. Most likely causes, in order:")
            print("   1. A login was STARTED but not completed. Approving the app on")
            print("      Schwab re-authorizes it and can invalidate existing refresh")
            print("      tokens -- even if you never pasted the redirect URL. Never")
            print("      begin a login you are not going to finish in one sitting.")
            print("   2. A second login elsewhere (another terminal, the /setup page,")
            print("      another machine) superseded this token family.")
            print("   3. A Schwab-side revocation on the app itself.")
            print()
            print("  Recovery:  gexps   (confirm nothing is running)")
            print("             gexauth --manual   (complete it in ONE go)")
        else:
            print("  Fix: ensure only ONE process uses the token at a time. Check for a")
            print("  stray server:   pgrep -fl 'server.py|gex.py'")
            print("  Then re-login:  gexauth --manual")
    else:
        print("  No stale-reuse, concurrency, or rapid-rotation pattern found.")
        print("  If the token still died, it was NOT a local race -- suspect the")
        print("  7-day cap, a second login elsewhere, or a Schwab-side revocation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
