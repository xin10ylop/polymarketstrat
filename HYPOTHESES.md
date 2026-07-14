# Polymarket BTC Up/Down — Hypothesis Register

Every hypothesis gets tested and logged in `results/results_table.csv` (win or lose).
Prices are always the **Up token** (token 0; `result_id=0` means Up won).
Fees: taker = `rate × p × (1−p)` per share (rate history: 0 → 0.0624 [Jan 5 2026] → 0.072 [Mar 30] → 0.07 [May 7, current]); **maker = 0** (+ rebates, ignored conservatively).
Final numbers always use the current regime: taker 0.07, maker 0.

## Market mechanics (verified from data)
- Markets are listed hours before the window opens; pre-open trading exists (~3% of trades).
- The "price to beat" = Chainlink BTC/USD at window open (`open_chainlink`); resolution = Chainlink at close, settled ~30s after close.
- Trading continues after window close until settlement (~30s) — outcome already determined.
- Families: 5m, 15m, 1h (ET-hourly, via Telonex), 4h.

## A. Model-value dislocations (binary option pricing)
- **A1**: Up token fair value = Φ((S−K)/(σ√τ)) with S = Binance spot, K = open Chainlink, σ = realized vol. Market mid deviates from model; deviations mean-revert → maker-buy underpriced side, exit on convergence/settlement.
- **A2**: Model edge is largest mid-window (30%–80% elapsed) when the crowd anchors to stale prices after BTC moves.
- **A3**: Near expiry the crowd under-updates: when model says >0.97 and market asks ≤0.93, taking the ask is +EV even with taker fee (fee is tiny near extremes: 0.07×p(1−p) ≤ 0.5c at p≥0.92).
- **A4**: After a sharp BTC candle (>1.5σ in 10s), the Up token overshoots the model value, then reverts → fade the overshoot with maker orders.
- **A5**: The model itself beats the market's implied probability out-of-sample (Brier score) → any systematic gap is monetizable at the touch.

## B. Pre-open behavior
- **B1**: Pre-open price should be ~50c ± tiny drift; when pre-open mid deviates (e.g. 53c+), it reverts after open → fade pre-open herding.
- **B2**: Pre-open flow direction (signed taker volume in last 60s before open) predicts the first post-open move (momentum from informed pre-positioning).
- **B3**: The user's scalp: buy ≤51c pre-open, sell 55c post-open. Side selection via Binance momentum at open determines which side wins this trade.
- **B4**: Book imbalance at open−10s (bid vs ask depth within 5c) predicts the winner.
- **B5**: Pre-open cheap side (ask ≤ 48c) is systematically mispriced when Binance short-horizon drift is aligned.

## C. Window-life map
- **C1**: First 10s post-open: price jumps from ~50c toward model value — buying the model-favored side in the first second at the stale quote is +EV.
- **C2**: Mid-life reversion: price paths overshoot relative to final outcome frequency (calibration curve by time-remaining shows bias).
- **C3**: Last 60s / 30s / 5s: favorite is systematically underpriced (longshot bias) → buy favorite late.
- **C4**: Post-close pre-settlement: quotes persist at non-degenerate prices after the closing Chainlink print is knowable → take free money (needs taker fee check + latency sensitivity).
- **C5**: Calibration by price bucket and time bucket: find (t, p) cells with |realized − implied| > fees → trade those cells directly.

## D. Longer families from scratch (1h, 4h)
- **D1**: Same model-value dislocation (A1) works on 1h/4h with more room (less bot competition).
- **D2**: 4h markets misprice overnight/weekend vol (σ regime mismatch) → systematic side bias by time-of-day/week.
- **D3**: 1h ET markets: open/close at ET hour boundaries interacts with US market hours (9:30 open, 4pm close vol spikes) → model with intraday vol seasonality beats flat σ.
- **D4**: Early-life 4h prices anchor at 50c long after BTC has moved → buy the favored side in first minutes of 4h window.

## E. Cross-market / oracle
- **E1**: 5m price inconsistent with running 15m market (same underlying, overlapping window) → arb the incoherence.
- **E2**: Chainlink lags Binance by measurable ms/s; in final 10s the Binance print predicts resolution better than market price → late-window sniping keyed to Binance-Chainlink basis.
- **E3**: Oracle update cadence: Chainlink deviation-threshold updates create discrete jumps; when spot sits near the price-to-beat, oracle quantization creates predictable coin-flips priced as non-coin-flips.
- **E4**: 15m and 5m windows sharing an endpoint: the 5m near expiry implies the 15m's conditional distribution → relative value.

## F. Crowd behavior / game theory
- **F1**: Gambler's fallacy: after ≥3 same-direction outcomes, next-window pre-open price tilts against continuation though BTC 5m returns are ~iid → buy continuation side pre-open.
- **F2**: Hot-hand chasing: after a big Up candle window, next window's Up is bid above 50c pre-open → fade.
- **F3**: Thin-book moments: when top-5-level depth < X shares, single $10 orders move price several cents; those moves revert → provide liquidity (maker) at wides.
- **F4**: Spread-width regimes: when spread ≥ 4c mid-window, posting both sides inside captures spread with low adverse selection given model filter.
- **F5**: Retail size clustering: round-number order sizes (5/10/20 shares) dominate at certain hours; their aggressor direction is contrarian signal.
- **F6**: Time-of-day: overnight (UTC 0-6) books are thin and mispricings bigger; restrict any edge to those hours and EV/trade rises.

## G. Data-driven anomalies (audit-driven)
- **G1**: Quotes/trades exist after settlement time in data — check if executable (data artifact vs real).
- **G2**: Volume distribution per window is extremely skewed; low-volume windows have wider mispricings (skip-rule input).
- **G3**: fee_rate transitions (Jan 5, Mar 30, May 7) changed maker/taker mix and spread — post-fee spreads widened → maker edge grew; validate maker strategies only on post-fee data.
- **G4**: ML side-classifier: gradient boosting on all pre-open features (book, flow, Binance momentum, streaks, time-of-day) vs simple momentum rule — compare OOS accuracy and PnL.
- **G5**: Existence check: wallets/trade-size patterns showing consistent profitability (from tape asymmetries) reveal which windows the pros trade — imitate their timing.

## Validation protocol
- Train: first 60% of each family's date range. Test: last 40%, untouched until final validation.
- All backtests use maker-fill simulation against the real tape (fill = subsequent aggressor trade crosses our price; $5-10 orders ⇒ queue ignored, but require strictly-through price OR ≥2× our size at price).
- Sensitivity: fees (0.07 taker), fill haircuts (50% of marginal fills), latency (+250ms, +1s), and per-month PnL stability.
