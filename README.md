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

---

## Usage

```bash
python3 gex.py                       # default: 0DTE AND all expiries, side by side
python3 gex.py --ticker QQQ          # any optionable underlying with listed OI
python3 gex.py --expiry 0dte         # single view
python3 gex.py --expiry 2026-08-21   # a specific expiration
python3 gex.py --expiry all --all-days 90        # widen the expiration window
python3 gex.py --rate 0.043 --div-yield 0.012    # override r and q
python3 gex.py --convention flipped  # flip the dealer sign assumption
python3 gex.py --levels-only         # compact output (used by notifications)
python3 gex.py --demo                # offline synthetic chain
```

**Flags:** `--ticker` · `--expiry` · `--all-days` (default 45; wider risks a vendor 502) · `--rate` ·
`--div-yield` (auto per-ticker if omitted) · `--multiplier` · `--price-range` (±10% flip window) ·
`--steps` · `--convention` / `--call-sign` / `--put-sign` · `--x-tick` (chart gridlines, default 10) ·
`--no-plot` · `--out-prefix` · `--levels-only` · `--token-path` · `--demo`. Full list: `python3 gex.py --help`.

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

**API:** `GET /api/gex?ticker=&expiry=both|0dte|all|YYYY-MM-DD&all_days=` · `GET /api/expirations?ticker=` ·
`GET /api/health` · interactive docs at `/docs`.

Results cache for 60s; the server rebuilds its Schwab client when the token file changes, so it heals itself
after a re-login with no restart. LAN-only by default — for remote access, use Tailscale on both devices.

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
- **`q` auto-resolves per ticker** (SPY 1.2%, QQQ 0.6%) with its provenance printed; `r` defaults to 0.043
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
| **Call wall** | Strike with the largest **call-side** dollar gamma | Resistance / pin |
| **Put wall** | Strike with the largest **put-side** dollar gamma (absolute) | Support that becomes a downside **accelerant** if breached |
| **Regime** | `spot > flip` → LONG gamma<br>`spot < flip` → SHORT gamma | Long: vol-damping, mean-reverting.<br>Short: vol-amplifying, trend-prone. |

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
| `gex.py` | Quant core + Schwab data layer + rendering + CLI |
| `test_gex.py` | 47 pytest cases (no network) |
| `server.py` | FastAPI JSON API, caching, client lifecycle |
| `static/index.html` | Installable iOS PWA |
| `scripts/schwab_setup.py` | One-time OAuth login + live verification |
| `scripts/oi_history.py` | Chain-archive dOI analysis + CBOE open-close dealer-sign test |
| `scripts/daily_gex.sh` | Multi-cadence notifier |
| `scripts/*.plist` | 4 launchd agents (3 push cadences + API server) |
| `GEX_Technical_Reference.pdf` | Full technical documentation |

## Tests

```bash
python3 -m pytest test_gex.py -v      # 47 tests, no network
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
- Plus: quote-filter behavior (including **wing preservation**), OpEx calendar arithmetic, retry
  classification (transient vs. auth), and cross-process token-lock exclusivity via a real subprocess.