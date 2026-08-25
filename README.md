# Dealer Gamma Exposure (GEX) & Gamma-Flip Engine

Estimates **dealer gamma exposure** from live options-chain data and derives the price levels where
market-maker hedging flow changes character: the **gamma flip**, the **call wall**, and the **put wall**.
Those levels form a daily trading bias — where volatility is likely to be suppressed, where it is likely
to be amplified, and which strikes act as magnets or accelerants.

Data comes from the **Charles Schwab Trader API**. Gamma is **recomputed from scratch** with
Black-Scholes-Merton on per-strike implied volatility; vendor greeks are never trusted. Output is a
plain-text bias summary, a per-strike chart, a JSON API, an installable iPhone app, and scheduled
pre-market push notifications.

Built to prioritize **correctness and auditability over features**: every modeling assumption is written
in code comments *and* printed at runtime.

> 📄 **[GEX_Technical_Reference.pdf](GEX_Technical_Reference.pdf)** — 20-page deep dive: full methodology,
> function-by-function reference, production-hardening history, and Q&A.

---

## ⚠️ Read this first: SPY/QQQ, not SPX

**Schwab returns zero open interest for cash-index options (`$SPX`, `XSP`).** GEX is open-interest-weighted,
so **index GEX is not computable from this data source at any price** — verified live, where every `$SPX`
strike returned `OI = 0` while SPY returned full OI on the identical request.

The default ticker is therefore **SPY** (the standard dealer-gamma proxy), and SPY levels are cross-quoted
into SPX terms using the **live** SPX/SPY ratio. Running `--ticker SPX` prints a warning pointing you back
to the ETFs.

---

## What it produces

```
##############################################################################
# BIAS SUMMARY  --  ALL EXPIRIES
##############################################################################
  Current spot ...... SPY 590.00
  Regime ............ SHORT gamma  (spot < flip)
  Gamma flip ........ SPY 592.35  |  SPX 5,923.50
  Call wall ......... SPY 605.00
  Put wall .......... SPY 590.00
  Total net GEX ..... -0.025 $Bn  (-$25,183,398)
  Interpretation .... Spot is 0.40% BELOW the flip -> dealers net SHORT gamma:
                      they buy rallies / sell dips, amplifying vol. Bias:
                      momentum/trend, wider ranges; a break of the put wall can
                      accelerate lower. Reclaiming the flip calms it.
##############################################################################
```

Plus a **hedging-urgency decomposition** on every run, so you can see how much of the headline number is
fast money versus open interest that will never be rebalanced today:

```
GAMMA BY EXPIRY  (weight by hedging urgency, not magnitude alone)
  bucket                          gross |GEX|    share         net GEX
  0DTE (evaporates 16:00 ET)        4.85 $Bn    33.2%      -2.47 $Bn
  <= 1 week                         4.30 $Bn    36.6%      -2.42 $Bn
  <= monthly OpEx Aug 21            3.80 $Bn    32.3%      -1.52 $Bn
  beyond OpEx (slow money)          0.29 $Bn     2.5%      -0.09 $Bn
```

…a per-strike GEX chart (PNG) with spot, flip, and both walls marked, and the same data as JSON for the
mobile app.

---

## Quickstart

```bash
pip install -r requirements.txt
python3 gex.py --demo          # offline synthetic chain — no credentials needed
```

> **Python:** the core is 3.9-compatible (`--demo` and the tests run anywhere). **Live data needs Python
> ≥ 3.10** because `schwab-py` requires it; `pip install -r requirements.txt` skips it automatically on 3.9
> via an environment marker.

### Schwab setup (one time, then weekly re-auth)

Schwab market data is **free with a brokerage account** — no per-asset entitlement, and the whole chain
(spot + OI + IV) arrives in one request.

1. At **developer.schwab.com**, create an app, add the **Market Data Production** product, and set the
   callback URL to **`https://127.0.0.1:8182`**. Wait for status **Ready For Use** (manual approval, can
   take days).
2. Copy the credential template and fill it in:
   ```bash
   cp .env.example .env          # add SCHWAB_APP_KEY / SCHWAB_APP_SECRET
   set -a; source .env; set +a
   ```
3. Log in and verify:
   ```bash
   pip install 'schwab-py>=1.3'
   python3 scripts/schwab_setup.py            # add --manual if the browser flow stalls
   ```
   Writes `.schwab_token.json` (git-ignored), then pulls a live chain to confirm the response shape
   parses before you rely on it.

> 🔑 **Schwab refresh tokens expire every 7 days** and require a browser re-login — there is no headless
> renewal path. The tool warns from day 5.5 in every push, and failures are labelled
> `SCHWAB TOKEN EXPIRED` with the exact command to run.

### Token reliability — read this if tokens die early

Weekly re-login is **unavoidable** (Schwab policy: refresh tokens older than seven days are
rejected, with no way to extend them). But a token dying in *hours* is a different problem
with a specific cause:

> ⚠️ **Any process still holding the OLD token will revoke your NEW one.**
> Schwab treats a superseded refresh token as a compromise signal and revokes the entire
> token family — including the token you just created. A long-lived `server.py` from
> yesterday is enough to kill every fresh login you make.

**Always check before re-authenticating:**

```bash
gexps                       # lists anything holding the token
pkill -f "server.py"        # only if it found something
gexauth --manual            # then re-login
```

**When a token dies, diagnose it instead of guessing:**

```bash
gexaudit                    # verdict from the token journal
gexaudit --all --hours 48   # full history
```

Every token read and write is journaled to `logs/token_audit.log` with the PID, command,
and a **sha256 fingerprint** of the refresh token (enough to see rotation, never the secret
itself). `scripts/token_audit.py` reads it and names the pattern: `STALE REUSE` (which
process presented a superseded token, and when), `CONCURRENCY`, or `RAPID ROTATE`. If it
finds none of those, the cause was *not* a local race — suspect the 7-day cap or a
second login elsewhere.

Two safeguards run automatically:

- **Atomic token writes.** `schwab-py`'s default writer opens the token with mode `'w'`,
  truncating it before writing — a crash or two overlapping writers leaves a corrupt token
  that Schwab rejects. Writes now go to a temp file (`fsync`) then `os.replace`, which is
  atomic on POSIX: a reader sees the old token or the new one, never a partial.
- **Staleness re-checked inside the lock.** Checking before acquiring the lock is a
  check-then-act race: another process can rotate the token in that window, after which
  this client holds a superseded one. The check now happens immediately before every API
  call, and a rotated token is reloaded from disk.

---

## Shell helpers (recommended)

The tool needs three things lined up every time: the right interpreter (the system
`python3` has no numpy, and `schwab-py` needs ≥ 3.10), the project directory, and
credentials loaded from `.env`. These `~/.zshrc` functions handle all three, and work
from **any** directory:

```bash
export GEX_HOME="/Users/brendanhan/Desktop/Quant_Projects/gamma_exposure"
export PY="/opt/anaconda3/bin/python"

gex()       { ( cd "$GEX_HOME" && set -a && . ./.env && set +a && "$PY" gex.py "$@" ); }
gexserver() { ( cd "$GEX_HOME" && set -a && . ./.env && set +a && "$PY" server.py "$@" ); }
gexauth()   { ( cd "$GEX_HOME" && set -a && . ./.env && set +a && "$PY" scripts/schwab_setup.py "$@" ); }
gexoi()     { ( cd "$GEX_HOME" && set -a && . ./.env && set +a && "$PY" scripts/oi_history.py "$@" ); }
gexaudit()  { ( cd "$GEX_HOME" && "$PY" scripts/token_audit.py "$@" ); }
gexps()     { pgrep -fl "server.py|gex.py" || echo "no gex processes running"; }
```

Each runs in a **subshell**, so your working directory and environment are untouched.
After editing `~/.zshrc`, run `source ~/.zshrc` in already-open terminals (new ones pick
them up automatically).

```bash
gex --ticker QQQ --expiry 2026-08-21      # instead of: cd … && set -a && source .env && …
```

> Without these, `$PY` is undefined in a fresh terminal and `$PY gex.py …` collapses to
> `gex.py …` → `zsh: command not found: gex.py`.

---

## Usage

```bash
python3 gex.py                       # default: 0DTE AND all expiries, side by side
python3 gex.py --ticker QQQ          # any optionable underlying with listed OI
python3 gex.py --expiry 0dte         # single view
python3 gex.py --expiry 2026-08-21   # a specific expiration
python3 gex.py --expiry all --all-days 90        # widen the expiration window
python3 gex.py --rate 0.0469 --div-yield 0.012   # override r and q
python3 gex.py --convention flipped  # flip the dealer sign assumption
python3 gex.py --levels-only         # compact output (used by notifications)
python3 gex.py --demo                # offline synthetic chain
```

**Flags:** `--ticker` · `--expiry` · `--all-days` (default 45; wider risks a vendor 502) · `--rate` ·
`--div-yield` (auto per-ticker if omitted) · `--multiplier` · `--price-range` (±10% flip window) ·
`--steps` · `--convention` / `--call-sign` / `--put-sign` · `--x-tick` (chart gridlines, default 10) ·
`--no-save-chain` / `--chain-dir` (chain archive; archiving is ON by default) · `--callback` ·
`--no-plot` · `--out-prefix` · `--levels-only` · `--profiles` · `--watch N` · `--token-path` · `--demo`. Full list: `python3 gex.py --help`.

### When to run what

| Cadence | Command | Why |
|---|---|---|
| **Daily** (pre-open) | `gex.py --ticker SPY` | The day's levels. OI updates overnight, so run once before the open. |
| **Weekly** (Mon) | `--expiry all --all-days 90` | Structural levels into the next quarter. |
| **Weekly** (auth) | `scripts/schwab_setup.py` | **Required** — the 7-day refresh token dies otherwise. |
| **Monthly** (OpEx) | `--expiry <3rd Friday>` | Monthly OpEx holds the bulk of OI; levels reset after it. |
| **Quarterly** | `--rate <current> --div-yield <current>` | Triple witching, plus refresh your rate/yield assumptions. |

---

## Mobile app

`server.py` wraps the core in a local HTTP API (FastAPI, port **8787**) and serves an installable phone app
from `static/` — same math, live data, with a **configurable ticker** and a **real expiration picker**
(already-settled dates are excluded automatically).

```bash
python3 server.py
```

On your iPhone (same Wi-Fi): open `http://<mac-lan-ip>:8787` in Safari → Share → **Add to Home Screen**.
Dark standalone app with regime banner, LOW-CONFIDENCE badge, level cards, per-strike canvas chart, and the
expiry-bucket table.

**API:** `GET /api/gex?ticker=&expiry=both|0dte|all|YYYY-MM-DD&all_days=&fresh=0|1` ·
`GET /api/expirations?ticker=` · `GET /api/health` · interactive docs at `/docs`.

Results cache for 60s unless `fresh=1` (the app's refresh button always sends it, so spot and IV are
never a replayed snapshot). The server rebuilds its Schwab client when the token file changes, so it
heals itself after a re-login with no restart. LAN-only by default — for remote access, use Tailscale
on both devices.

### Re-authenticate from the phone — `/setup.html`

Since the weekly re-login is permanent, it doesn't have to mean finding a laptop:

**`http://<mac-lan-ip>:8787/setup.html`** → tap **START LOGIN** → approve on Schwab → you land on a
"can't open the page" error (expected) → copy the address bar → paste → **INSTALL TOKEN**.

The server exchanges the code, writes the token atomically, drops its cached client, clears caches, and
reports days remaining. Two ways to reach it without typing the URL: the header token indicator is
tappable and turns **amber inside the last 1.5 days**, and any auth failure shows a
**"Re-authenticate now →"** link.

Endpoints: `GET /api/auth/start` (returns the authorize URL) · `POST /api/auth/complete`
(`{"redirect_url": "…"}`). The app secret never leaves the server — the browser only handles the
short-lived authorization code, exactly as in the CLI flow. The exchange holds the token lock, and the
result is validated: a token written without a refresh token is rejected immediately rather than dying
30 minutes later.

**Always-on service:**
```bash
cp scripts/com.brendanhan.gex-server.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.brendanhan.gex-server.plist
```

---

## Scheduled push notifications

`scripts/daily_gex.sh <cadence>` runs the tool for each ticker and pushes levels + charts to your phone via
**ntfy**, **Pushover**, or **Telegram** (whichever credentials are in `.env`).

| Cadence | Schedule (CT / ET) | Window | Plist |
|---|---|---|---|
| daily | Mon–Fri 07:45 / 08:45 | 45 days | `com.brendanhan.gex-daily.plist` |
| weekly | Monday 07:50 / 08:50 | 90 days | `com.brendanhan.gex-weekly.plist` |
| monthly | 1st of month 07:55 | 150 days | `com.brendanhan.gex-monthly.plist` |

```bash
bash scripts/daily_gex.sh daily        # test by hand
for c in daily weekly monthly; do      # install the schedules
  cp scripts/com.brendanhan.gex-$c.plist ~/Library/LaunchAgents/
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.brendanhan.gex-$c.plist
done
```

Tickers and windows are overridable in `.env` (`GEX_TICKERS`, `GEX_{DAILY,WEEKLY,MONTHLY}_DAYS`,
`GEX_SEND_CHARTS`). Failure is itself a notification: titles distinguish success, `PARTIAL (1/2 ok)`,
`RUN FAILED`, and `SCHWAB TOKEN EXPIRED`.

**Notes:** the Mac must be awake at fire time (launchd runs a missed job on next wake). `/bin/bash` needs
**Full Disk Access** if the project lives under `~/Desktop` (System Settings → Privacy & Security).
Market holidays are not skipped — you get the prior session's levels.

---

## Open-interest history & empirical dealer sign

Every live run archives the raw chain to `chains/<TICKER>/<date>.csv.gz` (OI is never backfillable, so an
unsaved day is lost forever). `scripts/oi_history.py` turns that archive into two things a single snapshot
cannot give you:

```bash
python3 scripts/oi_history.py                          # archive inventory
python3 scripts/oi_history.py --ticker SPY             # day-over-day dOI report
python3 scripts/oi_history.py --ticker SPY --open-close cboe_oc.csv
```

- **dOI report** — where positioning is *building* vs. stale OI that has sat for weeks. The crude aggressor
  lean (last print near ask → customers bought → dealers short that strike's gamma) is **graded by the
  opening ratio `dOI/volume`**: a high ratio means the day's flow mostly *opened* (trustworthy), a low ratio
  means mostly churn, so the lean is **suppressed as noise** instead of shown as false signal.
- **Empirical dealer sign (`--open-close`)** — the model's `put_sign = -1` assumption is its biggest
  weakness. CBOE Open-Close data splits volume into opening/closing × buy/sell × origin (customer / firm /
  market-maker), so **net customer put buying ⇒ dealers short those puts**. Point it at a CBOE open-close
  CSV and it reports the empirical dealer sign per strike and a verdict on whether the standard assumption
  is **supported or fails** on that snapshot:

  ```
  PUT SIDE (the put_sign = -1 assumption under test):
    put strikes with dealers SHORT: 2   LONG: 1
    net customer put opening: +8,100 contracts
    => customers NET-BOUGHT puts: dealers are net SHORT put gamma.
       The standard put_sign = -1 assumption is SUPPORTED by this data.
  ```

  Caveat: CBOE captures only its own exchanges' share of volume (multi-listed options trade on up to 17
  venues) — directionally informative, not the whole market.

---

## Methodology

### 1. BSM (Merton) gamma — identical for calls and puts

```
gamma = exp(-q*T) * phi(d1) / (S * sigma * sqrt(T))
d1    = [ ln(S/K) + (r - q + sigma^2/2) * T ] / (sigma * sqrt(T))
```

- **σ is per-strike vendor IV.** The smile is **never** flattened to a single ATM vol — flattening destroys
  wing gamma and is the most common silent GEX bug.
- **`T` is calendar ACT/365** to **16:00 ET** (PM settlement at the cash close), *deliberately* not
  trading-time. Gamma depends on σ²·T, and vendor IV is annualized on a calendar clock; pairing it with a
  `390×252` clock breaks that pairing and inflates 0DTE gamma severalfold. A trading-time clock is correct
  only when IV is re-derived under the same clock.
- **`T` is floored at 5 minutes**, because gamma carries `S·σ·√T` in the denominator and diverges as `T → 0`.
- **`q` auto-resolves per ticker** (SPY 1.2%, QQQ 0.6%) with its provenance printed; `r` defaults to 0.0469
  and is always echoed.
- **Exercise style:** European gamma is used for American ETF options. The early-exercise premium
  concentrates in deep-ITM (low-gamma) strikes and is small for the short-dated flow that dominates GEX.

### 2. Dollar GEX per contract

```
GEX = gamma * open_interest * multiplier * S^2 * 0.01
```

= the dollar change in aggregate dealer delta for a **+1% move** in spot.

> **Unit warning:** this is the **per-1%** convention. Per-**point** GEX is `gamma·OI·mult·S`
> (= per-1% ÷ `0.01·S`). SqueezeMetrics publishes per-point; most retail charts publish per-1%. Comparing
> them raw is a category error.

### 3. Dealer sign convention — the biggest assumption, not a fact

Default (*standard*): dealers **long call gamma (+)**, **short put gamma (−)**.

```
net GEX = SUM_calls(GEX) - SUM_puts(GEX)
```

Flippable via `--convention flipped` or `--call-sign` / `--put-sign`. Ground truth would require
dealer-direction data (e.g. CBOE open-close), which this feed does not provide — so the tool quantifies its
exposure to the assumption instead of pretending.

### 4. Gamma flip — found by root-finding, and projected forward in time

Total net GEX is **repriced across a 1,000-point grid of hypothetical spot prices** (±10%), recomputing both
`Γ(S')` **and** the `S'²` dollar term at each node. Each bracketed sign change is then refined to machine
precision with **Brent's method** (the 0DTE curve is near-discontinuous, so linear interpolation across a
wide grid cell is a poor model).

The curve can legitimately cross zero **more than once** — all crossings are detected, the nearest to spot
is reported, and the rest are listed. Repricing holds each strike's IV fixed (**sticky-strike**); reality
sits between sticky-strike and sticky-delta, so the flip is an estimate under a stated vol-dynamics
assumption, not a model-free level.

**The flip is not static.** It is computed at the current `T`, but as `T → 0` the ATM gamma (`~1/√T`) grows
and the zero-gamma level migrates — materially so for 0DTE-heavy chains. Every run therefore also reports the
flip **projected at the 16:00 ET close** (each contract's `T` advanced, `K`/`σ`/OI held fixed) with the
signed migration, so the drift is visible rather than hidden:

```
Gamma flip / zero-gamma level [#2]:
  SPY  591.38   (as of NOW)
  Flip at 16:00 close (T-decayed): 592.52   (migrates +1.13, +0.19%, in 3h00m)
    => the flip is NOT static: it drifts as T decays; plan around the path,
       not just the snapshot level.
```

### 5. Walls and regime

Walls are computed **per-side** (industry convention), not from net GEX. Net GEX at a strike is
`call − put`, so a strike with huge call *and* put OI nets to ~zero yet still carries enormous gross gamma
and acts as a real pin — netting would hide it. Each wall measures its own side's size:

| Output | Definition | Meaning |
|---|---|---|
| **Call wall** | Largest **call-side** gamma **at or above spot** | Resistance / pin |
| **Put wall** | Largest **put-side** gamma **at or below spot** | Support that becomes a downside **accelerant** if breached |
| **Regime** | `spot > flip` → LONG gamma<br>`spot < flip` → SHORT gamma | Long: vol-damping, mean-reverting.<br>Short: vol-amplifying, trend-prone. |

**Second rule: each wall must sit on the side of spot where its label is true.** An unrestricted
`argmax` can put "resistance" *below* spot, where it is not resistance at all. Observed live: QQQ
spot 717.51 with the largest call-side **and** put-side gamma both at strike 700, so **both walls
reported 700** — a breached magnet mislabelled as resistance. A dominant strike on the far side is
still real information, so it is reported separately (`call_dominant` / `put_dominant`) rather than
silently dropped:

```
Call wall (resistance/pin): QQQ 730.00   call-side GEX +0.671 $Bn
Put wall  (support/accel):  QQQ 700.00   put-side GEX -1.055 $Bn
  note: largest call-side gamma overall sits at 700.00
        (below spot -- breached magnet, not resistance)
```

---

## Uncertainty & guardrails (built in, not optional)

- **Put-sign sensitivity.** The flip is recomputed under the literal sign flip (which usually makes *all*
  gamma positive, i.e. the flip vanishes — an honest signal that it exists *only* under the short-put
  assumption) **and** under a graded **±50% change in short-put magnitude**, which yields an actual "how far
  does it move" number. A **LOW CONFIDENCE** warning prints when the flip moves more than **1% of spot**.
- **OI staleness — escalated for 0DTE.** Open interest updates once daily (overnight, OCC EOD). The
  reference date is printed with a clear caveat that **0DTE GEX lags** — today's freshly-opened 0DTE flow is
  *not* in this OI. Because that missing intraday flow is often *most* of the day's 0DTE gamma, a prominent
  `*** 0DTE CAVEAT ***` is printed **next to the 0DTE flip itself** (and in its bias summary), not just in
  the data-health block — so the precise-looking 0DTE level is not over-trusted.
- **Flip time-decay.** The flip migrates as `T` decays; the close-of-day projection (above) makes that
  drift explicit instead of presenting the flip as a fixed level.
- **Gamma by expiry.** The hedging-urgency decomposition shown above.
- **Quote-quality filters.** Crossed quotes (bid > ask) dropped anywhere; **deep-ITM** contracts (>5% ITM)
  with no bid or a relative spread >25% dropped — their IV comes from a sliver of extrinsic value and is
  noise. **Deep-OTM wings are never quote-filtered**: zero-bid wings still carry real tail gamma. Zero-OI
  strikes always dropped. All drop counts printed.
- **Never crashes on a thin chain.** Degenerate inputs return 0, never NaN or infinity.
- **Empty views explain themselves.** After 16:00 ET the day's 0DTE has settled, so the 0DTE view is
  legitimately empty — it now says *why* (`today's 0DTE settled at 16:00 ET (7h 20m ago) — expired, not
  missing`) instead of printing a bare `(empty)` that reads like a malfunction. Weekend, thin-chain and
  already-settled-expiry cases each get their own wording.

## Intraday signed order flow — measuring the dealer sign

```bash
gexflow --ticker SPY --interval 60     # track a session (run it during RTH)
gexflow --ticker SPY --report          # analyse what was tracked
```

The dealer sign convention (`put_sign = -1`) is the model's **largest error source** —
flip it and the gamma flip stops existing. `flow.py` measures it instead of assuming it.

**Why polling is the whole point.** Measured live, one SPY strike traded **39,407 contracts**
in a session while `lastSize` was **25**. A once-daily snapshot therefore classifies **0.06%**
of the day's volume and guesses the rest. Polling turns that into hundreds of real
observations:

```
window volume = totalVolume(t) - totalVolume(t-1)     # what actually TRADED
direction     = Lee-Ready on (last, bid, ask)         # who was the AGGRESSOR
signed flow   = window volume x direction             # accumulated per contract
```

**Lee-Ready classification** uses the quote rule (last above the mid → buyer-initiated,
below → seller-initiated), falling back to the tick rule for at-mid prints. Unclassifiable
trades contribute **zero**, never a guess.

**From aggressor to dealer sign.** Market makers post liquidity, customers take it — so a
buyer-initiated trade is customer-buys / dealer-sells:

```
put   customers net BUYING   -> dealers SHORT -> put_sign -1   [AGREES with the assumed -1]
call  customers net SELLING  -> dealers LONG  -> call_sign +1  [AGREES with the assumed +1]

-> The put_sign = -1 assumption is SUPPORTED by this session's flow.
```

When it **contradicts** the assumption, the report says so and tells you to re-run with
`--put-sign +1` and compare the flip — which is precisely the mis-specification the
LOW CONFIDENCE band has been measuring blind.

> **Sampling, not a tape.** Offsetting trades between polls net out, and direction uses the
> quote at each window's end. This infers **aggressor side, never counterparty identity** —
> only CBOE open-close data tells you whether the taker was a customer or another dealer.
> It replaces a pure guess with a measured estimate; it is not ground truth. Poll faster for
> a finer estimate.

Recordings land in `flow/<TICKER>/<date>.csv.gz` (git-ignored). Like `chains/`, a session
**not tracked is gone** — this data cannot be backfilled.

Flags: `--ticker` · `--interval` (seconds between polls) · `--report` · `--expiry` ·
`--date` (analyse a past session) · `--top` (rows shown) · `--flow-dir` · `--all-days`.

---

## Validation — does the model actually predict anything?

```bash
gexvalidate --ticker QQQ        # replay the archive, score against realized moves
```

Everything else improves the *estimate*. `scripts/validate.py` asks whether the estimate is
**useful**, by replaying every archived chain, recomputing that day's levels, and scoring them
against what price did next.

**No look-ahead:** a snapshot taken any time on day D is known by D's close, so every level is
graded on the **following** session. The most recent day always waits for tomorrow.

### Test 1 — overnight vs RTH (the falsifiable one)

Options hedging happens in regular hours: SPX/SPY are closed overnight while the underlying still
gaps on news. So **overnight is a natural control group** — the mechanism is switched off, every
other driver of volatility is still present.

| Outcome | Meaning |
|---|---|
| Effect in **RTH but not overnight** | Consistent with hedging transmission |
| Effect in **both, equally** | The regime is probably proxying general volatility, not dealer gamma |
| Effect in **neither** | No measurable predictive content |

This is the test that can embarrass the model, which is why it is worth running.

```
  |move| by window                 SHORT        LONG         t   verdict
  OVERNIGHT (control)            0.720%      0.526%     +0.67   n too small for a verdict
  RTH (mechanism live)           0.409%      0.600%     -1.09   n too small for a verdict
  RTH range (high-low)           0.910%      1.094%     -0.81   n too small for a verdict
```

### Test 2 — do the levels hold?

Wall containment (did the next session's high respect the call wall, the low the put wall) and flip
attraction (did price close *toward* the flip, vs a 50% coin flip). Containment is reported
**alongside the wall's distance**, because a wall 3% away that "holds" on a 0.5% day tells you nothing.

> **Honest statistics.** Sample size is printed with every result and **no significance is claimed
> below 20 sessions per group** — small samples get descriptives and an explicit "n too small",
> never a p-value that would only be noise. With ~11 sessions archived, current output is
> directional at best.

---

## Hedging flow — where this gamma is actually executed

Dealers hedging index gamma don't buy 500 single stocks; they trade **ES futures** (deepest book,
cheapest execution, best margin), or SPY shares for smaller clips. So the flow these levels describe
*is* futures flow — and index arbitrage carries it into SPX cash and SPY. Every run converts dollar
gamma into the contracts that actually have to trade:

```
HEDGING FLOW  (where this gamma is actually EXECUTED)
  Net GEX -7.415 $Bn per 1% move means, per 1% move, dealers must trade:

  ES futures               19,378 contracts   (1.29% of a typical day)
  SPY shares            9,711,805 shares      (12.95% of a typical day)

  Direction: SELL into a drop / BUY into a rally (amplifying)
```

`contracts = GEX_dollars / (multiplier × index level)` — ES is $50/point, MES $5. ES tracks SPX to
within the carry basis (<1%), immaterial for a flow estimate, which matters because **Schwab carries
no futures data at all** (see below). The scope caveat still applies: computed from one underlying's
chain, so it understates the full complex.

---

## Futures: not available from this data source

**Verified live** — `/chains` returns **HTTP 400** for `/ES`, `/MES`, `/NQ`, and the quotes endpoint
404s on `/MES` and `/ESZ26`. Worse, plain **`ES` silently resolves to EVERSOURCE ENERGY** (~$71), which
would produce a confident, complete gamma profile for an unrelated utility stock. `CL` → Colgate, and so on.

The tool now **refuses** slash-prefixed futures symbols and **warns loudly** on equity tickers that
collide with futures roots, before any computation happens.

> **The math would be fine if you had the data.** Futures options price under **Black-76**, and setting
> `q = r` collapses the Merton form onto it *exactly* (verified to 1e-15): the `(r − q + σ²/2)` drift
> becomes `σ²/2` and `exp(−q·T)` becomes `exp(−r·T)`. So
> `gex.py --rate 0.0469 --div-yield 0.0469 --multiplier 50` would be correct for ES options — only the
> data layer is missing. IBKR or CME DataMine would supply it.

---

## Implied move (ATM straddle)

Every run prices the **at-the-money 0DTE straddle** and converts it to an expected move —
the market's own quote on the session, and more precise than the `VIX/16` rule of thumb
(a crude annual→daily scaling of a 30-day variance index, not a quote on today).

```
IMPLIED MOVE  (ATM straddle -- the market's own priced-in move)
  basis ........... 0DTE | ATM strike 766 (+0.28 from spot 765.72)
  ATM call     1.50  + ATM put     1.90  = straddle     3.40

  Breakeven / expected |move|:  +/- 0.44%  (3.40 SPY pts)
  1-SD equivalent ...........:  +/- 0.56%  (4.26 pts)   <-- compare to VIX/16
  ATM IV cross-check ........:  +/- 0.35%   (IV 6.5% x sqrt(T))
  In SPX terms ..............:  +/- 34 pts breakeven, +/- 43 pts 1-SD (level 7,674)
```

> ⚠️ **The two figures are different quantities — this is the part that gets misused.**
> `straddle / spot` is the **breakeven**, which equals the *expected absolute move*
> `E|ΔS|/S`. `VIX/16` estimates a **1-standard-deviation** move. They differ by
> `√(π/2) ≈ 1.2533`, so comparing the straddle directly against VIX/16 **understates the
> band by ~20%**. Both are printed; only the 1-SD line is VIX/16-comparable.

Details: prices use the **mid** `(bid+ask)/2` rather than `last`, since a stale print badly
distorts a 0DTE straddle; the ATM strike is the nearest strike quoting **both** sides. After
16:00 ET the 0DTE contracts have settled, so it falls back to the nearest live expiry and
says so. An **ATM IV cross-check** is printed alongside — when quote-implied and IV-implied
vol disagree by more than ~25% a warning fires, which usually means a stale quote outside
regular hours or a vendor day-count mismatch.

Also exposed as `implied_move` in `/api/gex` and shown on the iPhone app.

---

## Dual profile: trading layer vs structural layer

```bash
python3 gex.py --ticker QQQ --profiles      # both layers, side by side
python3 gex.py --ticker QQQ --watch 60      # recompute the intraday layer every 60s
```

The same contract set answers two different questions, and conflating them is a modeling
error. `--profiles` runs both:

| | **INTRADAY** (trading layer) | **STRUCTURAL** (overview layer) |
|---|---|---|
| **Scope** | 0DTE → front week **+ next monthly OpEx** | Every expiry in the window |
| **Weighting** | **OI blended with today's volume** | **OI only** |
| **Why** | That's where essentially all real-time hedging pressure lives | Durable positioning, not today's churn |
| **Behavior** | Moves continuously — recompute on a fast cadence | Barely moves day to day; that's the point |
| **Use for** | Today's tradeable flip and nearest walls | Multi-day walls and the regime flip |

**The volume blend is the single biggest intraday fix.** Prior-close OI is stale by
mid-morning on 0DTE and systematically understates the gamma actually driving the tape.
Weighting: **volume × 1.0 for 0DTE**, **× 0.5 through the front week**, **× 0.0 beyond**.

> **Stated assumption:** traded volume includes *closing* as well as opening trades, so the
> blend is an **upper bound** on fresh positioning. It deliberately biases toward
> over-counting near-dated activity, because understating live 0DTE gamma is the larger error.
> The output prints the **volume uplift** (`+159.5%` on a recent QQQ run) so you can see
> exactly how much the blend changed versus OI alone.

**Per-expiry vol is preserved** — in fact the tool goes further and uses **per-strike** IV, so
front-week and monthly vols are never collapsed into one point that would smear the flip.

**The 0DTE decay cliff is surfaced explicitly.** The intraday profile warns what fraction of
itself expires at 16:00 ET and how long remains:

```
*** 0DTE DECAY CLIFF: 34% of this profile (218 contracts) expires
    at 16:00 ET -- 2h 41m left. These levels are valid only until then.
```

**Layer agreement is checked.** When the two flips differ by more than 1% of spot, the output
says the near-dated book is pulling the flip away from the standing structure — trade the
intraday level, but expect it to snap back toward the structural one once front-dated gamma
expires. When they agree, the intraday flip is anchored by durable structure and deserves more
confidence. (This is also the natural cross-check against an external regime model: the
structural flip and your regime state should broadly agree, and divergence is worth a look.)

**`--watch N`** re-fetches every N seconds and prints one line per tick with spot/flip/walls
and the change since the last tick — because a level computed at 09:35 is not the level at 13:00:

```
17:44:21  spot 729.87  flip 730.28  walls 735.00/730.00  net -0.342 $Bn  spot +0.00  flip -0.00
```

---

## The governing rule: match the expiry set to your holding period

The discipline almost nobody applies. GEX from the wrong expiry set is noise for your horizon:

| Horizon | Use | Reality check |
|---|---|---|
| **Intraday / 0DTE** | `--expiry 0dte` | **Prior-close OI is stale here** — 0DTE positions open and close intraday; real-time signed volume (not available on this feed) is what actually drives it. 0DTE gamma **evaporates at 16:00 ET daily**. |
| **Daily swing** | default (45d) | A **regime classifier, not an entry trigger**. |
| **Weekly/monthly** | 90d window | **Monthly OpEx holds the bulk of OI.** Charm/vanna dominate into roll-off — not modeled here. |
| **Quarterly** | 150d window | **Triple witching** = the biggest structural resets. |
| **Longer** | — | LEAPS gamma per contract is negligible; **vanna carries** — wrong tool. |

If you day-trade off an all-expirations chart, part of that number is six-month OI that will **not** be
rebalanced today. Weight by hedging urgency, not magnitude alone.

---

## Limitations (read before trading on this)

- The **dealer sign convention is an assumption**, and the flip's *existence* depends on it. Biggest
  weakness — treat the regime as directional context, not a precise tradeable level. It can now be **tested
  empirically** against CBOE open-close data (`scripts/oi_history.py --open-close`), which reports whether
  dealers are actually net short puts on a given day.
- **OI is end-of-prior-session**; the intraday 0DTE picture is necessarily stale (flagged prominently on
  the 0DTE view).
- **Single-underlying scope.** A full S&P dealer book aggregates SPX + SPXW + XSP + SPY + ES normalized to
  common notional. Schwab has no index OI and no futures options, so this measures **one listed underlying**
  as a correlated proxy. Raw GEX is deliberately never summed across underlyings.
- **Charm and vanna are not modeled** — they dominate into monthly OpEx roll-off.
- Uses vendor per-strike IV; quote filters catch the worst, but garbage IV in → garbage gamma out.
- **Sticky-strike** repricing and **European-exercise** gamma are stated approximations.
- Schwab OAuth needs an **approved** developer app and a **weekly** browser re-login.

---

## Repository map

| Path | Role |
|---|---|
| `gex.py` | Quant core + Schwab data layer + token persistence + rendering + CLI |
| `gex.py` core tests | `test_gex.py` — 63 pytest cases (no network) |
| `server.py` | FastAPI JSON API, caching, client lifecycle, phone re-auth endpoints |
| `static/index.html` | Installable iOS PWA (levels) |
| `static/setup.html` | Phone-based Schwab re-authentication |
| `scripts/schwab_setup.py` | Terminal OAuth login + token validation + live verification |
| `scripts/oi_history.py` | Chain-archive dOI analysis + CBOE open-close dealer-sign test |
| `scripts/token_audit.py` | Diagnoses early token expiry from the token journal |
| `scripts/daily_gex.sh` | Multi-cadence notifier |
| `scripts/*.plist` | 4 launchd agents (3 push cadences + API server) |
| `test_flow.py` | 11 pytest cases for the flow tracker (no network) |
| `chains/` | Chain archive — **not regenerable, back this up** (git-ignored) |
| `logs/token_audit.log` | Token read/write journal (fingerprints only, no secrets) |
| `GEX_Technical_Reference.pdf` | Full technical documentation |

## Tests

```bash
python3 -m pytest test_gex.py test_flow.py -v    # 74 tests, no network
```

Validation is against **independently derived truth**, not recorded output:

- **BSM gamma** vs a hand-computed closed form (asserted to 1e-9), **Hull's textbook example**
  (`S=49, K=50, r=0.05, σ=0.20, T=20/52 → Γ ≈ 0.066`), and a construction where `d₁ = 0` exactly to isolate
  the `exp(-q·T)` factor (1e-12).
- **`find_flip_level`** against a hand-derived analytical crossing: for one call at `Kc` and one put at `Kp`
  with equal OI and matching σ/T, the common factors cancel at the zero, giving
  `S* = √(Kc·Kp)·e^(-cT)` — the grid search must land on **97.5288**.
- **Per-side walls**: a strike with huge call *and* put gamma (net ≈ 0) must still be found as both walls —
  the regression the net-based definition missed.
- **Flip time-decay**: the close-of-day projection returns both the current and T-decayed flip, drops
  contracts that expire inside the window, and floors tiny `T` consistently with the live snapshot.
- **0DTE staleness escalation**: the caveat fires on 0DTE views with a flip and never on other views.
- **Empirical dealer sign**: open-close CSV parsing (flexible vendor column spellings), the
  customer⇒dealer sign inversion, and the supported/fails verdict in both directions.
- **Token persistence**: writes are atomic (a failed write leaves the previous token intact), the
  journal records rotation via fingerprints, and **raw refresh tokens never appear in the log**.
- **Stale-client reload**: a token rotated by another process forces a rebuild before the next call —
  the fix for tokens dying in hours instead of days.
- Plus: quote-filter behavior (including **wing preservation**), OpEx calendar arithmetic, retry
  classification (transient vs. auth), cross-process token-lock exclusivity via a real subprocess, and
  lock re-entrancy (nested acquisition must not deadlock against itself).