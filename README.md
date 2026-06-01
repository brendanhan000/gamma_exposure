# SPX Dealer Gamma Exposure (GEX) & Gamma-Flip Tool

A single, auditable Python script (`gex.py`) that estimates **dealer gamma
exposure** and the **gamma-flip (zero-gamma) level** for SPX, to set a daily
0DTE trading bias. It pulls the full options-chain snapshot from Polygon.io,
**recomputes gamma itself with Black-Scholes-Merton** (it does *not* trust the
vendor greeks), and prints a plain-text bias summary plus a per-strike chart.

Built to prioritize **correctness and auditability over features**: every
modeling assumption is written in code comments *and* printed at runtime.

---

## Install

```bash
pip install -r requirements.txt
export POLYGON_API_KEY=...        # never hardcoded; read from the environment
```

> Python **3.11+** is the target. The code is deliberately kept compatible with
> **3.9+** (uses `from __future__ import annotations`, no 3.10-only syntax), so it
> also runs on stock macOS Python 3.9 — that is what it was developed/tested on.

## Run

```bash
python3 gex.py                       # default: 0DTE AND all expiries, side by side
python3 gex.py --expiry 0dte         # 0DTE only
python3 gex.py --expiry all          # all expiries only
python3 gex.py --expiry 2026-06-19   # a specific expiration
python3 gex.py --rate 0.043 --div-yield 0.013   # override r and q
python3 gex.py --convention flipped  # flip the dealer sign convention
python3 gex.py --demo                # offline synthetic chain (no API key needed)
```

Useful flags: `--ticker` (default SPX -> `I:SPX`), `--multiplier` (100),
`--price-range` (±5% flip-search window), `--steps` (grid resolution),
`--call-sign`/`--put-sign` (raw sign overrides), `--no-plot`, `--out-prefix`,
`--max-pages`, `--sleep` (for free-tier rate limits). See `python3 gex.py --help`.

---

## Methodology (the part that matters)

**1. BSM gamma** — identical for calls and puts, computed from Polygon's IV:

```
gamma = phi(d1) / (S * sigma * sqrt(T))
d1    = [ ln(S/K) + (r - q + 0.5*sigma^2) * T ] / (sigma * sqrt(T))
```

`S`=spot, `K`=strike, `sigma`=implied vol (Polygon IV), `T`=time-to-expiry in
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
- Uses Polygon's IV as the input vol; garbage IV in → garbage gamma out.
- `q=0` by default (SPX really yields ~1.3%); pass `--div-yield` to include it.
- Pulling *all* expiries is many paginated requests; a paid Polygon tier is
  recommended (use `--sleep` on the free tier).

## Tests

```bash
python3 -m pytest test_gex.py -v
```

Covers BSM gamma against known reference values (a clean closed-form case and
Hull's textbook example) and `find_flip_level` against a synthetic chain whose
zero-gamma crossing is derived in closed form (see the derivation in the test).
