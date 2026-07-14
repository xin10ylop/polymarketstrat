# Data guide for analysis (read me first)

All prices are the **Up token** (token 0). `result==0` → Up won, `result==1` → Down won.
Buying Down at q ≡ selling Up at 1−q. The Up tape reflects all activity (complement matching).

## Ground rules
- **Train/test split: TEST starts 2026-05-16 (all families). Only tune/tabulate on dates < 2026-05-16.** Report train numbers; the final validation pass on test happens later, once, per surviving strategy.
- No lookahead: a feature at offset `o` may only use data with timestamp ≤ wts+o. `result`, `close_chainlink` only for scoring.
- Fees: maker = 0. Taker fee per share = `rate × p × (1−p)`, current rate 0.07 (`strategy_lib.data.taker_fee`). Historical rates in `windows_full.fee_rate`.
- Trade size assumption: $5–10 → 10–20 shares; fills at that size are reliable (verified live by the account owner).

## Files (relative to repo root)
- `data/windows_full.parquet` — one row per market: slug, family (5m/15m/1h/4h), wts (open, s), duration, close_ts, date, result, settled_at_us, open/close_chainlink (Apr 2+ only), volume_usdc (vault subset), fee_rate.
- `data/features/{fam}_master.parquet` (fam ∈ 5m/15m/4h) — one row per window; windows_full columns plus:
  - Snapshots: `bid_price@{o}`, `ask_price@{o}`, `bid_size@{o}`, `ask_size@{o}` for offsets o ∈ {-1800,-600,-300,-120,-60,-30,-15,-10,-5,-2,-1} (pre-open), {1,2,3,5,10,15,20,30,45,60,90,120} (post-open, capped), {25%,50%,75% of T}, {T-60..T-1}, {T+2..T+40} (post-close). Offset key is absolute seconds vs open (e.g. `bid_price@295` for 5m T-5).
  - Book (vault fams): `bid_depth_5c@{o}`, `ask_depth_5c@{o}`, `buy_avgpx_200@{o}`, `sell_avgpx_200@{o}`, `buy_avgpx_1000@{o}`, `sell_avgpx_1000@{o}`, `bid_p0/ask_p0/bid_s0/ask_s0@{o}` for o ∈ {-30,-10,-5,-1,1,5,10,30}.
  - Trades aggs: `{iv}_n`, `{iv}_vol`, `{iv}_signed` (buy−sell shares), `{iv}_vwap`, `{iv}_last_px` for iv ∈ pre3600, pre300, pre60, pre10, post10, post30, post60, mid (40–60% of T), end30, end10, postclose (T..T+45).
  - Binance at open: `S_open`, `r_5s r_15s r_30s r_60s r_300s r_900s r_3600s` (log returns ending at open), `vol_60 vol_300 vol_900` (1s-return std, per √s), `tbr_60 tbr_300` (taker-buy ratio).
  - `K` = price to beat (chainlink open, else Binance), `has_cl` flag.
  - `prev_result`, `prev_streak` (signed: +k = k consecutive Up outcomes immediately before), `hour_utc`, `dow`.
- `data/features/{fam}_grid_model.parquet` — long grid per window (step 5s/15s/30s/120s by family), columns: wts, off (s vs open, −60..T+40), bid/ask price+size, result, ts_s, close (Binance S_t), vol_300/vol_900, S_open, K, tau, fv (model fair value Φ(log(S/K)/(σ√τ))), mid.
- `data/binance/btc_1s.parquet` — global 1s Binance series (ts, close, r_*, vol_*, tbr_*).
- `data/binance/chainlink_1s.parquet` — Chainlink 1s (ts, cl_price, server/local receipt ts), 2026-04-02+.
- `data/daily/{fam}/{trades,quotes,bookcurves}/{date}.parquet` — raw tapes (vault families).
- `data/tlx/1h/{trades,quotes}/{date}/{slug}.parquet` — 1h family raw (string prices; date = event date).

## Tools
- `strategy_lib.backtest.TapeBacktester` — maker-fill simulation vs the real tape (see docstring).
- `strategy_lib.model.fair_value` — binary option model.
- `strategy_lib.master.build_master / build_grid_model`.

## Results protocol
Append rows to `results/frag_<agent>.csv` with header:
`id,area,hypothesis,family,period,method,metric,value,n,verdict,notes`
verdict ∈ {PROMISING, WEAK, DEAD}. Every tested variant gets a row, including failures.
Write a companion markdown `results/notes_<agent>.md` with the key tables/numbers.
