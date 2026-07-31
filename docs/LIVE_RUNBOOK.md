# LIVE RUNBOOK — Polymarket BTC snipe, real money

This is the operating manual for taking the validated snipe strategy live.
Written as a handoff: any competent operator (human or AI) should be able to
execute from here without re-deriving anything. Strategy validation history is
in REPORT.md; this file is only about running real money safely.

## What is already built (repo state)

- `bot/` runs the SAME code in paper and live; only the executor differs.
- Paper fleet (BTC toll + BTC/ETH/SOL snipe) runs on the NYC droplet
  (165.227.83.238) — it is the permanent control group. Its snipe fills pass a
  0.5s live-fidelity gate, so paper P&L ≈ what live should capture.
  The gate is PAPER-ONLY (enforced in code): in live mode the bot fires the FAK
  immediately — its latency is real, and the exchange decides the race. Never
  "re-add" a pre-send wait to live; it would forfeit ~every contested ask.
- `bot/engine/live.py`: LiveExecutor (FAK marketable-limit takes, sweep-sized,
  real matched size/price recorded from the exchange response), Bankroll
  (manual `BANKROLL` only — the bot NEVER sizes off the account balance),
  hourly spend-side balance reconciler (halts on unexplained shortfall).
- Retry policy: up to `SNIPE_MAX_ATTEMPTS=3` takes per window while the signal
  persists, capped by clip (250 sh), per-window cost (`SNIPE_WINDOW_MAX_COST`)
  and bankroll per-trade cap. A missed FAK costs nothing and is retried.
- Three locks before any real order (ALL must be opened deliberately):
  1. `BOT_MODE=live` + `BANKROLL>0` + `PM_PRIVATE_KEY` present
  2. `LIVE_CONFIRM=I-UNDERSTAND-REAL-MONEY` (needed only to POST; shadow runs
     without it)
  3. `LIVE_SHADOW=0` (default is 1 = full pipeline INCLUDING signing, no posts)

## Money rules (enforced in code, not by discipline)

| Rule | Value | Where |
|---|---|---|
| Per-trade cost | ≤ 10% of bankroll AT THE LIMIT PRICE (worst-case sweep) | live.py cap_sz + overspend tripwire |
| Position at a time | 1 | live.py _in_flight |
| Per-take size | ≤ 250 shares (audited: bigger = −EV) | config snipe_max_clip |
| Per-window cost | ≤ $300 across retries | config snipe_window_max_cost |
| Daily stop | −20% of bankroll → STICKY halt in live (human restart) | risk.py via bankroll |
| Cumulative stop | −50% of bankroll lifetime → sticky halt | risk.py live_max_drawdown_frac |
| Trades/day | ≤ 40 (persisted across restarts) | live.py |
| Live strategies | snipe only (toll NOT live-qualified) | LIVE_STRATEGIES |
| Ambiguous fill | booked worst-case + sticky halt (never dropped) | live.py _parse_fill |
| Reconciler shortfall | sticky halt via RiskManager (not just a log line) | live.py balance_reconciler |

## The bankroll ladder (manual; reviewed weekly, move ONE rung)

$150 → $300 → $600 → $1,200 → $2,400 (ceiling — beyond this the 250-share cap
binds and extra money earns nothing). Up one rung after a profitable,
zero-incident week; down one rung after a losing week. Change = edit
`BANKROLL=` in `/etc/polybot/live.env`, `systemctl restart polybot-live-btc`.
"Incident" = any mismatch, reconciler alert, unexplained fee, stuck order.

## Go-live sequence (DO NOT REORDER)

1. **Server**: non-US VPS (US IPs cannot place orders). Provision like the
   paper droplet: clone repo, `python3 -m venv venv`,
   `venv/bin/pip install aiohttp py-clob-client-v2`, run `venv/bin/python -m
   bot.preflight` (all PASS required), install chrony.
2. **Secrets**: `mkdir -p /etc/polybot && cp bot/deploy/live.env.template
   /etc/polybot/live.env && chmod 600 /etc/polybot/live.env`; owner fills
   PM_PRIVATE_KEY / PM_FUNDER on the server keyboard, never via chat/email.
3. **Shadow 24–48h**: install `bot/deploy/polybot-live-btc.service`, start with
   `LIVE_SHADOW=1` (LIVE_CONFIRM not needed for shadow). Shadow SIGNS every
   order it would send — so it validates credentials, signature type, funder,
   tick-size and neg-risk resolution, not just the signal pipeline. Verify in
   journal: `[SHADOW] signed+would take ...` lines at sensible sizes, balance
   readable at startup, zero errors. Compare shadow takes against the NYC
   paper bot's fills for the same windows — they should largely agree.
4. **Before flipping real**: in the Polymarket UI confirm the account's actual
   signature type (Settings) matches PM_SIGNATURE_TYPE, and enable
   "Auto redeem your wins" — unredeemed winnings are NOT buying power, and a
   bot that can't buy looks identical to a bot that can't fill.
5. **Tier 0**: set `LIVE_SHADOW=0`, `LIVE_CONFIRM=I-UNDERSTAND-REAL-MONEY`,
   `BANKROLL=150`. Restart. 3–5 days. The goals are MEASUREMENTS, not profit:
   (a) real fill-through rate vs paper's gate (expect roughly parity);
   (b) THE decisive one — for each fill compare realized avg price vs the
   triggering best ask (`grep live_forensics` + LIVE FILL lines): if live's
   average entry sits near the 0.97 limit while the triggering asks were deep,
   the deep-band edge is a sweep artifact and the strategy must stop;
   (c) actual fee charged (expect 0.07·p·(1−p) taker); (d) zero reconciler
   alerts. Emergency exit: `systemctl stop polybot-live-btc`, redeem in the
   UI, withdraw USDC to your own wallet — nothing in the repo moves funds.
6. **Ladder** per the table above. Keep the paper fleet running forever as the
   control group; investigate any paper-vs-live divergence before sizing up.

## Redemption (v1: manual)

Winning shares redeem at $1.00 after resolution. v1 process: claim in the
Polymarket UI daily; the reconciler treats redemptions as additive only, so
unclaimed winnings never trigger a false halt — but claim before the balance
runs low, since buying power comes from collateral balance. (v2 automation:
see research brief below.)

## Daily operator checks (2 minutes)

```bash
journalctl -u polybot-live-btc --no-pager | grep -E "LIVE FILL|SHADOW|HALT|reconciler" | tail
BOT_DATA_DIR=bot/data/live-btc venv/bin/python -m bot.report 7
```
Red flags: any HALT, reconciler shortfall, `mismatches>0`, win-rate over
trailing 30 fills far below ~75%, or live fills/day wildly below the NYC paper
bot's gated fills/day (means we're slower than assumed — investigate latency
before anything else).

## Known limitations / next work (for the next session)

- User-channel websocket (authenticated fill stream) not yet wired; v1 uses
  the FAK response + can poll `get_trades`. Wire it for real-time fee capture.
- Redemption automation (relayer / redeemPositions) — v1 manual.
- Toll strategy live = NOT qualified (queue race unproven); revisit only after
  live snipe data shows our actual queue position.
- ETH/SOL live only after their own 2-week paper qualification.

## Research briefs (appended)

See `docs/research/` for the API-mechanics brief and the retry/sweep EV study
that set the parameters above; they carry VERIFIED/INFERRED markings — trust
them in that order.

## Weekly parameter watch (from the retry/sweep study)

- Bucket the week's snipe fills by entry price (<=0.80 vs 0.90-0.97). The
  Jun-Jul reconstruction says deep asks decayed to -EV while live paper still
  prints +EV there. If a week of deep fills is net negative:
  `SNIPE_PRICE_FLOOR=0.90` in the unit env and restart. Do not raise the 0.97
  ceiling under any circumstances (0.97-0.985 measured significantly -EV).
- Attempts histogram: if 3rd attempts are net losers over a week, drop
  SNIPE_MAX_ATTEMPTS to 2.

### Reading log (append each check)

- 2026-07-31 15m FAMILY RE-TEST (fresh month, current instruments) — ALIVE.
  The old "dead" verdict came from the artifact-corrupted early study
  ("corrected ~ 0", pre-gate tooling). Fresh candidate tape, btc-updown-15m
  Jun 30-Jul 30 (2,782 windows; Telonex missing ~192 recent files):
  BOT-AS-IS (same params, T=900): 151 entries (5.2/day), win 85.4%,
  +8.52c/sh weighted, +$1,135/29d (~$39/day at <=250sh clips); halves
  +9.67c/+7.79c. Deep band +26.3c/sh (halves +25.96/+26.48, n=40).
  Competition tax (gate-off vs gated) only ~12% vs 5m's ~47% — few snipers
  watch 15m. Current timing already optimal (T-12/T-9/T-4.5/T-3 variants all
  <= baseline pnl). Same caveats as all replica work (understating proxy
  instrument; one coin; one month). ACTION: paper bot deployed as referee —
  bot/deploy/polybot-snipe-btc15.service (FAMILY=15m, WINDOW_SECS=900,
  SLUG_PREFIX=btc-updown-15m, warmup 1800s); code generalized (WINDOW_SECS
  env). Its own 2-week paper gate before any live thought. If paper
  corroborates, 15m adds ~40% to btc capacity at LOWER competition.

- 2026-07-31 IMPROVEMENT SEARCH (post-audit; "search for everything").
  Method: candidate tapes (EVERY qualifying touch per window, loose gates,
  scripts/candidate_tape.py) over 41d eth/sol/doge + 12d btc from Telonex,
  exact re-simulation of 12 parameter variants per coin
  (scripts/analyze_tape.py), split-half day validation, and a TWO-WITNESS
  rule: no change unless the replica tape AND the true-feed ledger agree.
  RESULTS — NO CHANGES APPLIED ANYWHERE, each rejection evidenced:
  * Time-of-day: no losing 4h block survives split-half on any coin, and the
    ledgers show every block net-positive (btc blocks $126-655; eth all
    positive incl. the 38%-win 12-16 block, which is +EV via cheap entries).
    Apparent bad blocks = regime days (Jul 24) landing in one half. Rejected.
  * Day-of-week: "bad" days appear at chance rate (~25% per dow per coin of
    both-halves-negative under zero edge) and contradict across coins
    (eth Fri/Sun weak vs doge Fri/Sun strongest). Rejected.
  * BTC scans: everything loses or ties. ask_max 0.95 LOOKS +$600 better but
    is the known instrument artifact (replica reads 0.90-0.98 band -2.2c
    where the true-feed ledger prints +3.6c on 114 fills). Floors -$450 to
    -$630 (deep band remains the engine). Gate-off fantasy = 1.9x gated pnl
    (measured cost of rival takers). Rejected all.
  * ETH scans: two split-half survivors (fv_min 0.999 +$670 vs +$442;
    eval-from-T-3 +$802) REFUTED by the ledger early/late bucket test:
    real early fills (T-6..T-3) EARN MORE than late fills on both coins
    (eth +14.31c vs +12.22c; btc +8.00c vs +6.73c). The replica's
    early-entry weakness is proxy-signal noise at long horizons, not market
    reality. Rejected; do NOT re-tune eth off the replica.
  * SOL: tape baseline -4.57c is the BLIND INSTRUMENT (feed-noise ratio
    1.03), not the strategy — ledger (true feed) prints +9.4c/96% on n=25.
    No tape-based tuning is valid for sol, ever; ledger is sole referee.
  * DOGE: deployed floor-0.90 config positive in both halves (+2.03c overall,
    97.8% win, n=185). fv_min 0.999 improved both halves (+$297, n=127) but
    NOT applied: the live paper bot's issue is zero fills in 4d (true-feed
    top-band asks are scarcer than the proxy suggested) — tightening a
    silent bot deepens silence. Revisit both at its 2-week verdict (~Aug 10);
    if still ~0 fills, retire the bot.
  CONCLUSION: current per-coin configs sit at their measured optima; the
  search's value is the documented proof that no obvious knob was left
  unexamined. Droplet upgraded to the audit-fix build (d05685f) same day.

- 2026-07-30 EXTERNAL ADVERSARIAL AUDIT (independent Opus-model auditor,
  full code + docs read; verdict NO-GO) — 19 findings, 5 critical, ALL
  verified against the code and ALL blockers fixed the same day:
  #1 reconciler "halt" only wrote a ledger row nothing read -> now trips a
  real sticky RiskManager halt; #2 confirmed-but-ambiguous order responses
  (matched/delayed/tradeIDs without parseable amounts) were booked as misses
  -> now booked as worst-case provisional fills + sticky halt; #3 per-trade
  cap was computed at best_ask while the FAK sweeps to the 0.97 limit (could
  spend ~5x cap) -> cap now at limit price + post-fill overspend tripwire;
  #4 post_order blocked the event loop up to 30s polling trade hashes -> live
  takes run in a worker thread + poll bounded to 2s + tick/neg-risk caches
  prewarmed at discovery; #5 shadow phase couldn't start (LIVE_CONFIRM
  required in constructor) -> confirm now required only to POST, shadow signs
  orders (exercising creds/allowances), StartLimit added so config errors
  stop the unit instead of crash-looping. Also fixed: mismatch tripwire now
  works with toll disabled and halts ALL strategies (#6); daily stop sticky
  in live + new cumulative -50% bankroll sticky stop (#7); trades/day cap
  persisted across restarts (#14); runtime clock-skew guard from the oracle's
  server-stamped samples (#13); reconciler math measures since-process-start
  and aborts startup on unreadable/short balance (#12); fill-forensics
  logging for the Tier-0 sweep-artifact test (per the auditor's edge
  challenge); preflight filter fix (#17); service MemoryMax 300M +
  StartLimit + NoNewPrivileges (#19); _NullOrder attrs (#16); runbook/table
  drift corrected (#15). REMAINING accepted risks, monitored not fixed:
  paper's sweep assumption may overstate deep-band capture (Tier-0 forensics
  measurement decides, ~20 fills); user-channel ws still unwired (v2 item);
  redemption manual until "Auto redeem" is enabled in the UI (step 4).

- 2026-07-24 (BTC paper, lifetime to date): <=0.80: 117 fills +$1,369 (+16.5c/sh);
  0.80-0.90: 40 fills -$79 (-3.0c/sh, NOT significant at n=40); 0.90-0.98:
  114 fills +$494 (+3.6c/sh). VERDICT: deep band is the profit engine — the
  reconstruction's decay warning is contradicted by live paper; price floor
  stays OFF. Watch the 0.80-0.90 mid-band: if still negative at n~100, consider
  a mid-band skip. Trailing breaker fired correctly 07-24 01:45 (-$267 fast
  bleed) and was cleared by restart after this check.

- 2026-07-27 NEW-COIN EXPLORATION (xrp/doge/bnb — the only other coins with
  updown-5m markets; ada/link/ltc/avax/sui/pepe/shib/trx/matic have none).
  Liquidity (median $ volume/window, 50-window sample): btc $57.8k, eth $5.2k,
  sol $1.8k, xrp $618, doge $335, bnb $161 — new coins are 90-350x thinner than
  btc; capacity is small even if an edge exists. Feed validity (15-min live
  chainlink-vs-binance, p90 basis-change / signal threshold): bnb 0.37 (cleanest
  of all 6), btc 0.44 (control, replicates), xrp 0.60, doge 0.67 — all three
  new coins have working chainlink streams and NONE is SOL-blind (1.03), so the
  replica verdicts below are meaningful (with the known ~4c/sh understatement
  measured on btc). Replica replay Jul 14-25 at <=250sh clips:
    XRP: 143 opps (11.9/d), win 76.9%, -10.9c/sh, -$1,217; ALL bands negative
      (deep -24.6c at 42% win, mid -13.6c, top -4.2c). NO-GO — dead even after
      bias correction.
    DOGE: 100 opps (8.3/d), win 81.0%, -1.9c/sh, -$87; deep -10.3c (41% win),
      mid -6.3c, BUT top band (0.90-0.98) +3.0c at 98% win on n=56 — positive
      DESPITE the blurriest instrument. Bias-corrected overall ~breakeven to
      positive. CANDIDATE for a paper-bot experiment.
    BNB: 159 opps (13.2/d), win 76.7%, -6.6c/sh, -$589; deep band +17.6c
      (n=34) — btc-like engine — but mid -37.6c and top -9.9c with the CLEANEST
      instrument. Mixed. CANDIDATE for a paper-bot experiment; if the pattern
      holds on the true feed, bnb would want a price CEILING near 0.80 (inverted
      from the usual floor logic).
  DECISION: no live candidacy for any new coin. Optional cheap experiments:
  paper bots for doge and bnb (NOT xrp) if droplet RAM allows (512MB already
  runs 4 bots — check free -m first; upgrade droplet before adding, or skip).
  Their realistic full-size ceilings are ~$5-20/day each given thin volume.
  Fleet priorities unchanged: btc live > eth gate (~Aug 2) > sol sample > this.
  FINAL (same day, 41-day extended sample Jun 15-Jul 25, ~11.8k windows/coin,
  specialized-band bot simulation + day-block bootstrap):
    XRP: NO-GO (stands — all bands negative on a valid instrument).
    BNB: NO-GO — CLOSED. The 12-day deep-band +17.6c halved to +10.1c at n=64,
      and the honest deep-only bot simulation prints +1.63c/sh with a CI of
      [-13.9c, +15.7c], P(EV<=0)=40.5%, ~$2/day ceiling. Small-sample mirage;
      not worth a bot slot. Do not revisit without a new reason.
    DOGE: GO for a PAPER experiment — top-band-only bot [0.90,0.97] over 41
      days: 185 entries (4.5/day), win 97.8%, +2.03c/sh on an instrument that
      understates by ~4c (btc calibration), 35/38 days positive,
      P(EV<=0)=14.6%. True edge plausibly +4-6c/sh, ceiling ~$6-15/day.
      Deploy: bot/deploy/polybot-snipe-doge.service (SNIPE_PRICE_FLOOR=0.90 is
      the strategy — deep/mid doge bands are -5 to -7c). PAPER ONLY; its own
      2-week gate + band bucket check before any live thought. Add only if
      free -m shows >=150MB headroom on the droplet (or after a resize).

- 2026-07-27 ETH/SOL EDGE-FRESHNESS AUDIT (same replica as BTC's, Jul 14-25):
  the proxy replay could NOT confirm the eth/sol edges the way it confirmed
  btc's. ETH: 171 opps (14.2/day), win 80.1%, -0.44c/sh, -$52 (deep band +7.7c
  but only 54% win; mid -14.1c; top -0.5c). SOL: 80 opps (6.7/day), win 81.2%,
  -9.87c/sh, -$702 (deep band -31.4c at 53% win = the whole loss). Binance-proxy
  direction accuracy is NOT the issue (~5% miscalls overall, ~0% on decided
  windows); the failure mode is marginal-window fv confidence, hypersensitive
  to proxy-vs-chainlink basis in the final seconds. Meanwhile the true-feed
  paper bots printed ETH +$342/85.1%/+8.3c (101 fills, 7/7 days positive) and
  SOL +$187/95.5%/+10.0c (22 fills, 5/5 days positive), zero mismatches.
  READ: for BTC the edge was strong enough to survive the blunt proxy
  instrument; for eth/sol it is not (proxy noise and/or genuinely informed deep
  asks on thinner books). VERDICT: eth/sol remain paper-only and their 2-week
  qualification gate is HARD — no live shortcut on a pretty paper week; add a
  price-band bucket check to their weekly watch (deep band especially for SOL);
  no parameter changes to the paper bots (they are the referee).
  ADDENDUM (same day) — instrument validity MEASURED, divergence resolved:
  (a) quote staleness ruled out: replica entry quotes are fresh on all coins
  (p90 age <=0.8s); an age<=3s filter and a +1-sigma signal margin change
  nothing qualitatively (btc stays positive in all variants, eth/sol negative).
  (b) 15-min live capture of chainlink (resolution truth) vs the binance
  stand-in, per coin, 3s decision horizon: |basis-change| p90 as a fraction of
  the fv signal threshold = BTC 0.41, ETH 0.42, SOL 1.03. SOL's stand-in noise
  EQUALS the signal itself -> the replica is BLIND on SOL; its -$702 is
  instrument artifact, corroborated by fire-rate (replica 6.7/day vs true-feed
  bot 2.4/day = phantom signals). For BTC/ETH the instrument is usable but
  understates the true-feed bot by ~4c/sh (BTC calibration: replica +2.5 vs bot
  +6.6). Applying that to ETH's replica -0.4c/sh -> consistent with a real
  positive edge, weaker external confirmation than BTC's.
  FINAL: BTC GO unchanged. ETH on track — complete the 2-week gate (~Aug 2)
  + band buckets, then eligible for its own Tier 0. SOL: replica cannot
  referee it and bot n=22 is too small — extend paper qualification until
  n>=60-100 fills before any live decision. No parameter changes.

- 2026-07-26 EDGE-FRESHNESS AUDIT (independent of the bots): replayed the snipe
  rules over 3,455 never-before-analyzed windows (Jul 14-25, Telonex ticks +
  gamma official results, binance-proxy signal, 0.5s survival gate, first-touch
  only): 376 opportunities (31/day), win 75.5%, +2.52c/sh weighted, +$1,056 at
  <=250sh clips; 8 of 12 days positive. Deep band (<=0.80) independently
  confirmed as the engine: +11.5c/sh. Top band (0.90-0.98) measured -2.2c here
  vs +3.5c in the paper ledger over the same days — most likely proxy-oracle
  noise (replica lacks the chainlink feed; marginal-window misclassification
  penalizes the high-price band hardest); paper (true oracle) is the better
  instrument there. WATCH both; no parameter changes.
