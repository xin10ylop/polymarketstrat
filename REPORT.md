# Polymarket BTC Up/Down — Strategy Research Report

**Data**: 9 months (2025-10-11 → 2026-07-13) across four families — 5m / 15m / 1h (ET-hourly) / 4h —
92,741 resolved markets; full quote/trade tapes, 25-level book curves (vault families), 1-second
Chainlink oracle feed (Apr 2+), Binance BTCUSDT 1s klines for the whole period.
**Fees** (verified from official docs + fee_rate column in data): taker = `0.07 × p × (1−p)` per share
(≈1.75c at 50c, ≈0.33c at 95c); **maker = 0** (+ rebate program, ignored conservatively).
**Split**: tuned on data before 2026-05-16; validated out-of-sample on 2026-05-16 → 2026-07-13
(59 trading days, tick data recovered from Telonex for the vault's gap).
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
5. **Binance ≠ oracle**: from late May 2026 the Binance BTCUSDT price ran a persistent ~5 bp
   above Chainlink BTC/USD (a USDT discount). That dwarfs the 5-second vol that decides these
   windows: any model that compares a Binance price against the oracle strike **must estimate and
   remove this basis from the live oracle feed** (rolling 60s median of chainlink/binance works).
   Uncorrected, the late-window sniper flips from +4.0c to −1.2c/share on the test period — the
   single most dangerous artifact found in this project.

## RANKED VIABLE STRATEGIES (both survived walk-forward)

### #1 — Post-close winner toll ("settlement-lag liquidity provision")
**Edge type**: riskless toll collection. **Family**: 5m (+15m marginal).
After the window closes, the outcome is fully determined by the already-public Chainlink print,
but position-dumping continues for ~20s until settlement (median 4,000 shares/window of winner
tokens sold at ≤0.995). Resting a bid at **0.992 on the winning token at T+2s** collects
1.00 − 0.992 = **+0.8c/share, zero variance** (win rate 100% by construction; the only risk is
operational: reading the oracle print and posting within ~2s).
- Train (5m, Feb–May): 23,305 fills / 26,773 windows (87%), +0.800c/share, ~250 fills/day.
- **OOS (May 16 – Jul 13): 13,833 fills / 16,996 windows (81%), +0.800c/share, 231 fills/day,
  100% win, EV flat at +0.8c in May, June and July.** The edge cannot decay in price (it is a
  mechanical bound), only in fill rate — which held at 81%.
- Sensitivity (all on OOS): queue_mult 2→5: 214 fills/day (−7%); posting at T+4s instead of
  T+2s: 226 fills/day (−2%); limit 0.99 → fewer fills, +1.0c; 0.992 maximizes $/day.
- 15m OOS: 1,451 fills / 60 days = 24/day at the same +0.8c, but fill rate fell 36% → 26%
  (the 0.992 queue is getting crowded there). 1h adds ~6/day.
- **Capacity**: ~$25–40/day at 15-share clips. Erosion risk: other bots joining the 0.992+ queue.

### #2 — Oracle-lag terminal snipe (taker, last 5 seconds; basis-corrected)
**Edge type**: information latency. **Family**: **5m only** (T−5s; the 15m version is dead — see below).
In the final seconds, Binance spot has already decided the outcome, but the book still quotes
mid-range prices. Signal: basis-correct lagged Binance (S × rolling-60s-median(chainlink/binance)),
compute P(win) against the exact oracle strike K; when P ≥ 0.995 and the standing ask
(size ≥ order) is ≤ 0.97: cross it and hold ~30s to settlement. Taker fee at these prices: 0.2–0.7c.
- Train (Apr 2 – May 15, exact K + basis correction): **+10.5c/share** (t=10.6, 27 trades/day);
  deep-disagreement subset (ask ≤ 0.80) **+26.5c** (t=12.7, 10/day).
- **OOS (May 16 – Jul 13): +4.81c/share (t=5.2), 16.6 trades/day, every month positive
  (May +5.9c, Jun +3.5c, Jul +6.6c). Deep subset: +7.97c (t=4.2), 8.1/day, all months positive.**
- Latency stress (OOS): with a 2-full-seconds-stale signal still **+3.27c** (t=3.4) —
  not a milliseconds race at $5–10 size.
- The OOS edge is roughly half the train edge (competition arriving); it did not die, but expect
  continued decay. Feb–Mar train history is unusable (no oracle K before Apr 2), so the honest
  estimate rests on Apr 2+.
- **The 15m variant's spectacular in-sample number (+14.4c, t=15) was entirely the USDT-basis
  artifact** (mechanics #5). Basis-corrected it is −0.7c train / +0.8c test (t=0.45): dead.

### Failed walk-forward — 5m favorite harvest (was #2 in-sample)
The behavioral favorite-longshot edge (join the favorite's bid at +60s when it trades 0.80–0.93;
train +2.95c/share, t=3.7, positive every train month) **did not survive**: OOS +0.25c (t=0.27),
May negative, and every timing/queue variant insignificant (entry@45s −0.57c, entry@90s +0.55c,
t<1 on all). Win rate held (84.5%) but entry prices no longer carry the 2–6c discount — the
mispricing closed in mid-May 2026, consistent with competitors harvesting it. Do not deploy;
worth re-measuring monthly (the signal is one line of SQL) in case the crowd re-fattens.

### Portfolio (both survivors, OOS replay, chronological fills)
$100 float, flat $5 stakes, 59 test days: **+$952 total (mean +$16.1/day; median +$14.1;
worst day −$0.11; 98% of days positive; max drawdown −6.8%)**. Standalone: toll 5m +$558,
snipe 5m +$336, toll 15m +$59. The two strategies touch different seconds of the window's life
and never compete for the same fill; they share one Chainlink + one Binance websocket and the
CLOB API. At $5 clips the combined book never holds more than ~$30 at once, so a $100 bankroll
runs it comfortably; scaling stakes 3× is within observed queue depth for the toll, less certain
for the snipe.

## What was tested and ruled out (the full register: results/results_table.csv)

| Area | Hypothesis | Verdict | Why |
|---|---|---|---|
| A | Naive Φ(d) model vs market | DEAD | market better calibrated (Brier 0.176 vs 0.200) |
| A | Model-market dislocation reversion | MERGED | tradeable residue = #2 |
| B | Pre-open herding fade (B1) | DEAD | tilt is informed & fully priced; −4c at executable ask |
| B | Pre-open flow momentum (B2) | DEAD | −1.8 to −2.8c after honest fills |
| B | Book-imbalance side signal (B4) | DEAD | AUC 0.497 alone |
| B | **The user's pre-open scalp (B3)** | DEAD as mechanical | −0.3 to −3.1c across ML/flow/unconditional gates, entries 48–52, targets 53–60, aborts settle/taker: capped +4c winners can't pay for −50c losers at 50c entry. Live profitability must come from discretionary skips or variance |
| B/G4 | ML side classifier (LGBM, 29 features) | WEAK | AUC 0.536 CV; favored side already costs 52–53c; taker EV +0.5–2.4c, not robust |
| C | Naive late-window favorite taking (C3) | DEAD | late ask efficiently priced (−1 to −2c) |
| C | First-second stale-quote taking (C1) | DEAD | ask at +1s already moved (−1.5c) |
| C | Calibration treasure map (C5) | FOUND→DIED | 5m favorite harvest: +3c train, +0.2c OOS — edge closed mid-May 2026 |
| D | 4h favorite harvest | DEAD | +0.4c pooled, −2.6..+5.0c by month (no power) |
| D | 1h favorite harvest (late window) | WEAK | +1.3c at mid, unstable, spread eats it |
| D | 1h slight-leader late | DEAD | month-sign flips = noise |
| E | 5m↔15m lead-lag arb (E1/E4) | DEAD | ρ=0.40 contemporaneous, zero at ±15s |
| E | Oracle lag snipe 5m (E2) | FOUND | → strategy #2 (survives OOS only with USDT-basis correction) |
| E | Oracle lag snipe 15m | DEAD | in-sample +14.4c was pure USDT-basis artifact; corrected ≈ 0 |
| E | 1h T−30s snipe | DEAD | −9.5c; 30s too early, book too smart |
| F | Streak fade/follow (F1/F2) | DEAD | crowd prices streak mean-reversion correctly |
| F | Thin-book overshoot reversion (F3) | DEAD | thin and thick books revert identically |
| F | Time-of-day concentration (F6) | absorbed | favorite harvest worked all hours in-sample; died OOS anyway |
| G | Volume-based skip rule (G2) | DEAD | non-monotonic noise |
| G | Post-close winner-dump toll (G1-adjacent) | FOUND | → strategy #1 (validated OOS) |

Also audited and used: fee regime history (0 → 0.0624 Jan 5 → 0.072 Mar 30 → 0.07 May 7);
vault tick-data gap May 13 – Jul 5 (recovered from Telonex for true OOS validation);
result orientation (token 0 = Up; result 0 = Up won) verified against tape and oracle.
Known data caveats: trade tapes for 2026-05-13 (Telonex's own outage day) and 2026-07-13
(publication lag at fetch time) are partial (81/288 and 52/288 windows) — quotes are complete;
excluding those days moves no OOS number by more than 0.1c.

## Execution playbook (for the survivors)
- One process, three websockets: Chainlink BTC/USD (poll the same aggregator Polymarket uses),
  Binance BTCUSDT trade stream, Polymarket CLOB user/market channels.
- #1: at each window close, read the close print, post GTC bid 0.992 × 15 shares on the winner,
  cancel at settlement.
- #2: maintain basis = rolling 60s median(chainlink/binance); from T−6s, recompute
  P(win) each 250ms from basis-corrected lagged Binance with exact K; if P ≥ 0.995 and standing
  ask ≤ 0.97 with size: IOC buy. Hold to settlement.
- Both are pure API plays at $5–10/clip; neither requires sub-second infrastructure
  (#2 tolerates 2s of latency; #1 tolerates ~5s).
- Kill-switches: #1 — if a fill ever settles at 0 (oracle misread), halt; #2 — if daily PnL
  < −$5 or win rate over trailing 50 trades < 60%, halt and re-measure the basis estimator.

## Data assets produced (all pushed to the vault)
`data/processed/daily/1h/{trades,quotes}` (275 days, consolidated), `binance/{klines_1s,btc_1s,chainlink_1s}`,
`tlx/btc_updown_markets.parquet` (93k-market catalog), `windows_full.parquet` (92,741 windows),
`features/*` (per-window snapshots, path grids, trade aggregates, masters, model grids),
plus this repo's builders/backtester (`strategy_lib/`, `scripts/`).
Gap tick data (May 13 – Jul 6 + Jul 8–13, 5m/15m/4h quotes+trades) fetched from Telonex and
consolidated into `data/daily/`; Binance klines re-fetchable from binance.vision if the vault
cap blocks their upload. Validation artifacts: `results/validation_all.csv` (S1/S3),
`results/validation_s2.csv` (basis-corrected S2), `results/trades_*_test.csv` (every OOS fill),
`scripts/validate_survivors.py`, `scripts/bankroll_path.py`.
