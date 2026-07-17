#!/usr/bin/env python3
"""One-time Schwab auth + live verification for gex.py (run this on YOUR machine).

Mirrors the sibling overnight_vs_intraday project's setup: it uses the schwab-py
library for the OAuth flow + token refresh, writes the token file, then pulls a
tiny option-chain slice and parses it with gex.parse_schwab_chain so you can
confirm the live response shape matches the loader BEFORE a real run.

Prereqs:
  pip install 'schwab-py>=1.3'     # needs Python >= 3.10
  export SCHWAB_APP_KEY=...         # 'App Key' from developer.schwab.com
  export SCHWAB_APP_SECRET=...      # 'Secret'
  # App must have the *Market Data Production* product, status "Ready For Use",
  # callback URL https://127.0.0.1:8182
  # (tip: copy .env.example -> .env, fill it in, then: set -a; source .env; set +a)

Usage:
  python3 scripts/schwab_setup.py             # browser login flow + verify
  python3 scripts/schwab_setup.py --manual    # copy/paste flow (no local server)
  python3 scripts/schwab_setup.py --ticker SPY
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import timedelta
from pathlib import Path

# Make the repo root importable so `import gex` works regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gex  # noqa: E402  (after sys.path tweak)

CALLBACK = os.environ.get("SCHWAB_CALLBACK_URL", gex.DEFAULT_CALLBACK)
TOKEN_PATH = os.environ.get("SCHWAB_TOKEN_PATH", gex.DEFAULT_TOKEN_PATH)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="One-time Schwab auth + verify for gex.py.")
    p.add_argument("--manual", action="store_true",
                   help="copy/paste flow (no local loopback server).")
    p.add_argument("--ticker", default=gex.DEFAULT_TICKER,
                   help="symbol to verify against (SPX -> $SPX).")
    args = p.parse_args(argv)

    app_key = os.environ.get("SCHWAB_APP_KEY")
    app_secret = os.environ.get("SCHWAB_APP_SECRET")
    if not (app_key and app_secret):
        print("ERROR: set SCHWAB_APP_KEY and SCHWAB_APP_SECRET first.", file=sys.stderr)
        return 2

    try:
        if args.manual:
            from schwab.auth import client_from_manual_flow
        else:
            from schwab.auth import easy_client
    except ImportError:
        print("ERROR: pip install 'schwab-py>=1.3'  (needs Python >= 3.10)", file=sys.stderr)
        return 2

    # 1) Authenticate -> writes/refreshes the token file.
    if args.manual:
        print("[auth] MANUAL flow:")
        print("  1) open the URL it prints below, log in, and click ALLOW/ACCEPT")
        print("  2) you'll land on a blank/'can't be reached' {}/?code=... page".format(CALLBACK))
        print("  3) copy that FULL URL from the address bar and paste it back here\n")
        client = client_from_manual_flow(app_key, app_secret, CALLBACK, TOKEN_PATH)
    else:
        print("[auth] launching browser login flow (callback {}) ...".format(CALLBACK))
        print("       (if nothing happens after login, Ctrl+C and re-run with --manual)")
        client = easy_client(api_key=app_key, app_secret=app_secret,
                             callback_url=CALLBACK, token_path=TOKEN_PATH)
    print("[auth] OK -- token written to {} (refresh expires in ~7 days)".format(TOKEN_PATH))

    # 2) Live verification: pull a minimal chain slice and run it through gex's
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
