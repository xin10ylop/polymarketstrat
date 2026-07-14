# Polymarket BTC Up/Down — Strategy Research Report

**Data**: 9 months (2025-10-11 → 2026-07-13) across four families — 5m / 15m / 1h (ET-hourly) / 4h —
92,741 resolved markets; full quote/trade tapes, 25-level book curves (vault families), 1-second
Chainlink oracle feed (Apr 2+), Binance BTCUSDT 1s klines for the whole period.
**Fees** (verified from official docs + fee_rate column in data): taker = `0.07 × p × (1−p)` per share
(≈1.75c at 50c, ≈0.33c at 95c); **maker = 0** (+ rebate program, ignored conservatively).
**Split**: tuned on data before 2026-05-16; validated out-of-sample on 2026-05-16 → 2026-07-13.
**Fills**: maker fills simulated against the real trade tape (fill requires ≥2× our size in aggressor
volume through our price after placement); taker fills only against standing quotes with size ≥ our order.
$5–10 clips (12–15 shares) throughout.

## Verified market mechanics (foundation for everything below)

1. Resolution = Chainlink BTC/USD sampled exactly at the window's open and close second;
   Up wins iff close > open. The public 1s feed matches the resolver bit-for-bit (11,457/11,457 windows).
2. The median deciding move in a 5m window is **4.8 bps** ($3–8); 10% of windows resolve on <0.8 bps.
   These markets are coin flips decided by oracle-grade noise — which is why microstructure edges
   dominate "prediction" edges.
3. Markets trade **before** the window opens (listed hours early) and **after** it closes
   (until settlement: ~23s for 5m, ~36s for 15m, ~69 min median for 1h).
4. The market's implied probability is *well-calibrated overall* (Brier 0.176 vs 0.200 for a
   textbook Black-Scholes-style binary pricer using realized vol) — the crowd here is not dumb,
   and naive "model vs market" trading loses. The money is in the residuals listed below.

## RANKED VIABLE STRATEGIES

### #1 — Post-close winner toll ("settlement-lag liquidity provision")
**Edge type**: riskless toll collection. **Family**: 5m (+15m marginal).
After the window closes, the outcome is fully determined by the already-public Chainlink print,
but position-dumping continues for ~20s until settlement (median 4,000 shares/window of winner
tokens sold at ≤0.995). Resting a bid at **0.992 on the winning token at T+2s** collects
1.00 − 0.992 = **+0.8c/share, zero variance** (win rate 100% by construction; the only risk is
operational: reading the oracle print and posting within ~2s).
- Train (5m, Feb–May): 22,755 fills / 26,773 windows (85%), +0.800c/share, ~245 fills/day.
- OOS (May 16 – Jul 13): **{TBD_S3}**
- Sensitivity: queue_mult 2→5 changes fills by <2%; placing at T+4s instead of T+2s: −3% fills.
  Limit 0.99 → fewer fills (35%), +1.0c; 0.992 maximizes $/day.
- 15m adds ~30 fills/day at the same +0.8c. 1h adds ~6/day.
- **Capacity**: ~$25–40/day at 15-share clips. Erosion risk: other bots joining the 0.992+ queue.

### #2 — 5m favorite harvest (maker join-bid at +60s)
**Edge type**: behavioral favorite-longshot bias. **Family**: 5m only (dies on 15m/1h/4h).
At 60s into a 5m window, when the favorite (whichever token's mid ≥ 0.5) trades at **0.80–0.93**,
its true win probability exceeds its price by 2–6c (train: price 0.87 → wins 92.8%). Join the
favorite's bid (maker, fee-free), cancel if unfilled in 45s, hold to settlement.
- Train (tape-simulated, queue_mult=2): fills 90%, **+3.17c/share**, 18.9 trades/day,
  positive every month (Feb +3.1, Mar +3.2, Apr +2.1, May +5.5).
- OOS: **{TBD_S1}**
- Adverse selection is real (filled entries win 87.1% vs 94.5% for unfilled) and already
  reflected in the tape-based numbers.
- Timing map: +3.2c @60s, +2.5c @90s, +0.2c @120s — the bias is harvested in the first ~90s.
- Longshot mirror (selling 0.07–0.20 tokens) is the same trade — buying the favorite's complement.

### #3 — Oracle-lag terminal snipe (taker, last 5 seconds)
**Edge type**: information latency. **Family**: 5m (T−5s) and 15m (T−15s).
In the final seconds, Binance spot has already decided the outcome, but the book still quotes
mid-range prices (Chainlink lags Binance ~1s; crowd reaction lags more). When a lookahead-free
binary model on lagged Binance says P ≥ 0.995 and the standing ask (size ≥ order) is ≤ 0.97:
cross it and hold ~30s to settlement. Taker fee at these prices: 0.2–0.7c.
- Train (Apr 2 – May 15, exact oracle K): 5m **+4.3c/share** (t=6.4, 31 trades/day, median day +3.2c),
  deep-disagreement subset (ask ≤ 0.80) **+10.4c** (t=8.0, 13/day);
  15m T−15s: +2.4c (7/day).
- OOS: **{TBD_S2}**
- Latency stress: with a 2-full-seconds-stale signal it still earns +3.0c (Apr) / +13.9c (May) —
  the edge is not a milliseconds race at $5–10 size.
- PnL is vol-event-skewed (May 1–2 = 46% of Apr–May PnL) but 78% of days positive.
- Feb–Mar train numbers were near zero **because the backtest lacked the true oracle open price
  (K) before Apr 2**; a live bot always has K exactly → Apr+ numbers are the honest estimate.

### Portfolio note
All three run simultaneously off one Chainlink + one Binance websocket and the Polymarket CLOB API.
They are mutually independent (different windows of the market's life) and share the $100 bankroll:
**{TBD_PORTFOLIO}**

## What was tested and ruled out (the full register: results/results_table.csv)

| Area | Hypothesis | Verdict | Why |
|---|---|---|---|
| A | Naive Φ(d) model vs market | DEAD | market better calibrated (Brier 0.176 vs 0.200) |
| A | Model-market dislocation reversion | MERGED | tradeable residue = #2 and #3 |
| B | Pre-open herding fade (B1) | DEAD | tilt is informed & fully priced; −4c at executable ask |
| B | Pre-open flow momentum (B2) | DEAD | −1.8 to −2.8c after honest fills |
| B | Book-imbalance side signal (B4) | DEAD | AUC 0.497 alone |
| B | **The user's pre-open scalp (B3)** | DEAD as mechanical | −0.3 to −3.1c across ML/flow/unconditional gates, entries 48–52, targets 53–60, aborts settle/taker: capped +4c winners can't pay for −50c losers at 50c entry. Live profitability must come from discretionary skips or variance |
| B/G4 | ML side classifier (LGBM, 29 features) | WEAK | AUC 0.536 CV; favored side already costs 52–53c; taker EV +0.5–2.4c, not robust |
| C | Naive late-window favorite taking (C3) | DEAD | late ask efficiently priced (−1 to −2c) |
| C | First-second stale-quote taking (C1) | DEAD | ask at +1s already moved (−1.5c) |
| C | Calibration treasure map (C5) | FOUND | → strategy #2 (also proved 15m/4h/1h versions unstable) |
| D | 4h favorite harvest | DEAD | +0.4c pooled, −2.6..+5.0c by month (no power) |
| D | 1h favorite harvest (late window) | WEAK | +1.3c at mid, unstable, spread eats it |
| D | 1h slight-leader late | DEAD | month-sign flips = noise |
| E | 5m↔15m lead-lag arb (E1/E4) | DEAD | ρ=0.40 contemporaneous, zero at ±15s |
| E | Oracle lag snipe (E2) | FOUND | → strategy #3 |
| E | 1h T−30s snipe | DEAD | −9.5c; 30s too early, book too smart |
| F | Streak fade/follow (F1/F2) | DEAD | crowd prices streak mean-reversion correctly |
| F | Thin-book overshoot reversion (F3) | DEAD | thin and thick books revert identically |
| F | Time-of-day concentration (F6) | absorbed | #2 works all hours (+1.8 to +4.6c per block) |
| G | Volume-based skip rule (G2) | DEAD | non-monotonic noise |
| G | Post-close winner-dump toll (G1-adjacent) | FOUND | → strategy #1 |

Also audited and used: fee regime history (0 → 0.0624 Jan 5 → 0.072 Mar 30 → 0.07 May 7);
vault tick-data gap May 13 – Jul 5 (recovered from Telonex for true OOS validation);
result orientation (token 0 = Up; result 0 = Up won) verified against tape and oracle.

## Execution playbook (for the survivors)
- One process, three websockets: Chainlink BTC/USD (poll the same aggregator Polymarket uses),
  Binance BTCUSDT trade stream, Polymarket CLOB user/market channels.
- #1: at each window close, read the close print, post GTC bid 0.992 × 15 shares on the winner, cancel at settlement.
- #2: at open+60s, if favorite ∈ [0.80, 0.93]: post GTC bid at the favorite's best bid, cancel at +105s, hold fills to resolution.
- #3: from T−6s, recompute P(win) each 250ms from Binance-lagged model with exact K; if P ≥ 0.995
  and standing ask ≤ 0.97 with size: IOC buy. Hold to settlement.
- All three are pure API plays at $5–10/clip; none require sub-second infrastructure
  (#3 tolerates 2s of latency; #1 tolerates ~5s).

## Data assets produced (all pushed to the vault)
`data/processed/daily/1h/{trades,quotes}` (275 days, consolidated), `binance/{klines_1s,btc_1s,chainlink_1s}`,
`tlx/btc_updown_markets.parquet` (93k-market catalog), `windows_full.parquet` (92,741 windows),
`features/*` (per-window snapshots, path grids, trade aggregates, masters, model grids),
plus this repo's builders/backtester (`strategy_lib/`, `scripts/`).
Gap tick data (May 13 – Jul 5 + Jul 8–13) fetched from Telonex; Binance klines re-fetchable
from binance.vision if the vault cap blocks their upload.
