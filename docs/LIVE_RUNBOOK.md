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

1. **Server**: non-US, non-restricted VPS. REGION DECISION (researched
   2026-08-02; CLOB origin = AWS eu-west-2 London): PRIMARY = DigitalOcean
   AMS3 Amsterdam (~8ms; NL is frontend-close-only but CLOB API open — KSA
   crackdown ongoing, so treat as revocable) -> FALLBACK = Vultr Madrid or
   Stockholm (~28ms, clean list). Blocked (do NOT use): US, UK, DE, FR, IT,
   BE, PL, PT, HU, SG, TW, TH, AU, CN, CA(ON/AB/BC/QC). Before committing
   ANY region (hourly billing = ~1 cent): POST {} to clob /order — 400/401
   = region works, 403/HTML = blocked, destroy; and check dynamic-endpoint
   TTFB ~0.02-0.06s. If the region dies mid-live, the H1 rejection fix
   halts cleanly (books nothing); snapshot-migrate to fallback. Provision
   like the paper droplet: clone repo, `python3 -m venv venv`,
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

- 2026-08-02 BIG-ASK VERDICT (depth-event join, pre-registered n>=50 rule):
  BTC-5m FINAL: n=60, 57/60 won, +13.7c/sh — big asks NOT adverse; raise cap
  250->500 AFTER Tier 0 (control group stays identical to record until live
  parity is measured). ETH-5m: n=18, 11/18, -20.9c — leaning ADVERSE, opposite
  sign; no action until n>=50. Per-coin verdicts only; never pool coins.

- 2026-08-02 FAMILY-TABLE RE-AUDIT (tape-derived): daily stops CONFIRMED
  (5m organic daily min -$195, 0/12 days breach -250; 650 floors clear one
  max loss). Cap headroom: 15m cap1000 would add ~+$100/mo (binds 1%), 1h
  +$21 — DEFERRED to post-gate (no mid-gate config churn). CORRECTIONS:
  competition-tax by windows-lost metric = 21%/22%/11% (5m/15m/1h) — earlier
  47%/12%/9% used the ask-vanish metric; both real, table now says which.
  5m sz>250 shows +3.7c on TAPE vs -2.4c on LEDGER — ledger law wins, cap
  250 stays; depth events will retest with true-feed data post-gate.

- 2026-08-02 ETH-5m GATE: PASSED (ledger-derived, per the conditional-verification
  protocol): EV +9.86c/sh on 55 distinct windows all-settled (criterion >=+5c),
  halves +$314/+$568 both positive, 11/12 trading days green, 0 mismatches,
  0 halts in window. Effective n=55 windows (fills=rows inflation confirmed:
  top window 17 rows at the 250-sh clip). ETH-5m is LIVE-ELIGIBLE #2.
  Sequencing: BTC-5m Tier 0 proves the live pipeline first; ETH follows onto
  the live box after a clean BTC Tier 0 (one new variable at a time).

- 2026-08-02 BOOK-FRESHNESS AUDIT (Telonex, self-audit of the 45s/15s widening):
  Archive truth at eval instants (T-6..T-1.5): book age >3s on only 8.5%
  (btc15 weekday), 7.8% (btc15 WEEKEND — no weekend effect), 9.1% (eth15),
  17% (btc1h, p90 8.9s). Live bots measured 33-59% stale — 3-6x the market
  truth. CORRECTED DIAGNOSIS: staleness is mostly RECEIVER-SIDE (six bots'
  ws clients on one 512MB box applying updates in bursts), not market quiet.
  WIDENING STANDS as safe+useful: across true market gaps >3s the best ask
  is IDENTICAL on resume 88.8-93.4% of the time; paper recheck + FAK miss
  semantics + official settlement bound any phantom risk; and receiver
  bursts resolve in seconds, far under the new tolerances. IMPLICATIONS:
  (1) live Amsterdam box (one bot, no contention) will see MORE than NYC
  paper — another conservative bias in the record; (2) NYC droplet resize
  is the structural fix if slow-family fills matter pre-live; (3) my
  'quiet market' framing was half-right, receiver load was the other half.

- 2026-07-31 1h HOURLY FAMILY discovered (gamma series API): btc/eth/sol/xrp
  hourly up-or-down series, dated ET slugs (bitcoin-up-or-down-july-31-2026-5pm-et).
  Volumes: BTC median $24.5k/window (9x the 5m windows, ZERO dead hours),
  ETH $5.9k. Tape (Jun 30-Jul 30, 696 windows/coin):
  BTC-1h: ALIVE — bot-as-is +9.47c/sh, 96.8% win, n=31 (1.1/day), halves
  +8.00/+10.71, ALL variants positive, competition tax ~9% (vs 47% on 5m).
  Paper bot shipped: polybot-snipe-btc1h.service (SLUG_STYLE=et_hourly; the
  slug generator is validated against all 744 real month slugs, 0 mismatches).
  Gate: 2 weeks paper from deploy (~Aug 14): ev>=+5c weighted, both-halves
  positive, no unexplained divergence from this tape.
  ETH-1h: leaning positive (+8.4c, n=8) but FAR too thin — parked; revisit
  alongside eth-15m after btc-15m/btc-1h paper gates.
  Also catalogued for later: hourly SOL/XRP, daily up/down on stocks
  (TSLA/AAPL/...), forex, metals, indices — none tape-tested yet.

- 2026-07-31 EVENING INCIDENT: 5m FILL DROUGHT (chronicle + verdicts).
  Fills stopped 10:39:58Z fleet-wide on 5m; 15m filled 7x same period.
  Diagnosis chain: timeline acquitted the day's code changes (drought began
  under morning code); live book probe filmed EMPTY winning-side asks in the
  final 10s; new nm= gate-reason telemetry measured no_ask=99/stale_book=75
  on 174/174 near-misses. VERDICT: seller absence on 5m closes (competition/
  maker withdrawal), NOT a bot defect. Watch 24-48h; if paper stays dry the
  question moves to Tier 0 (paper's 0.5s referee cannot see sub-300ms races
  a live bot could still win — the drought may be speed migration).
  BAR-AGE FIX AUDIT (user-requested, 2 models): spot_max_bar_age_s 2->5
  verdicts: Fable 5.0-CORRECT-WITH-SOL-OVERRIDE (strict-subset proof vs the
  profitable historical config; SOL unit -> 3.0, applied), Opus OVERSTATED-
  BUT-FIX-CORRECT. RECORD CORRECTION: my '~40% of BTC evals rejected' claim
  was UNSUPPORTED — the 178 no_datas were ~100% warmup polls, and the 43%
  nm figure was CLOB book staleness (different feed/knob); true gate cost
  ~0-4%. Fix stands on merits, justification retracted.
  Also fixed same night (both auditors converged): close_at now skips
  synthetic gap-fill bars — a stale price can no longer wear a fresh label
  (the completed F1 fix). Follow-ups queued: 1h no_outcome give-up should
  scale with window; nothing guards the 5-10s spot-silence band except the
  10s feed kill; paper recheck suppresses wrong-side fills live would take
  (watch live SOL separately at Tier 0).

- 2026-07-31 CONVERGENCE AUDIT (ROUND 3): 8 auditors — 2 models line-by-line
  over the SAME core (adversarial pair) + 6 per-unit deep audits with live
  probes and data refetches. RESULT: decision core CLEAN again (both models,
  independently); money conservation re-proven; 60/60 data spot-checks agree
  with gamma; live probes pass the firewall on every family. All confirmed
  findings fixed same-day (commit r3), unit-tested, smoke-booted:
  * LIVE (pre-money, the big two): 4xx order rejections no longer book
    phantom worst-case fills (only transport/5xx ambiguity does — H1);
    per-WINDOW live exposure now capped at per_trade_cap (retries could
    stack ~3x documented risk — O3). Plus: price-floor enforced in the live
    path, low-balance guard from cached reconciler balance, fee-scaled
    reconciler tolerance (warn first, halt on repeat), fsync'd pending-intent.
  * SPARSE-TAPE INSTRUMENT (SOL): bar staleness now priced (close_at returns
    bar age; >2s = no-data) — a 5-9s-stale SOL print was priced as fresh,
    overstating fv; barrier must exceed 3 spot ticks (below input resolution,
    99.5% fv is quantization noise); basis median age-filtered. GOOD NEWS:
    live SOL feed pair measured CLEAN (basis 0.999988) — the 1.03 'SOL
    blindness' was the research replica only, never the bot.
  * BREAKERS: trailing-30 now counts TAKES not price-level rows (on thin
    books 'trailing 30' had become ~5 windows); unmarked-fills halt counts
    windows and is time-limited (transient, healer-repaired); reconciler
    grace outlives toll fill-polling; settlement re-entry can't erase a
    recorded mismatch; MAX_DAILY_LOSS 650 on cap-500 units (one max loss
    ~$486 must not exceed the 0.8x trailing floor alone).
  * GUARDS: et_hourly+non-3600 window fails startup; live mode on a ledger
    with paper fills fails startup; toll requires authoritative oracle AND
    toll_enabled (incl. pre-positioning); ws resub race re-arms; spot clean-
    close reconnects pay backoff; order ids seeded from MAX(id)+1; preflight
    family/coin-aware; LIVE_STRATEGIES whitespace-safe; MemoryMax 300M all.
  * eth15 RECONFIGURED to default signal (audit demolished the fv=0.999 tape
    claim: carried by 4 lucky large fills, CI spans zero, deployed first-clip
    truncated exactly those winners, and the runbook's own law — never tune
    eth off the replica — was violated). Neutral true-feed evidence only.
  * ETH-5m GATE = CONDITIONAL: lifetime EV jumped from +8.3c (audited Jul 27)
    to ~+36c/sh implied on the final unaudited days, which also ran the old
    retry over-fill. Before declaring live-eligible run on the droplet:
    report 14 + fills-per-window histogram + per-window EV (commands in
    chat log). EV-weighted criterion is robust to the bias; the $ headline
    is not. 'Fills' = price-level rows, not windows (report now prints
    distinct windows).
  * btc15 day-one +$186 7/7 deep wins: 3-5 sigma vs tape base rates (deep
    opportunities ~1/day all month, never 7). Tape is most pessimistic in
    that exact band, so 'extraordinary, unverified'. VERIFY vs Telonex
    Aug 1 (T+1 archive; 46 resolved windows already listed).
  * REFUTED by triage (no change): 'paper sweeps unverified depth' (take
    reads the post-latency ladder — depth survival IS honest); 'paper fv is
    stale at fill' (live's in-flight order is equally stale at match; the
    simulation is faithful).
  * btc1h: full PASS incl. DST proof through 2027 (<=3 skipped windows/yr,
    zero wrong-market paths). btc5m: PASS (its H1/M2 findings were in the
    live path and shared code, fixed above).
  CONVERGENCE STATEMENT: three rounds in, the decision core (signal, gates,
  window/strike alignment, money math) has never had a confirmed finding.
  Round 3's finds were: live-path hardening (untested by definition until
  Tier 0), sparse-tape instrument honesty (SOL), breaker semantics at
  low cadence, and evidence-quality corrections (eth15/ETH-gate). The
  system is as verified as paper can make it; what remains unknowable
  (fill-fraction under contention, real fees) is exactly what Tier 0
  measures with $150.

- 2026-07-31 MASTER AUDIT ROUND 2 (three tracks: Opus 5 deep-code, Fable 5
  reality/deployment, empirical Telonex reconciliation). ALL confirmed
  findings fixed same-day, unit-tested, smoke-booted both families:
  * WRONG-MARKET FIREWALL (C1): discovery now asserts market endDate ==
    window close for EVERY family — any slug bug (incl. Polymarket's
    hours-since-midnight DST-night naming, proven on 2026-03-08 data)
    becomes a skipped window, never a silent wrong trade. DST cost: <=4
    skipped windows/yr (fall-back ambiguity guard + spring-forward firewall).
  * TOKEN-STATE WIPE (C2): re-discovery can no longer reset a live book
    (setdefault + duplicate-condition guard + REST resync on registration).
  * PAPER OVER-FILL (C3): PaperExecutor tracks consumed liquidity per
    (window,token,price) — retries can never re-buy the same displayed
    shares the ws book restored. THIS WAS PAPER-OPTIMISTIC; expect slightly
    lower paper retry fills going forward (that's honesty, not decay).
  * LIVE POST CRASH-SAFETY (C5): durable live_pending event before every
    POST; ambiguous transport failures book the worst-case provisional fill
    + sticky halt (previously: possible real position with zero ledger rows).
  * 1h ORACLE RETENTION (M3): sample retention now scales with window size;
    the 1h cross-check was silently never running (reports "cross-check
    missing on N" now surfaces this in bot.report).
  * Paper budget sized at sweep limit (M2), unified entry/recheck gate incl
    price floor + giant-ask refusal (M4), exchange 5-share minimum in paper
    (M5), healer can't clobber a recorded mismatch (M7), task supervision so
    bookkeeping crashes restart the task not the process (M8), StartLimit in
    [Unit] + MemoryMax 300M (M9/M10), fee-metadata drift check at startup
    (M11 — was promised in a comment, never implemented; now real, passes
    1000/1000), ledger indexes, preflight uses slug_for/window_secs.
  * KNOWN REMAINING PAPER OPTIMISM (documented, not yet calibratable): paper
    wins contested races WHOLE — when an ask survives the 0.5s gate, paper
    takes all of it; live's FAK gets whatever faster rivals left (C4). The
    Tier-0 live gate MUST measure matched_shares/requested_shares as a
    first-class metric (live_forensics already records it). Until calibrated,
    treat paper share counts as an upper bound; EV/share is unaffected.
  * GATE DATA RELABEL (M1): btc15/eth15/btc1h results before this date ran
    an effective ~330-share cap ($300 window budget bound before the 500
    clip); budgets now scaled (SNIPE_WINDOW_MAX_COST=500, MAX_DAILY_LOSS=600).
  * POST-LIVE CONTAMINATION PROTOCOL (F4): once live trades, paper bots on
    the same family will FAIL their recheck on exactly the asks live wins —
    paper fill-rate drops through no fault of the edge. Compare paper-vs-live
    at the ATTEMPT level (join depth events by window+ts), classify paper
    recheck-fails within ~1s of an own live fill as self-consumed, and
    pre-expect the paper fill-rate drop. Never read that drop as edge decay.
  * eth15 EXPERIMENT AMENDMENT (F7): deployed same-day at owner's request
    (paper is free); supersedes the same-day "no bot now" line above. It IS
    the single-survivor-of-a-scan config — treat as exploratory only.
  * 1h GATE INTERPRETATION (F1): bot signal runs in the Chainlink frame while
    the market resolves on the Binance candle — the bot is the PROXY on this
    family (tape held the true frame). Non-authoritative oracle logs
    disagreements; expect bot-vs-tape gap from frame noise. On a non-US live
    host, consider SPOT_FEED=binance for the 1h family.

- 2026-07-31 TAPE-vs-BOT EMPIRICAL RECONCILIATION (audit track 3): replayed
  research tapes on days where live paper bots have actuals, independent data
  paths (tape: Binance-proxy signal + Telonex archive; bot: Coinbase+oracle
  live feeds). BTC-5m 8 clean days: tape +$1,014 vs bot +$2,039, daily corr
  0.51, mostly same-sign. ETH-5m week: tape -$177 vs bot +$282. CONCLUSION:
  tapes are systematically PESSIMISTIC vs real-feed bots (proxy-signal noise
  is asymmetrically punished: wrong entry ~-90c vs right entry ~+8c), and
  day-level tape numbers are noisy (corr ~0.5). Policy confirmed: tapes SCOUT
  (their positives are likely conservative floors), paper bots DECIDE, gates
  stay mandatory. btc15's +$186 first day: Telonex archive is T+1, direct
  quote-level verification queued for tomorrow (raw_15m 2026-07-31 fetch 404
  today; windows resolved list already pulled, 44 windows).

- 2026-07-31 DEPTH RECORDER shipped (all snipe bots): every take attempt now
  logs a 'depth' event — top-5 ask ladder at signal time AND after the 0.5s
  latency gate, plus filled qty. Passive (zero behavior change; report
  filter already excludes it). Purpose: measure layer-2/3 profitability
  offline (join settlements for outcomes) = the deeper-book counterfactual,
  including the key case where we LOSE the front-layer race but layers 2-3
  survive. Analyze after ~1 week of events. If layers vote profitable, ship
  a separate experimental walking bot (own unit + ledger, A/B) — do NOT
  fold walking into the qualified bots.

- 2026-07-31 BTC-1h OPPORTUNITY-EXPANSION GRID (wide tape: entries to T-120s,
  fv down to 0.95): more COUNT exists, not more DOLLARS. T-6s strict: 1.1-1.3/d
  at +8.6..+9.0c = ~$300/mo. Loosest consistent tier (T-12s): 1.8/d at +5.4c =
  same ~$293/mo. Everything earlier (T-30/60/120) is second-half NEGATIVE or
  ~zero (adverse selection eats it). Loosening fv adds ~nothing at T-6 (fv is
  extreme by then anyway). CONCLUSION: the pot is ~fixed at current 250-sh cap;
  strict settings capture it in fewer, better trades. Bot stays as-is. The
  real scaling lever on 1h is SIZE (window vol $24.5k) at the live ladder, not
  filters. (Curiosity, n=8: px 0.80-0.90 from T-60 = +14.1c both halves.)
  SOL-1h: DEAD (n=4, -22.7c, half2 collapse; same blind-instrument pattern).
  XRP-1h: NO TRIGGERS (n=1 in 9 taped days despite $10.4k/window volume) —
  our signal rarely fires there; park, no bot.

- 2026-07-31 15m FAMILY, ETH + SOL (same month, same tape method):
  ETH-15m: MARGINAL-PROMISING, parked. Bot-as-is +2.95c/71% on n=62
  (2.1/day) but halves +7.68/-0.21 = not consistent. The one both-halves
  survivor: fv_min 0.999 -> +12.4c (+13.0/+11.9, n=37, +$216/29d). Deep band
  +18.5c both halves; mid band toxic (-33c). Volume real (~$3.6k/window).
  DECISION: no bot now — small n, and the 5m lesson (ETH tape candidates
  refuted by the true-feed ledger) demands humility. Revisit once BTC-15m's
  paper gate proves the family transplant; if so, trial eth-15m with
  SNIPE_FV_MIN=0.999.
  SOL-15m: NO. Instrument blind for sol (feed ratio 1.03), halves flip sign
  everywhere, volume dust (~$745/window). Do not revisit without new reason.

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

- 2026-08-02 KALSHI 15M VERDICT (user-supplied archive, 2026-05-02..07-30):
  full intake of Kalshi KXBTC15M/KXETH15M — 17,004 markets (all finalized),
  117M trades reduced to a final-180s tape, books (July-complete per source),
  1m candles, settlement index (== official expiration_value EXACTLY on all
  8,499 BTC joins — archive internally consistent). Resolution is 60s-TWAP vs
  60s-TWAP (rules_primary), so outcome knowability at T-t is computable: flip
  margin m bps needs a ~m*60/t bps spot move in t sec. Cheap-winner scout on
  taker-bought-winner prints (net 7% Kalshi fee): raw pool looks huge ($50k+/d
  BTC <=0.90 last-30s) but 90% sits in <1bp final-margin knife windows =
  hindsight, not edge. Requiring knowability at entry (2s/1bp, 5s/2bp, 10s/3bp,
  30s/10bp ladder): DECIDED pool = $18/day BTC, $0/day ETH — the entire venue,
  before competition — vs $100-150/day our Polymarket BTC-5m bot actually
  nets. Books cross-check (closes the un-taken-ask hole): after dropping
  sentinel states (best_ask=0 = empty side — verified EMPTY list in raw;
  bid=1.0 stubs), decided-window min winner ask med 0.997-0.999 across all 6
  coin-months; <=0.97 sightings ~1/day both coins combined, nearly all
  single-snapshot flickers or asks resting on DEAD windows (1-7 prints in
  final 35s); the one active case (JUL22 0115, winner traded 0.93-0.98) is the
  already-counted $3/day 10-30bp bucket. READ: Kalshi's CFTC venue with pro
  MMs ($3.7B volume in these series) prices the TWAP endgame perfectly; our
  edge is Polymarket-specific book laziness and does not travel. VERDICT:
  NO Kalshi bot, NO recorder — do not revisit without evidence the MM regime
  changed. Data archived in user's B2 (kalshi-nico-0826); reduced tapes in
  session scratchpad (reduced/, reduced_books2/, tw_cache.parquet).

- 2026-08-02 KALSHI ADDENDUM — THE EDGE MATCH (user pushback: "kalshi doesn't
  work exactly like polymarket, search the differences"): user was right.
  Re-checks first: fee semantics verified (taker ceil(0.07*P*(1-P)), maker 25%
  of that; crypto series may carry higher multipliers — worse for taking,
  immaterial here); trade side semantics verified against API docs (my
  taker_won read was correct); semantics-FREE both-sides bound on the decided
  pool = $52/day BTC — the no-trading verdict stands under any field
  interpretation. Maker-side measured: $532/day BTC decided-endgame maker
  harvest at median 0.999 acquisition = queue-priority MM business vs pro
  firms (Kalshi runs a live Liquidity Incentive Program through Sep 2026);
  not our lane. THE FINDING — cross-venue signal: joined all matched
  quarter-hour windows Jun30-Jul29 (Kalshi KX{BTC,ETH}15M trades tape vs our
  Polymarket 15m Telonex quote archive; 2,736 BTC + 2,709 ETH windows). When
  Polymarket offers a side at 0.90-0.97 near the close, Kalshi's concurrent
  same-side price separates gold from poison: T-30s BTC — Kalshi>=0.985:
  n=196, 100% win, +5.3c/sh; Kalshi<0.95: n=38, 57.9% win, -35.7c/sh. T-10s
  dissent: 12.9% win, -80.7c/sh (ETH replicates: 100%/+5.6 vs 47.6%/-45.8 at
  T-30). Kalshi's pro-priced book is an ORACLE for our venue's 15m windows.
  Caveats: outcome labels are Kalshi's (BRTI TWAP) not Chainlink — dissent
  buckets are 26-78% knife windows where labels may flip, but a <1bp-margin
  window is ~coinflip under either source vs 0.90-0.97 cost, so the veto
  survives label noise; tape-level top-of-book, signal question not fill
  question; overlap with our own spot-fv gate unmeasured (bot fills start
  Jul 31, Kalshi data ends Jul 30 — no direct backtest possible). Live
  feasibility verified: api.elections.kalshi.com/trade-api/v2 serves market
  list + FULL orderbook UNAUTHENTICATED, 0.2-0.5s, no account needed.
  PLAN (staged, fidelity law): (1) add Kalshi feed to the two 15m paper bots
  as TELEMETRY ONLY — log concurrent Kalshi same-side price on every take
  attempt + depth event; zero behavior change, gate undisturbed; (2) after
  1-2 weeks measure on OUR fills: losing fills with Kalshi dissent at entry
  vs winners; (3) enable dissent-veto (and evaluate confirm-relaxation for
  more fills) only on that evidence. Kalshi is a signal source, not a venue:
  no account, no trading, no cost.

- 2026-08-04 CROSS-VENUE STRATEGY HUNT (7 backtests, May02-Jul13 matched tick
  data, adversarial verification where session limits allowed): SURVIVORS:
  (1) S5 intra-PM shared-close structure (5m+15m windows ending together;
  buy Up-longer + Down-shorter when combined ask cost <1 -> payoff floor $1,
  verified 0 violations/9,729 pairs): conservative tier $20-27/day EVERY
  month, slippage-robust; strict tier adds $14-40/day but 75% concentrated
  in 8 dislocation days, cancel-latency unproven -> build as paper scanner,
  log-only the deep tier. (2) Kalshi K<0.95 veto on 15m cheap takes:
  +$21/day avoided losses, execution-proof, CONFIRMED by both verifiers ->
  task #18 continues exactly as planned. (3) S1 deep-shelf (15m ask
  0.90-0.99 + Kalshi>=0.98): +$36/day, execution-robust but in-sample-picked
  -> out-of-sample on Jul14+ paper first. GRAVEYARD: S1-as-specified +$4/day
  after execution (statistically zero); my earlier "196/196 +5.3c" was a
  LABEL ARTIFACT (Kalshi results used as PM labels; true labels -> +0.19c)
  — retracted; S2 strike-bridge dead ("Kalshi-certain" != "Poly-certain":
  TWAP basis + stale strike cap transfer at ~0.97-0.98); S3 quarter-drift
  null (10x below bar); S4 dutch decaying/negative-in-July + 36% knife
  split risk; S6 open-jump scalp -5.7c/sh all months (unfilled tail wins
  0.6%, needs 81.5% side accuracy, best signal 60.5%; Kalshi does NOT lead
  spot; chainlink-staleness mechanism nonexistent at 1s tick). LAWS LEARNED:
  pin decoupling is the universal loss engine (74/81 S1 losers; 35.8% of
  knife windows settle differently across venues — same family as the SOL
  mismatch); displayed size is adversely informed everywhere; edges live in
  dislocation bursts (fat tails, not steady income); 0.07*p(1-p) fee kills
  mid-price strategies; July showed venue-wide competitive erosion — paper
  bots stay the referee. Full analyses: session scratchpad research_out/
  (FINAL.md + 7 per-strategy mds + verifier reports + trade-level parquets).

- 2026-08-07 BIG-LOSS ANATOMY (user q: same hour / same window / cumulative?
  Per-window ledgers: BTC 284 windows +$2311, ETH 58 windows +$664, Jul15-Aug7).
  VERDICT — SINGLE-WINDOW EVENTS, not decay: top-10 losing windows carry 55%
  (BTC) / 98% (ETH) of all loss dollars; every catastrophe is a max-size
  (250sh) window flipping. NO loss momentum: P(loss | prev window lost) 16.0%
  vs 18.1% after a win (base 17.6%); the 11 windows traded within 60min after
  a big loss made +$72.75. "Streaks" exist (08-02 07:20-07:55 -$225/35min)
  but are chance clustering, not contagion. Hour-of-day: damage sits in the
  01/06/07/12 UTC quiet-chop zone, but the hour-block backtest gain is a
  MIRAGE — ETH's entire walk-forward gain was the 08-07 resolution-anomaly
  window sitting in hour 01 (ex-anomaly: exactly $0.00); BTC ex-anomaly WF
  +$118 over 10 windows (one lucky skip); blocking hours 0-7 broadly LOSES
  $750 (BTC) / $436 (ETH) — nights also feed the champions. RULE TOURNAMENT
  (cost caps, cooldowns, daily stops, hour blocks, px-band caps, combos —
  scored on crash-day savings vs champion-day cost + Jul28+ walk-forward):
  daily stop $150-200 is HARMFUL (-$583 BTC: 08-01 dipped below -$200
  intraday then finished +$443 — intraday drawdown is the strategy working);
  cooldowns are noise (sign flips 30m vs 60m pause). ADVERSARIAL VERIFY
  (clean-room reimpl matched every number; Fisher tests on momentum all
  p>=0.54): 3 of 4 conclusions confirmed; my flat-cost-cap claim REFUTED —
  $150 flat cap costs 9.7-14.5% of ex-anomaly pnl (not <2%), $200 flat cap
  is cheap (-2.1%) but bought almost nothing (saves $23 of the -$421
  anomaly, binds ZERO ETH losers). What verified instead: (a) BTC's
  px>=0.95 bucket is structurally thin — 104 windows net -$61.50
  ex-anomaly (breakeven hit rate ~97%, observed 95.2%); a CONDITIONAL cap
  (cost<=$150 only when avgpx>=0.95, BTC only) is net +$30.91 in-sample
  while cutting both worst ex-anomaly windows (-$240.67, -$234.95) to
  ~-$150 — two-event evidence, adopt at live, keep paper unchanged;
  (b) ETH must NOT be capped: its engine is multi-fill cheap windows (67%
  of pnl; flat caps bind 19 ETH winners, 0 losers) and its px>=0.95 bucket
  has zero losses; (c) the UNPRICED exposure is cross-book same-window
  concurrency — the two largest combined hits (08-07 -$421, 07-19 01:25
  -$203) were simultaneous near-max positions on BTC+ETH in ONE window; no
  tested rule bounds it; a combined per-window ceiling (~$300 across books)
  is the missing lever and needs a cross-bot mechanism at live. 08-07
  "ANOMALY" — RETRACTED, see the 2026-08-08 RESOLUTION-RULE CHANGE entry:
  I recorded w1786066800 (01:40, -$421 across btc+eth+sol) as a venue
  resolution error because Binance 1s spot showed UP while it settled DOWN.
  It was not an error — Polymarket changed the resolution rule effective
  08-07 00:00 UTC (spot close -> 30s rolling TWAP), and under the new rule
  that window WAS Down (-1.28bp). The bots were wrong, not the venue; every
  "disputed window" in that list has the same cause. The mismatch halts
  still earned their keep: they caught a silent venue regime change within
  two hours. Treat the Aug-7 windows as a BROKEN-SIGNAL era, not a tail
  event — and do not use them to price resolution risk.
  L2 signature: catastrophic flips entered at 0.83-0.97 with 2.3k-42k-share
  0.98/0.99 ask walls BEHIND the entry (informed sellers) — but depth was
  only pulled for loss windows (selection bias), so the wall-behind veto is
  NOT yet scored. bot/loss_audit.py closes that: joins ALL depth events
  (winners too) to window outcomes, prints wall-bucket win rates + veto
  counterfactual grid. DECISIONS (per "no changes for changes' sake"): paper
  bots UNCHANGED (comparability; no rule beat baseline robustly ex-anomaly);
  cost-cap $200/window adopted as a LIVE-deployment requirement, not a paper
  change; wall-veto decision deferred to loss_audit evidence.

- 2026-08-07 loss-anatomy ADDENDUM (completeness critic): (1) REGIME DRIFT
  is the top caveat — all rule economics are July-dominated (edge $8-22/
  window, ~28 windows/day) while August ex-anomaly runs ~$1.19/window at
  8.5/day; the $200-cap delta flips SIGN by regime (Jul -$108, Aug +$37
  ex-anomaly), so re-score the tournament on August data as it accrues.
  (2) The $200-flat-cap verdict is statistically UNDECIDABLE on this sample
  (bootstrap CI [-$163,+$43], P(benefit)=0.10) — only the $150 flat cap is
  settled (harmful). (3) Proportional pnl-scaling is provably wrong for
  mixed-side multi-fill windows (2/9 depth-logged BTC loss windows filled
  BOTH sides) — a real cap truncates the fill sequence; needs per-fill
  replay before any cap ships. (4) Depth evidence is doubly censored:
  only 9/50 BTC + 5/12 ETH loss windows have depth, all post-Jul-31 —
  loss_audit must run before any wall-veto decision. (5) Treat oracle
  disputes as a RECURRING cost, not an excludable anomaly: the two worst
  fleet events were correlated cross-book single windows; the fleet-level
  combined per-window ceiling (~$200-250) is the most promising UNTESTED
  rule and addresses both tails. (6) Paper-fill adverse selection at
  px>=0.90 (fills in front of 2k-42k walls) can shrink live winners while
  losses persist — price this before live sizing. Trading-gap censoring
  (BTC 64h, ETH 112h) is explained: breaker/mismatch halts, not missing data.

- 2026-08-08 RESOLUTION-RULE CHANGE (root cause of every "Aug 7 dispute";
  THE most important entry in this file — it invalidates the target variable
  every direction strategy was built on). WHAT CHANGED: Polymarket switched
  the Chainlink-resolved crypto up-down series from the SPOT data stream to
  ROLLING TWAP streams. Old rule (market description, verbatim): "resolve Up
  if the Bitcoin price at the END of the range is >= the price at the
  BEGINNING", source data.chain.link/streams/btc-usd. New rule: "resolve Up
  if the time-weighted average price (TWAP) ... is >= the price at the
  beginning of that range", source .../btc-usd-twap-30s-streams. TWAP length
  scales with window: 30s for the 5m families, 60s for the 15m families
  (eth/sol identical wording). WHEN: market metadata shows the last
  spot-sourced market was created 08-06 00:02 UTC and the first TWAP-sourced
  one 08-06 01:48 UTC; because these markets are minted ~24h ahead, the
  change took effect for all windows from 08-07 00:00 UTC. Our first
  mismatch: 08-07 01:40. NOT AFFECTED: the 1h series
  (bitcoin-up-or-down-*) still resolves on Binance BTC_USDT via UMA —
  verified unchanged 08-07/08-08. WHICH QUANTITY RESOLVES (measured, not
  assumed — five candidate rules scored against official outcomes on 287
  Aug-7 BTC windows using exact Binance 1s data): 30s-TWAP@close vs
  30s-TWAP@open 94.4%; 30s-TWAP@close vs spot open 90.6%; old spot rule
  90.2%; full-5min mean vs open 80.8% (dead — "TWAP of the range" does NOT
  mean averaging the whole window). Difference-in-differences confirms the
  cutover on two assets: BTC old rule 95.8%/97.9% on 08-05/08-06 vs 90.2% on
  08-07, while the TWAP rule goes 87.4%/89.2% -> 94.4%; head-to-head on
  windows where the two rules disagree, spot won 27-3 and 26-1 before the
  change and LOST 5-17 after. ETH replicates independently (spot 95.8% ->
  87.5%, TWAP 90.6% -> 96.5%, head-to-head 21-6 -> 2-28). Residual ~5% is
  Binance-vs-Chainlink basis on sub-2bp windows, not rule ambiguity.
  CONSEQUENCES: (1) bot/feeds/oracle.py reads the SPOT stream, so both the
  strike and the settle comparison are now the wrong quantity — the snipe
  fv (P(spot close >= spot open)) targets a variable that no longer decides
  anything. All 5m/15m direction bots must stay STOPPED until the oracle
  consumes the TWAP stream. (2) XWIN's payoff floor is VOID: its leg choice
  compares spot strikes, and spot-vs-TWAP strike ordering flips sign on 6.3%
  of shared closes (6/95 on Aug 7) — a flipped package has a $0 hole where
  the $2 band used to be, and 2 of those 6 lost BOTH legs (~-$237 each). Its
  +$57 on 08-08 is variance from the new payoff shape, not evidence of
  health. Even with correct strikes, the 5m market settles on a 30s TWAP
  while the 15m settles on a 60s TWAP, so the two legs no longer share one
  close price: require strike gap > the expected 30s/60s spread (|spread| >=
  |gap| on 3.2% of Aug-7 closes, 0 actual violations) before entering.
  (3) Every historical backtest in REPORT.md and the strategy-hunt files
  measured the OLD target — treat their edges as unvalidated until re-run
  against TWAP labels. THE SILVER LINING (untested, promising): a 30s TWAP
  at close is ~83% locked 5 seconds before the close, so the new target is
  MORE predictable at snipe time than a spot print was. The edge may be
  better once implemented correctly — but competitors get the same gift, so
  measure before believing. NEXT STEPS: (a) bot/twap_probe.py discovers the
  topic/symbol carrying TWAP values on wss://ws-live-data.polymarket.com
  (must run on the droplet — the research box cannot open websockets);
  (b) rewrite the oracle to serve TWAP-at-instant with the window-length-
  appropriate averaging, keep the spot stream only as telemetry; (c) re-derive
  the snipe fv against the TWAP target and re-run the honesty gate;
  (d) re-verify XWIN's floor with TWAP strikes + a minimum-gap rule.
  MONITORING LESSON: a silent venue rule change looked exactly like a
  resolution anomaly for a full day. Add a weekly check that the live market
  description/resolutionSource still matches what the oracle implements —
  gamma-api /markets?slug=...&closed=true returns both fields.

- 2026-08-08 TWAP FEED PROBE RESULT + migration gate. ws-live-data.polymarket.com
  does NOT publish the TWAP streams: an unfiltered subscribe enumerated every
  symbol on every live topic and found only crypto_prices_chainlink (1s
  Chainlink grid: btc/usd, eth/usd, sol/usd, bnb, doge, xrp, hype, zec) and
  crypto_prices (exchange spot: btcusdt, ...). No crypto_prices_*twap* topic
  answers at all. CONSEQUENCE: the resolver's 30s/60s TWAP must be
  RECONSTRUCTED from the 1s Chainlink grid the oracle already consumes —
  almost certainly how Polymarket computes it too, since that is the same
  series. THE GATE (do not skip): bot/twap_record.py captures the 1s grid to
  bot/data/twapcal/<coin>_1s.db; bot/twap_verify.py rebuilds each candidate
  rule (TWAP/TWAP, TWAP/spot, OLD spot/spot; boundary conventions [t-N,t) and
  (t-N,t]) and scores them against OFFICIAL gamma outcomes for every fully
  covered window. Because the recording is the resolver's own price series, a
  correct reconstruction must score ~100% — require >=99% before touching
  bot/feeds/oracle.py, and keep all 5m/15m direction bots stopped until it
  passes. Mechanics validated 08-08 by replaying the Aug-7 Binance 1s series
  through the verifier: it correctly ranked TWAP/TWAP first (94.4%) and the
  old spot rule last (89.9%), with all 16 disagreements under 1.5bp — that
  residue IS the Binance-vs-Chainlink basis and is exactly what should vanish
  on the real grid. If it does not vanish, the boundary convention or the
  weighting is wrong and must be re-derived BEFORE any oracle change.

- 2026-08-09 TWAP MIGRATION SHIPPED (the fix for the 08-07 rule change).
  VERIFIED FIRST, then coded: bot/twap_verify.py scored candidate rules
  against official outcomes on a 13h recording of the live 1s Chainlink grid
  — BTC 5m 56/56 = 100%, BTC 15m 20/20 = 100%, ETH 5m 62/63 = 98.4% (the one
  miss at a 0.056bp margin, i.e. inside our own reconstruction error), old
  spot rule 89.6/93.6/93.2%. Gate was 99%: BTC PASS both families. The two
  boundary conventions ([t-N,t) and (t-N,t]) BOTH score 100% and cannot be
  distinguished — they differ only when one boundary sample moves the mean
  across the strike, so the code computes both and refuses to call the
  window when they disagree. WHAT SHIPPED: (1) Oracle.twap_at / twap_winner
  / twap_known reconstruct the rolling TWAP from the 1s grid we already
  consume (no TWAP topic exists on the live-data socket — probe swept 15
  topic names, only crypto_prices_chainlink and crypto_prices answer).
  (2) config oracle_twap_s auto-selects 30s (5m) / 60s (15m) / 0 (1h family,
  which still settles on Binance via UMA and MUST stay on the old path),
  plus oracle_twap_min_coverage 0.9 — a TWAP over a gappy grid is a
  different number, so refuse rather than impute silently. (3) The
  reconciler cross-check now judges the venue's actual rule, so the mismatch
  tripwire means something again. (4) snipe strike = TWAP at open, and a new
  fv model: the closing average is PARTLY HISTORY at decision time, so only
  the seconds after our last oracle sample are random —
  TWAP_close ~ N( (known + u*S)/n , (S*vol/n)*sqrt(u^2*a + u(u+1)(2u+1)/6) ).
  At u=5, n=30 that sd is ~9x smaller than the old vol*sqrt(tau); unit-tested
  in scripts/test_twap_math.py (22 assertions, closed-form cross-check).
  WHAT THIS MEANS ECONOMICALLY — UNMEASURED, WATCH IT: a confident call now
  needs ~0.5bp of edge instead of ~10bp, so fv will clear 0.995 far more
  often and the bot will fire much more, at higher ask prices — straight
  into the px>=0.95 bucket the loss audit showed is structurally thin
  (breakeven ~97%, observed 95.2%). Two guards ship with it:
  snipe_min_gap_bps (0.1bp, below our reconstruction error) and the existing
  tick floor, damped by u/n. Neither is tuned — paper is the referee, and
  the first day's fill count is the number to look at. STICKY HALTS: the
  08-07/08-08 mismatches re-trip on every restart by design;
  scripts/clear_rule_change_mismatches.py acknowledges ONLY windows at or
  after the 1786060800 cutover, refuses pre-cutover rows (those are real
  bugs), keeps winner/oracle_winner intact and writes a mismatch_cleared
  audit event. RESTART ORDER (matters — clearing halts while old code runs
  would resume trading on the broken signal): stop services -> git pull ->
  run the unit tests -> dry-run the clear -> apply -> start btc5m ALONE.
  XWIN STAYS DOWN and is NOT repaired by this: its floor needs both legs to
  settle on one closing price, but the 5m leg now settles on a 30s TWAP and
  the 15m leg on a 60s TWAP, so the shared-close premise itself is gone. Its
  strikes are also still spot-based (ordering flips on 6.3% of closes). A
  repair means re-deriving the floor with a minimum strike-gap that exceeds
  the plausible 30s/60s spread (|spread| >= |gap| on 3.2% of Aug-7 closes) —
  a design, not a proven edge. Do not restart it on a hunch.

- 2026-08-09 SIMPLIFIED THE ADAPTATION (owner call: "keep the og strategy,
  just adapt it to the change"). He was right and I had overbuilt it. The
  bespoke TWAP confidence model is REMOVED. What the venue actually changed
  for us is TWO things, and only two: the STRIKE (a rolling TWAP at the open,
  not the open print) and the SETTLED QUANTITY (an average, not the closing
  tick). Both are facts we must read correctly — the strike now comes from
  Oracle.twap_at and the reconciler cross-check from twap_winner. Everything
  else — vol*sqrt(tau), fv>=0.995, the 0.97 cap, clip, budgets — is
  UNTOUCHED, so the net strategy diff versus the pre-change code is the
  strike lookup plus a coverage guard. WHY NOT price the average exactly:
  the honest model puts the sd ~10x tighter, which sounds like an upgrade
  and is actually the opposite — measured live on 08-09, fv pinned at
  1.0000 on 6054 of 6087 evaluations. A gate that passes 99.5% of ticks is
  not a gate; it deletes the selection that IS the strategy. The old
  vol*sqrt(tau) is now deliberately CONSERVATIVE (it prices the closing
  tick's uncertainty against a target that is smoother than a tick), and
  that conservatism is exactly what preserves the ~6bp distance filter the
  validated edge was built on. Trade frequency should return to roughly the
  historical ~13 fills/day; if it does not, the cause is the book, not the
  signal. Derivation of the exact model is kept in the 08-09 migration entry
  above should we ever want it, but it does not ship. LESSON: when a venue
  changes a definition, change what READS the definition; re-deriving the
  model on top of it is a second, unvalidated change riding on the first.

- 2026-08-09 FIRST FILL AFTER THE MIGRATION LOST EVERYTHING — and found a
  real bug in my own adaptation. One window, 22 fill rows, 250 shares at avg
  0.33, win rate 0%, -$85.89. Our fv said >=0.995 on a side the book priced
  at 0.33. The book was right. CAUSE (not bad luck): I fixed the strike to a
  TWAP but left the ESTIMATE as raw spot — comparing a spot price against an
  average. Since the window settles on the closing TWAP and most of that
  average is already history at decision time, a late spot move only moves
  what settles by u/n of itself: at 6s left in a 30s average, a +30bp spot
  jump is a +6bp move in the settled quantity. Using spot overstates the gap
  5x, and it does so EXACTLY in the setup the strategy trades (a fresh move
  the book has not repriced). My earlier claim that keeping vol*sqrt(tau)
  made the model "conservative" was only half right: the UNCERTAINTY was
  conservative, the CENTRE was biased, and the centre is what picks the side.
  FIX: est = (known + u*spot)/n, compared against the TWAP strike; the
  uncertainty term stays the original vol*sqrt(tau). Still no re-derivation —
  the change is that we now estimate the thing that settles. Regression test
  in scripts/test_twap_math.py section [5] pins the exact failure shape.
  EXPECT FEWER TRADES: to move a 30s average 6bp with 6s left the spot must
  move ~30bp, which is rare — so at T-6s this strategy is now nearly dead by
  construction, independent of liquidity. That is the honest read of the
  arithmetic, and it reframes the fill drought: the book being empty at T-6s
  (no_ask 10/22, REFUSED-BUT-PRICED 0/22 over 24h) is the market agreeing
  with that arithmetic, not a separate problem. WHERE THE EDGE MAY HAVE GONE:
  earlier. At lead L the spot carries L/n of the estimate, so before the
  averaging window opens (L >= n) the projected average IS the spot and the
  old geometry returns — at the cost of more unwritten window. bot/timing_scan.py
  scans the recorded grid against official outcomes across lead times to find
  where accuracy and gap size coexist. Signal-only: it says nothing about
  whether an ask was resting, which needs a separate book recorder.

- 2026-08-09 THE EDGE MOVED EARLIER IN THE WINDOW (bot/timing_scan.py, 26h
  grid, 298 BTC 5m windows vs official outcomes, no lookahead — each lead
  uses only samples at or before its own decision instant). Direction
  accuracy of the projected closing average, restricted to |gap| > 2bp:
  100% at leads 3-30s (67 windows at L=30), 98.4% at 45s and 60s (61-63
  windows), 94.9% at 90s, 94.1% at 120s. Unrestricted accuracy decays as
  expected (99.6% at L=6 -> 69% at L=120), so the GAP FILTER is doing the
  work, not the lead time. ~23% of windows clear 2bp at any given lead, i.e.
  ~55-60 signals/day. THE CAVEAT THAT MATTERS: realized BTC 1s vol over the
  measured period was 0.359bp/sqrt(s) (1.05%/day — a calm regime). A
  driftless random walk at that vol predicts only 81.1% accuracy for a 2bp
  gap at L=60 and 71.2% at L=120; we observed 98.4% and 94.1%. Observed
  beats theory by a wide margin, which most likely means vol clustering (the
  median window is far quieter than the unconditional vol implies) rather
  than a new law — so do NOT hardcode a 2bp threshold. The fv machinery
  already divides by vol*sqrt(tau) and adapts by construction; that is the
  right harvester. Expect this edge to shrink hard in a 3%/day regime.
  WHAT IS STILL UNKNOWN — and it is the whole question: whether anyone is
  OFFERING the winning side at those leads and at what price. The T-6s book
  is empty (no_ask 10/22, REFUSED-BUT-PRICED 0/22 over 24h) precisely
  because the answer is locked by then; at T-30..T-120 the outcome is still
  genuinely uncertain to the market, so offers should exist — but "should"
  is not evidence. bot/book_record.py samples both tokens' books at leads
  120/90/60/45/30/20/10/6/3s via the CLOB REST endpoint into
  bot/data/bookcal. Only once that is joined to the grid and outcomes can
  the opportunity be priced. DO NOT move snipe_eval_from_s on the strength
  of accuracy alone: a 98% call bought at 0.99 is a losing trade.

- 2026-08-09 THE EARLIER-LEAD EDGE LOOKS PRICED (first three-way join,
  bot/edge_report.py, 20 windows — a shape, not a verdict). The book DOES
  exist earlier: winning-side quote availability runs 89% at L=120s and 95%
  at L=90s, decaying to 13% at L=3s while the LOSING side stays quoted
  95-100% throughout. That decay curve is the fill drought, measured
  directly, and it confirms the T-6s emptiness was the market withdrawing
  from a decided outcome rather than anything wrong with us. BUT the prices
  are already there: the side our signal picks is offered at 0.967 (L=120),
  0.975 (L=90), 0.982 (L=60), 0.985 (L=45), 0.990 (L=30). Cross-referencing
  those asks against the accuracy measured on the LARGER timing_scan sample
  (~250 windows, |gap|>2bp) gives break-even vs measured:
    L=120  need 96.9%  have 94.1% (n=51)  -> -2.82c/sh
    L= 90  need 97.7%  have 94.9% (n=59)  -> -2.77c/sh
    L= 60  need 98.3%  have 98.4% (n=63)  -> +0.08c/sh  (i.e. zero)
    L= 45  need 98.6%  have 98.4% (n=61)  -> -0.20c/sh
    L= 30  need 99.1%  have 100%  (n=67)  -> +0.93c/sh
  At the 95% lower confidence bound EVERY lead is negative (-3.0c to -9.3c);
  the L=30 line uses a 100% run of 67, whose honest floor by the rule of
  three is 95.5%, which prices at -3.55c. The market is quoting our forecast
  back to us, slightly better than we can forecast it. DO NOT read the
  edge_report hit% column as accuracy — 7 clean trades is not 100%; the
  report now prints a need% column beside it so the comparison is explicit.
  STATUS: leaning "priced, no edge", NOT concluded. Cheap checks left before
  calling it: (a) a bigger gap threshold (GAP_BPS=4/6 — if accuracy rises
  faster than the ask does, an edge could survive in the tail), (b) days
  rather than hours, (c) a livelier vol regime, (d) eth/sol. Nothing about
  this changes the oracle migration, which remains correct and verified.

- 2026-08-09 STRICT-THRESHOLD IDEA KILLED OUT OF SAMPLE; MAKER ROUTE IS THE
  LIVE QUESTION. Owner pushed back on the premature "no edge" call, rightly:
  my accuracy test used a FIXED 2bp gap while the live gate is vol-scaled
  (fv >= 0.995), so it mixed trades the bot would take with ones it would
  refuse. Re-ran with the real vol-scaled gate on Aug 7 (288 windows, 1s
  Binance proxy, rolling 300s vol, no lookahead): it helps but not enough —
  L=90 92.4%->95.3%, L=60 94.5%->96.1%, L=45 95.2%->97.6%, all still under
  the break-even the book demands. Then swept stricter thresholds and four
  cells appeared to BEAT the market (L=120 fv>=0.9999 100% n=12 +3.08c;
  L=90 fv>=0.99999 100% n=14 +2.33c; L=30 fv>=0.9999 100% n=105 +0.93c).
  That is 20 lead x threshold combinations tested, so I ran Aug 8 as pure
  out-of-sample. THEY COLLAPSED: L=120 100%->80.0% (n=5), L=90 100%->85.7%
  (n=7), L=30 100%->95.8% (n=120, -3.24c). In-sample artifacts, exactly what
  multiple testing produces. Do not resurrect them without OOS.
  THE ONE SURVIVOR: L=45s, fv>=0.9999 — 98.6% on n=74 in-sample AND 98.6%
  on n=74 out-of-sample. Identical across two days, decent sample. As a
  TAKER that is EV -0.00c/share: exactly, precisely break-even against an
  0.985 ask plus fee. The forecast is not the problem at 45s; paying the
  spread is 100% of the problem. MAKER ARITHMETIC (maker fee = 0, verified
  live): resting at 0.97 -> +1.60c/sh ($4.00 per 250), at 0.96 -> +2.60c/sh
  ($6.50), at 0.95 -> +3.60c/sh ($9.00). ~74 signals/day on BTC 5m alone.
  UNKNOWN AND DECISIVE: fill probability on a resting bid at those prices —
  who crosses to us, how often, and whether the flow that does is adversely
  informed. bot/book_record.py already stores bid/bid_sz and now runs on
  btc 5m, eth 5m and btc 15m, so the data is accruing. NOTE the venue's own
  history here: the repo's toll strategy was maker-side and was never
  live-qualified because queue position was unproven — same open question,
  and its notes should be mined before building anything.
  PROCESS NOTE: two Fable 5 review agents were launched at the owner's
  request and both died on out-of-usage-credits before returning findings.

- 2026-08-09 SELF-REVIEW (owner asked for a Fable 5 audit; both agents died
  on usage credits, so this is the Opus 5 review of my own chain).
  FIRST, THE SUSPICIOUS NUMBER: L=45 fv>=0.9999 gave n=74, 73 correct on BOTH
  Aug 7 and Aug 8 — identical, which smelled like a duplicated dataset. It is
  not: verified different price ranges (64167-65391 vs 64823-65192),
  non-overlapping window ids, and 3x different realised vol (0.359 vs
  0.110bp). The stability is the vol-scaling WORKING — a fv gate normalises
  by vol*sqrt(tau), so it admits small gaps on calm days and demands big ones
  on busy days, which is exactly why signal count and accuracy hold steady.
  That is evidence FOR the gate, not against it.
  INDEPENDENT ASSET CHECK: same gate on ETH. L=45 gives 55/100% (Aug 7) and
  92/100% (Aug 8). POOLED over btc+eth x 2 days, across a 5x vol range
  (0.110-0.536bp): L=45 -> 293/295 = 99.3%; L=30 -> 98.1% (n=424); L=60 ->
  96.4% (n=196). L=45 is the peak and it is not a one-cell fluke.
  ECONOMICS at L=45: taker EV +0.72c/sh against the 0.985 ask, maker at 0.96
  +3.32c/sh. Wilson 95% CI on the accuracy is [97.56%, 99.81%]; at the LOWER
  bound the taker is -1.04c (dead) but the maker at 0.96 is still +1.56c.
  That asymmetry is the whole argument for the maker route.
  WEAKEST LINK, STATED PLAINLY: the 0.985 ask at L=45 rests on TWO
  observations from the 20-window book sample, and winner-side quote
  availability (58% at L=30) is measured on the same tiny sample. Every
  dollar figure above is hostage to those two numbers. The book recorders
  now running on btc 5m / eth 5m / btc 15m will replace them with hundreds
  within a day; nothing should be built or restarted before they do.
  OTHER REVIEW FINDINGS: (1) no lookahead — vol uses [t-300,t], strike is
  pre-open, and at L>=30 the closing average has not begun so est is simply
  the spot; (2) Binance is a proxy for Chainlink, and its basis noise can
  only DEPRESS measured accuracy, so 99.3% is a floor not a ceiling; (3) the
  fv denominator still prices the closing TICK's uncertainty, which is wider
  than the average's — the gate is conservative by construction, which is
  why a 0.9999 threshold behaves like a much higher true confidence;
  (4) both test days were calm in absolute terms (0.32-1.05%/day) — nothing
  here speaks to a 3%/day regime; (5) 1 of 20 swept cells surviving OOS is
  still multiple testing, mitigated but not erased by the ETH replication.

- 2026-08-09 TAKER VERDICT IS FINAL: PRICED. Real Chainlink grid, 332 BTC +
  97 ETH windows, |gap|>2bp, accuracy vs the break-even the book charges:
    BTC  L=120 94.6% vs 96.4% (-1.8pp) | L=90 95.3% vs 98.1% (-2.8pp)
         L=60  98.5% vs 98.5% (0.0pp)  | L=45 98.5% vs 99.2% (-0.7pp)
    ETH  L=120 94.3% vs 95.3% (-1.0pp) | L=90 96.9% vs 97.3% (-0.4pp)
         L=60  94.4% vs 98.6% (-4.2pp) | L=45 100%  vs 98.7% (+1.3pp, n=33)
  Negative or exactly break-even everywhere with real size behind it, on two
  independent coins, agreeing with the Binance-proxy runs to within half a
  point. RETRACTION: I hypothesised that my proxy's basis noise was hiding
  real accuracy — it was not. The proxy said 94.1% at L=120 BTC and the true
  feed says 94.6%. The 36/36 clean run in edge_report that prompted that
  hypothesis had an 11-23% chance of occurring at the measured accuracy;
  it was luck, and I let it pull me toward a conclusion the larger sample
  did not support. Third time this week a small sample has done that, so:
  no conclusion from fewer than ~100 signals, without exception.
  WHAT REMAINS, AND IT IS THE LAST ONE: not paying the spread. Maker fee is
  ZERO. At L=60 BTC our 98.5% against a 0.984 ask is dead even; the same
  98.5% against a bid resting at 0.96 is +2.5c/share. The forecast does not
  need to improve at all — the entry price does. bot/maker_report.py
  estimates fill probability from the snapshots already recorded: a bid at P
  counts filled if any later snapshot shows an ask <= P. Biases stated in
  the file (optimistic on queue position, pessimistic on unseen dips).
  If the fill rate is a few percent this is dead; if it is 20%+ at 0.96,
  the original strategy survives as a maker and the next step is papering it.

- 2026-08-09 MAKER ROUTE: THIN, NOT DEAD — and my own tool lied first. The
  first maker_report run printed $129/day (btc) and $150/day (eth) at a 0.98
  bid. Both figures were artifacts of two bugs I wrote into it:
  (1) it counted a match as a FILL whenever the ask at entry was already at
  or below our bid — that is a TAKER trade, and 94% of those "maker fills"
  were exactly that, i.e. the break-even trade we had already rejected;
  (2) it computed EV from the realised outcome of the filled subset, which
  was 17/17 and 16/16, instead of the accuracy measured over hundreds of
  windows. A clean run of 17 at 94.6% true accuracy happens 39% of the time.
  CORRECTED (EV = measured accuracy - price, genuine resting fills only):
    btc  0.98 -3.40c  0.96 -1.40c  0.94 +0.60c  0.92 +2.60c  0.90 +4.60c
    eth  0.98 -3.70c  0.96 -1.70c  0.94 +0.30c  0.92 +2.30c  0.90 +4.30c
  Profitable only at 0.94 and below, and the genuine resting-fill count
  there is 1-2 per bid level across 34 HOURS. Both coins together:
  ~$43/day at 250 clip, on roughly 3-5 fills/day, with adverse selection
  entirely unpriced. That is not nothing — the old strategy ran $60-100/day
  — but it rests on single-digit fill counts, so it is a hypothesis, not a
  result. maker_report.py now excludes @touch fills and uses the measured
  accuracy table; the raw first-run numbers must never be quoted.
  NEXT: more days of book data (recorders running on 3 markets), then a
  paper maker bot ONLY if the fill count holds up at 0.92-0.94 over a week.

- 2026-08-10 INDEPENDENT AUDIT (two reviewers, code and numbers). Findings
  that change conclusions, worst first.
  * TOLL WAS NEVER MIGRATED. bot/strategies/toll.py still calls the winner
    from raw spot prints (line ~186) and writes toll.oracle_calls, which
    bot/main.py:47 reads BEFORE the TWAP branch — so wherever the toll runs,
    the mismatch tripwire silently reverts to the pre-08-07 rule.
    polybot-toll.service sets no TOLL_ENABLED=0, no FAMILY: it is btc 5m,
    toll_enabled, oracle_authoritative, TOLL_PREPOSITION=1, and bootstrap.sh
    enables it. Spot and TWAP calls disagree on 8% of all windows and 3-5% of
    the windows the toll actually trades; each wrong call rests 250 shares at
    0.99 on the LOSING token, ~$250 of wrong-side notional against ~$2.50 of
    upside. Verified in code: oracle_calls is only written inside
    _trade_window, which requires toll_enabled AND oracle_authoritative, so
    the snipe units (TOLL_ENABLED=0) are unaffected — the exposure exists
    only if polybot-toll itself is running. CHECK AND STOP IT.
  * THE ACCURACY TABLE IS UNREPLICATED AND PROBABLY 3-5pp OPTIMISTIC. An
    independent recomputation over pooled 08-07/08-08 gives BTC L=120 89.5%
    (claimed 94.6), L=90 92.2% (95.3), L=60 94.6% (98.5), L=45 95.2% (98.5);
    ETH L=120 89.3% (94.3). Seven of nine claimed cells fall outside its 95%
    CI. Corroborating: my OWN earlier proxy run on 08-07 gave L=90 92.4%,
    L=60 94.5%, L=45 95.2% — i.e. the runbook holds two inconsistent
    estimates of the same quantity and the final entry quoted the optimistic
    one. Suspected mechanism: timing_scan applies MIN_COVER=0.9 to a grid
    whose density is only 89.5%, so it scores a coverage-selected minority of
    windows (56 of 332 at L=120). If the lower numbers are right, the taker
    table goes from "break-even at L=60" to -3.7c/share and EVERY maker bid
    level flips negative. Resolve by fixing grid density first, then re-running
    timing_scan and recording raw counts.
  * THE MAKER ROUTE IS DEAD ON ITS OWN TERMS, independently of the above.
    maker_report priced a CONDITIONALLY ADVERSE fill set with an
    UNCONDITIONAL win rate: a row only counts as filled when a later snapshot
    shows an ask at or below our bid, and on this venue that is close to the
    definition of "our side is losing" (the winner's ask climbs to 0.99, the
    loser's falls through 0.94 toward zero). The correct quantity is
    P(win | filled), which we cannot estimate from 1-2 fills. The reviewer
    reproduced the bug on a synthetic zero-edge book: truth -$3,850, reported
    +$402. THE "~$43/day MAKER ROUTE" IS RETRACTED.
  * edge_report still computes EV and "best lead" from the realised outcome
    of 7-36 trades — the exact statistic its own footer warns against, and
    the machinery that produced the retracted 36/36 excitement. Its need%
    is also measured on the OFFERED subset, which over-represents wrong picks
    (winner quotes vanish, loser quotes persist), so the taker table is
    optimistic rather than conservative.
  * "Observed accuracy beats random-walk theory, so vol clustering" is a
    CONDITIONING ARTIFACT: theory was evaluated at exactly 2bp while
    observation averaged the whole |gap|>2bp set (median 5.8bp). Integrated
    over the observed gap distribution, theory predicts 96.7% at L=60 vs
    94.5% observed — observation sits at or BELOW the plain random walk.
    There was never an anomaly to explain.
  * The fv gate is NOT a "~6bp distance filter" (claimed in the 08-09
    simplification entry). Replayed live config fires on 77-83% of windows at
    T-6s at 93-98% accuracy against a 97.2% break-even. Comment corrected.
  * book_record recorded an HTTP failure as "no offer", and failures cluster
    at the close where the drought is claimed — so part of the measured
    quote decay may be the recorder. FIXED: retry once, then store err=1.
  * snipe could reach u=0 via clock skew (clock_ok tolerates -0.75s), making
    the closing average fully determined and handing paper a certainty live
    could never have. FIXED: refuse when n_elapsed >= n_twap.
  * oracle_tie_bps=2.0 disabled the mismatch tripwire on 34-71% of windows
    against a ~0.05bp reconstruction error. FIXED: 0.3bp for 5m/15m, 2.0 kept
    for the 1h family.
  CONFIRMED UNCHANGED by both reviewers: the rule change and its date, the
  TWAP reconstruction and oracle.py (clean), the fee model, the out-of-sample
  collapse of the strict-threshold cells, and the estimator fix. The 1s
  lookahead in the analysis scripts is real but immaterial (<=0.6pp).

- 2026-08-10 WHERE THE MONEY ACTUALLY CAME FROM (third audit — the finding
  that reframes the entire post-rule-change effort). P&L decomposed by entry
  price over 283 old-rule BTC windows, +$2,536:
      px < 0.50   n=46   6,435 sh   +$1,154.57   46% of profit  +17.9c/sh
      0.50-0.80   n=52   8,204 sh     +$856.16   34%            +10.4c/sh
      0.80-0.90   n=38   5,323 sh      +$89.38    4%             +1.7c/sh
      0.90-0.95   n=43   7,237 sh     +$497.80   20%             +6.9c/sh
      px >= 0.95  n=104 15,814 sh      -$61.50   -2%             -0.4c/sh
  Entries at or above 0.95 are 37% OF ALL SHARES EVER TRADED and produced
  MINUS $61.50. z = -0.06 against a fair market: not thin, zero. Pooled with
  ETH: -$0.50 on 17,460 shares. Price-floor counterfactual ($/day): no floor
  $121, floor 0.80 $25, floor 0.90 $21, floor 0.95 -$3.
  THE IMPLICATION: every analysis I ran after the rule change — the taker
  study at 0.96-0.99, the whole maker study at 0.92-0.94 — targeted the one
  price region that provably never had an edge, in the regime where the
  strategy DID work. The runbook already contained this ("px>=0.95 is
  structurally thin, breakeven ~97%, observed 95.2%", 08-07 loss audit) and
  every subsequent step walked past it.
  WHAT THE EDGE ACTUALLY WAS — not speed, not forecasting. The bot selected
  knife-edge windows (median |final margin| 1.27bp vs 4.06bp baseline; in the
  cheap bucket the price had crossed the strike a median 7s before the close,
  41% within the final 6s). At those margins WHICH FEED DECIDES is close to a
  coin flip: Chainlink-vs-Binance-tape agreement is 55.6% at 0-0.5bp, 87.5%
  at 0.5-1bp, and the median |closing tick - closing 30s TWAP| is 0.46bp,
  comparable to the decision margin itself. The order book priced the
  exchange tape's answer; Polymarket settled on Chainlink. We were buying the
  RESOLVER's answer at the TAPE's price. Direct proof: rescore the identical
  trades against the Binance close instead of the Chainlink outcome and
  +$2,536 becomes -$2,082. And we were not even the fast side — in the cheap
  bucket spot sat on our side 87-91% at T-30/-20/-10 and only 40% at T-3, so
  the late move went AGAINST us and we were reading a pre-move print.
  WHY THE TWAP ENDS IT, DELIBERATELY. Freeze test, share of the outcome still
  undetermined at L seconds: L=6 old 2.28% vs new 0.26% (8.9x, and 13.1x on
  knife-edge windows); L=120 old 22.8% vs new 21.1% (1.08x). The change is a
  precision strike on terminal sniping that leaves the 2-minute forecasting
  problem untouched. It also averages away the tick idiosyncrasy (~sqrt(30))
  that made the resolver's identity worth more than the direction call. Both
  legs of the edge, removed on purpose.
  "THE EDGE MOVED EARLIER" IS A CONSERVATION-OF-EDGE FALLACY. Nothing moved.
  At L=120 the new rule is 1.08x as hard as the old one — that forecasting
  problem existed unchanged before the change, was reachable with the same
  code, and the bot never made money there: its own ledger says 0.90+ earned
  $21/day and 0.95+ earned -$3/day. RETRACTED.
  ALSO FLAGGED: top 20 of 283 windows > 100% of profit; day-block bootstrap
  5th percentile is $35/day against the $60-100/day figure quoted to justify
  the maker route. ETH is not an independent check (rho=0.809 with BTC on 5m
  returns, same 48 hours, error correlation +0.223) — pooling n=295 as
  independent overstates precision and cannot address the calm-regime risk.
  OPEN AND UNRESOLVED — paper/live fill parity on the load-bearing bucket.
  snipe_take_recheck_s runs in PAPER ONLY: paper sleeps 0.5s, re-reads the
  book and sweeps it, and _ask_ok has no "price has not collapsed" condition,
  so a book falling 0.97 -> 0.29 passes and paper buys the wreckage (5 of 24
  sampled fills saw the ask drop >=10c across that sleep; one dropped 68c on
  250 shares). MY READ, recorded as a partial dissent: live sends a
  marketable limit that ARRIVES ~0.25-0.5s later and would sweep the same
  collapsed book at the same prices, so this may be far less severe than an
  outright paper-only artifact. But it is unexamined, it lands on the 81% of
  profit that came from cheap entries, and it must be settled before any
  claim about the record's validity. That audit is upstream of everything.
