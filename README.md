# SPX Dealer Gamma Exposure (GEX) & Gamma-Flip Tool

A single, auditable Python script (`gex.py`) that estimates **dealer gamma
exposure** and the **gamma-flip (zero-gamma) level** for SPX, to set a daily
0DTE trading bias. It pulls the full options-chain snapshot from the **Charles
Schwab Trader API**, **recomputes gamma itself with Black-Scholes-Merton** (it does
*not* trust the vendor greeks), and prints a plain-text bias summary plus a per-strike chart.

Built to prioritize **correctness and auditability over features**: every
modeling assumption is written in code comments *and* printed at runtime.

---

## Install

```bash
pip install -r requirements.txt
```

> Python **3.11+** is the target. The core is kept compatible with **3.9+** (uses
> `from __future__ import annotations`, no 3.10-only syntax), so `--demo` and the
> tests run on stock macOS Python 3.9. The **live Schwab path uses `schwab-py`,
> which needs Python ≥ 3.10** — its import is lazy, and `pip install -r
> requirements.txt` skips it automatically on 3.9 (via an environment marker).

## Schwab setup (one time)

Data comes from the **Charles Schwab Trader API** (Market Data) via the
[`schwab-py`](https://schwab-py.readthedocs.io) library — **free with a Schwab
brokerage account** (no per-asset entitlement), and the whole chain (spot + OI +
IV) arrives in one `get_option_chain()` call. This mirrors the sibling
`overnight_vs_intraday` project's setup, so the token file is interchangeable.

1. At **developer.schwab.com**, create an app, add the **Market Data Production**
   product, and set the callback URL to **`https://127.0.0.1:8182`**. Wait for the
   app status to reach **Ready For Use** (Schwab approves manually — can take days).
2. Provide credentials (never hardcoded). Easiest: copy the template and source it:
   ```bash
   cp .env.example .env          # then fill in SCHWAB_APP_KEY / SCHWAB_APP_SECRET
   set -a; source .env; set +a   # load into the shell
   ```
   (or just `export SCHWAB_APP_KEY=... SCHWAB_APP_SECRET=...`).
3. Install `schwab-py` (Python ≥ 3.10) and run the guided login + live check:
   ```bash
   pip install 'schwab-py>=1.3'
   python3 scripts/schwab_setup.py            # browser flow; --manual for copy/paste
   ```
   This opens a browser, writes the token to **`.schwab_token.json`** (git-ignored),
   then pulls a tiny SPX chain and parses it to confirm everything works. The
   refresh token lasts ~7 days, so re-run weekly.

No credentials yet? Use `python3 gex.py --demo` for an offline synthetic chain.

## Run

```bash
python3 gex.py                       # default: 0DTE AND all expiries, side by side
python3 gex.py --expiry 0dte         # 0DTE only
python3 gex.py --expiry all          # all expiries (out to --all-days)
python3 gex.py --expiry 2026-06-19   # a specific expiration
python3 gex.py --rate 0.043 --div-yield 0.013   # override r and q
python3 gex.py --convention flipped  # flip the dealer sign convention
python3 gex.py --ticker SPY          # SPY ETF options proxy
python3 gex.py --demo                # offline synthetic chain (no credentials)
```

Useful flags: `--ticker` (default SPX → `$SPX`, but **Schwab returns no OI for
`$SPX`** — use `SPY`/`QQQ`), `--all-days` (window for `all`/default, default 45),
`--x-tick` (chart gridline spacing, default 10), `--multiplier` (100),
`--price-range` (±5% flip-search window), `--steps` (grid resolution),
`--call-sign`/`--put-sign` (raw sign overrides), `--no-plot`, `--out-prefix`,
`--callback`, `--token-path`. See `python3 gex.py --help`.

---

## Command cheat-sheet (by cadence)

> **Interpreter note (this machine):** the *live* interpreter is
> `/opt/anaconda3/bin/python` (Python 3.13 with `schwab-py`). The bare `python3` and
> the `.venv` are 3.9 — use those only for `--demo`/tests. Set a shortcut and load
> credentials once per terminal:
> ```bash
> PY=/opt/anaconda3/bin/python
> cd /Users/brendanhan/Desktop/Quant_Projects/gamma_exposure
> set -a; source .env; set +a
> ```

**One-time — setup**
```bash
pip install -r requirements.txt      # deps (schwab-py auto-skipped on 3.9; needs 3.10+ live)
cp .env.example .env                  # then fill in SCHWAB_APP_KEY / SCHWAB_APP_SECRET
$PY scripts/schwab_setup.py           # first OAuth login -> writes .schwab_token.json
```

**Daily — before the open (get the day's levels)**
```bash
$PY gex.py --ticker QQQ                # 0DTE + all-expiries: prints levels, saves chart
$PY gex.py --ticker SPY                # SPY instead of QQQ
$PY gex.py --ticker QQQ --expiry 0dte  # just today's 0DTE view
$PY gex.py --ticker QQQ --no-plot      # text only (faster, no chart)
```
Check the printed `OI reflects [date]` line — it should read yesterday's session.

**Weekly — re-authenticate (the refresh token expires ~7 days)**
```bash
$PY scripts/schwab_setup.py            # re-run the login (add --manual if the browser hangs)
```
If a daily run errors with a refresh-token / auth message, this is the fix.

**Monthly — around monthly OpEx (3rd Friday)**
```bash
$PY gex.py --ticker QQQ --expiry 2026-08-21   # that monthly expiration's positioning
$PY gex.py --ticker QQQ --all-days 60          # widen the window to catch the next monthly
```
The big walls reset after monthly OpEx — expect materially different levels the next week.

**Quarterly — triple-witching (3rd Fri of Mar/Jun/Sep/Dec) + refresh assumptions**
```bash
$PY gex.py --ticker QQQ --rate 0.043 --div-yield 0.013   # update r and q to current values
pip install -U -r requirements.txt                        # refresh dependencies
```

**Any time — reference / sanity**
```bash
$PY gex.py --demo                       # offline synthetic sample (no credentials)
$PY gex.py --ticker QQQ --x-tick 25     # chart gridlines every 25 instead of 10
$PY gex.py --ticker QQQ --convention flipped   # sensitivity: flip the dealer-sign assumption
$PY gex.py --help                       # every flag
$PY -m pytest test_gex.py               # run the test suite
```

---

## Daily automation → your phone (macOS launchd)

Runs the tool automatically **Mon–Fri at 07:45 CT (08:45 ET, ~45 min before the open)**
and pushes the levels + chart to your iPhone. Files: `scripts/daily_gex.sh` (runs the
tool + sends the push) and `scripts/com.brendanhan.gex-daily.plist` (the schedule).

**1. Pick a notifier** and add its keys to `.env` (see `.env.example`) — choose one:
- **Pushover** — polished, sends the chart image: create an app at pushover.net →
  set `PUSHOVER_TOKEN` + `PUSHOVER_USER`.
- **Telegram** — free, private, sends the chart: make a bot via @BotFather →
  set `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`.
- **ntfy.sh** — free, no account: install the ntfy app, subscribe to a hard-to-guess
  topic → set `NTFY_TOPIC`.

**2. Test it once by hand:**
```bash
bash scripts/daily_gex.sh
tail -n 20 logs/daily_gex.log        # shows it ran + which notifier was used
```

**3. Install the schedule:**
```bash
cp scripts/com.brendanhan.gex-daily.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.brendanhan.gex-daily.plist
launchctl kickstart -k gui/$(id -u)/com.brendanhan.gex-daily   # optional: run once now
```
Remove with `launchctl bootout gui/$(id -u)/com.brendanhan.gex-daily`.

Notes:
- The Mac must be **awake** at 07:45 CT (launchd runs a missed job on next wake; a
  powered-off Mac skips it).
- If a run **fails** (e.g. the weekly token expired), you still get a push titled
  "RUN FAILED" — your cue to re-run `scripts/schwab_setup.py`.
- Market **holidays aren't skipped** — you'll just get the prior session's (stale) levels.

---

## Methodology (the part that matters)

**1. BSM gamma** — identical for calls and puts, computed from Schwab's IV:

```
gamma = phi(d1) / (S * sigma * sqrt(T))
d1    = [ ln(S/K) + (r - q + 0.5*sigma^2) * T ] / (sigma * sqrt(T))
```

`S`=spot, `K`=strike, `sigma`=implied vol (Schwab IV), `T`=time-to-expiry in
years (ACT/365, to **16:00 ET** on the expiry — SPXW is PM-settled), `r`=risk-free
(`--rate`), `q`=dividend yield (`--div-yield`). `T` is floored at 5 minutes so
at-the-money 0DTE gamma stays finite near the close.

**2. Dollar GEX per contract** — "dollar gamma per 1% move":

```
GEX = gamma * open_interest * multiplier * S^2 * 0.01
```

= the dollar change in the aggregate (delta) position for a +1% move in spot.

**3. Dealer sign convention — the model's single biggest assumption, not a fact.**
Default = *standard*: dealers **long call gamma (+)**, **short put gamma (−)**:

```
net GEX = SUM_calls(GEX) - SUM_puts(GEX)
```

Flippable via `--convention flipped` or `--call-sign/--put-sign`.

**4. Gamma flip / zero-gamma level.** Total net GEX is **repriced across a grid of
hypothetical spot prices** (±5% by default), recomputing gamma *and* the `S^2`
term at each. The flip is where the total crosses zero (crossing nearest spot).
Reported in SPX and SPY-equivalent (live SPX/SPY ratio; fallback ~10).

**5. Walls.** Call wall = strike with the largest **positive** net GEX (pin /
resistance). Put wall = strike with the largest **negative** net GEX (support
that becomes a downside accelerant once breached).

**6. Regime.** `spot > flip` → dealers net **long gamma** (vol-dampening,
mean-reverting). `spot < flip` → net **short gamma** (vol-amplifying, trend-prone).

---

## Key outputs

1. Total net dealer GEX (`$/1% move`) — 0DTE vs all expiries, side by side.
2. Gamma flip level (SPX + SPY-equivalent).
3. Call wall and 4. put wall.
5. Per-strike GEX profile chart (PNG) with flip, walls, and spot marked.
6. A plain-text bias summary with a one-line interpretation.

## Uncertainty / guardrails (built in, not optional)

- **Put-sign sensitivity.** The flip is recomputed under the literal put-sign
  flip (which usually makes *all* gamma positive — i.e. the flip vanishes, an
  honest signal that the flip exists *only* under the short-put assumption) and
  under a graded **±50% change in the short-put magnitude**, which yields an
  actual "how far does it move" number. A **LOW CONFIDENCE** warning prints when
  the flip is materially sensitive (> 1% of spot).
- **OI staleness.** Open interest updates only once per day (overnight, OCC EOD).
  The tool prints the reference date and a clear caveat that **0DTE GEX lags**
  intraday positioning (today's freshly-opened 0DTE flow is *not* in this OI).
- **Gamma concentration.** Reports the fraction of total gamma in 0DTE vs later.
- **Thin/missing data.** Contracts with no OI or no IV are dropped (counts
  reported); expired contracts are dropped; it never crashes on a sparse chain.

## Limitations (read before trading on this)

- The dealer sign convention is an **assumption**, and the flip's *existence*
  depends on it. This is the biggest weakness — treat the regime as directional
  context, not a precise tradeable level.
- OI is end-of-prior-session; the intraday 0DTE picture is necessarily stale.
- Uses Schwab's IV as the input vol; garbage IV in → garbage gamma out.
- `q=0` by default (SPX really yields ~1.3%); pass `--div-yield` to include it.
- Schwab OAuth needs a one-time browser login and an **approved** developer app
  (approval can take days); `schwab-py` auto-refreshes the ~30-min access token,
  but the ~7-day refresh token requires re-running `scripts/schwab_setup.py`.
- The live Schwab path needs **Python ≥ 3.10** (`schwab-py`); `--demo` and the
  tests run on 3.9.

## Tests

```bash
python3 -m pytest test_gex.py -v
```

Covers BSM gamma against known reference values (a clean closed-form case and
Hull's textbook example) and `find_flip_level` against a synthetic chain whose
zero-gamma crossing is derived in closed form (see the derivation in the test).
