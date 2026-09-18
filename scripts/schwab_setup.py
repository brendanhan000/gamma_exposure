#!/usr/bin/env python3
"""Live verification of the Schwab data path for gex.py, via the central schwab_hub.

Login is no longer done here: the hub owns the token (`../schwab_hub/run.sh login`,
weekly). This script pulls a minimal chain slice and runs it through gex's parser,
confirming the response shape before you trust a full run.

Prereqs: the hub is running (`../schwab_hub/run.sh`) and `pip install -e ../schwab_hub`.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gex  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(description="Verify the Schwab option-chain path for gex.py.")
    p.add_argument("--ticker", default=gex.DEFAULT_TICKER,
                   help="symbol to verify against (SPX -> $SPX).")
    args = p.parse_args(argv)

    try:
        client = gex.get_schwab_client()
        client.get_quote("SPY").raise_for_status()
    except Exception as e:  # noqa: BLE001
        print("ERROR: {}".format(e), file=sys.stderr)
        return 2

    # Live verification: pull a minimal chain slice and run it through gex's
    #    parser, confirming the response shape before you trust a full run.
    symbol = gex.to_schwab_symbol(args.ticker)
    today = gex.now_et().date()
    to_date = today + timedelta(days=7)
    print("\n[pull] verifying a minimal {} chain ({} .. {}) ...".format(symbol, today, to_date))
    data = gex.fetch_chain_schwab(client, symbol, from_date=today, to_date=to_date, strike_count=2)
    if data.get("status") != "SUCCESS" and symbol.startswith("$") and not symbol.endswith(".X"):
        data = gex.fetch_chain_schwab(client, symbol + ".X",
                                      from_date=today, to_date=to_date, strike_count=2)
    contracts, spot, ts_ns, dropped, status = gex.parse_schwab_chain(data)
    print("[pull] status={}, underlying spot={}, {} sample contract(s) parsed."
          .format(status, gex.fmt_px(spot), len(contracts)))
    if status != "SUCCESS" or spot is None:
        print("[warn] unexpected response -- check entitlement (app 'Ready For Use' with "
              "Market Data Production) and the symbol.", file=sys.stderr)
        return 1
    if not contracts:
        # Auth + data path work, but nothing had usable OI+IV. For cash indices
        # ($SPX) Schwab always returns zero OI, so this is expected there.
        print("[warn] auth OK but the sample had 0 contracts with usable OI+IV. "
              "Cash indices ($SPX) have no OI on Schwab -- verify with an ETF, "
              "e.g. --ticker SPY.", file=sys.stderr)

    print("\n[done] You can now run:  python3 gex.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
