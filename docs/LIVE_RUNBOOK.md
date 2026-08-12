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

- 2026-08-10 A LIVE HYPOTHESIS WITH THE OLD MECHANISM: FEED DIVERGENCE.
  Owner pushed back on the "it's dead" conclusion. He was right that I had
  stopped one step short. The third audit established WHAT the old edge was:
  the book priced the exchange TAPE while Polymarket settled on CHAINLINK,
  and at knife-edge margins those disagree. I treated the rule change as
  deleting that discrepancy. It did not — it MOVED it. Settlement is now a
  30s Chainlink TWAP; a participant estimating the outcome from spot (the
  exact error this repo shipped and had to fix on 08-09, at a cost of
  $85.89) is systematically wrong whenever spot and the TWAP diverge.
  MEASURED, Aug 7-8, 574 windows per coin, no lookahead, Binance 1s as both
  the spot view and the TWAP proxy. Divergence occurs in ~6% of windows
  (~17/day/coin). When it does, the TWAP side wins:
      BTC  Aug-7 82.4% (n=17)  Aug-8 64.7% (n=17)  pooled 73.5% (n=34)
      ETH  Aug-7 92.0% (n=25)  Aug-8 78.9% (n=19)  pooled 86.4% (n=44)
  Four of four day-asset cells above chance; ETH stronger and steadier.
  WHY THIS ONE IS DIFFERENT FROM EVERYTHING ELSE TRIED THIS WEEK:
  (1) It is a MECHANISM (two feeds, one book) rather than a claim that we
      out-forecast the market.
  (2) It INVERTS the liquidity problem. Every drought measured — winner-side
      quotes at 13-27% near the close — was on the side the book agrees is
      winning. In a divergence window the side we want is the one the book
      thinks is LOSING, and that side is quoted 95-100% of the time
      throughout. The trade wants exactly the inventory that is always there.
  (3) It targets CHEAP entries, the price region that produced 81% of all
      historical profit, instead of the 0.95+ region that produced -$61.50
      on 15,814 shares.
  (4) Its shape matches the old edge: buy at what the book calls 10-30%,
      win 74-86%. The old cheap bucket won 49.9% against 30.6% implied.
  THE DECISIVE UNKNOWN IS THE PRICE, and only the droplet's book recording
  can answer it. bot/divergence_report.py measures it: for each divergence
  window it looks up what the TWAP side actually cost, prices EV against the
  win rate of the FULL divergence population (never the filled subset — that
  error produced two retracted results this week), treats a failed fetch as
  unknown rather than as an absent quote, and prints the 95% lower bound
  beside every number. n is 34-44 per coin; four of the last five promising
  results on this project died out of sample. This one gets the same
  treatment: no action until the price is measured and the signal survives
  days it has not seen.

- 2026-08-10 DIVERGENCE: REFUTED BY THE PRICES. The book recording came back
  and it kills the hypothesis above. I predicted that a spot-driven book
  would price the TWAP side at 0.10-0.30. What it actually charges, btc,
  real Chainlink feed:
      lead   our win rate   predicted    actual ask
        3          100%     0.10-0.30         0.990
        6          100%     0.10-0.30         0.987
       10          100%     0.10-0.30         0.980
       30           90%             —         0.777
       45           73%             —         0.943
       60           61%             —         0.598
  The book SIDES WITH THE TWAP. It is not making the spot mistake, so there
  is no wrong price to buy. Point (2) of the claim above inverts too: the
  reason the quote rate is 4-12% is that the TWAP side IS the side the book
  thinks is winning, which is exactly the side that stops being offered.
  ETH is worse than refuted at L=30/45 — 50% and 56%, i.e. the market is
  better than our signal there. Every positive-EV cell rests on 1-5 quoted
  observations and every lower bound is negative. RETRACTED in full.

- 2026-08-10 A DEAD END CLOSED CHEAPLY: SYNTHETIC LIQUIDITY. Polymarket's
  CTF exchange mint-matches complementary BUY orders, so a bid on DOWN is an
  offer on UP. book_record has been storing bid/bid_sz since day one and no
  report ever read those columns — if the per-token books were separate,
  the "winning side is not offered" drought would have been a measurement
  artifact and the fix would have been free. Checked live on two windows:
  ask(up) == 1 - bid(down) to the tick, both sides, both windows. The API
  already merges. The drought is real. No further work here.

- 2026-08-10 WHAT THE RULE CHANGE ACTUALLY CREATED: THE OPEN IS OFF-STRIKE.
  Every study since the cutover, mine included, has aimed at the last few
  seconds of the window. That is where the book is empty and the winner
  costs 0.98. It is also the region the ledger says never paid: px >= 0.95
  earned -$61.50 on 15,814 shares, z = -0.06, while px < 0.80 earned 80% of
  all profit.
  The rule change did something at the OTHER end of the window that nobody
  has looked at. Under spot/spot settlement the strike was spot at the open:
  at T the price WAS the strike and the market was a genuine coin flip.
  The strike is now a TRAILING 30s MEAN (60s for 15m). A trailing mean lags.
  So at T+0 spot already sits some distance from the number it will be
  judged against, the closing average is centred on spot rather than on the
  strike, and the market does not open at 50/50 — it opens tilted, and the
  tilt is readable at T+0 from the grid with no forecast of any kind.
  Under the old rule this offset was identically zero. The trade could not
  have existed before 08-07. It is not the old edge relocated.
  Rough size, BTC at ~0.9bp/s: the trailing-mean offset has sd ~2.8bp
  against a 15bp terminal move, i.e. z ~ 0.18, so a typical open is worth
  ~57/43 and a 2-sigma open ~64/36 — against a book quoted 0.49/0.51 with
  hundreds of shares a side. Both the price region and the depth are the
  ones the historical record liked.
  MEASUREMENT, NOT A CLAIM. bot/open_offset.py scores it on grid + official
  outcomes only, no book, so nothing it prints is P&L. It buckets by tilt,
  prints the diffusion model's value beside the realised win rate as a
  sanity yardstick, prints the highest price that still breaks even AT THE
  95% LOWER BOUND, and runs a far-shuffle control that must land on 50%.
  bot/price_curve.py is the companion: accuracy against what the book
  actually charged, decomposed BY PRICE — the split that located the old
  edge — conditioning hit rate and price on the same rows, which is the
  step whose absence produced the retracted maker result.
  The book recorders now sample T+2/T+15/T+30 (leads 298/285/270 on 5m,
  898/885/870/840/780 on 15m). That moment has never been recorded and
  cannot be back-filled. NO BOT CHANGE, and none until the tilt survives a
  second disjoint sample and the measured open price leaves room after fees.

- 2026-08-10 THE OPENING TILT IS REAL, IS GENUINELY NEW — AND IS ALREADY
  PRICED. Measured here, not on the droplet: 21 days of Binance 1s closes
  for BTC and ETH joined to real Polymarket settlements and to the CLOB
  price-history endpoint, which is public and retrospective and therefore
  answered in an hour what the book recorders would need a week to answer.
  STEP 1 — does the tilt predict? Self-settled, 1998 windows, BTC 5m:
      |tilt|      n    win%        95% CI    random-walk model
      0.0-0.5   923   52.8%  [49.5,56.0]%          50.8%
      0.5-1.0   349   58.5%  [53.2,63.5]%          54.4%
      1.0-2.0   398   53.3%  [48.4,58.1]%          58.6%
      2.0-3.0   177   58.8%  [51.4,65.7]%          64.4%
      3.0-5.0   114   64.0%  [54.9,72.3]%          71.8%
      5.0-8.0    31   58.1%  [40.8,73.6]%          82.7%
      ALL      1998   55.2%  [53.0,57.3]%
  Yes: +5.2 points, z ~ 4.6. But note it falls FURTHER below the random
  walk the larger the tilt gets. The tilt mean-reverts, so it is worth
  materially less than the diffusion says — the fitted realised value is
  ~0.031 of price per bp against the model's 0.060.
  Also note the tilt is half the size I guessed: sd 1.68bp, not 2.8bp, on
  measured vol of 0.384 bp/s rather than the 0.9 I assumed.
  STEP 2 — does the book price it? Slope of (quote at the open - 0.50) on
  the tilt, same method both sides of the cutover:
      BTC pre-change   (Aug 1-6, tilt worthless)   +0.0024 per bp   n=1438
      BTC post-change  (Aug 7-9)                   +0.0375 per bp   n= 847
      ETH post-change  (Aug 7-9)                   +0.0172 per bp   n= 847
  The pre-change slope is the control and it is flat, which is exactly what
  a blind book looks like and is what validates the measurement. Post
  change both coins price it, correlation +0.30 to +0.33. BTC's +0.0375 is
  ABOVE the tilt's realised worth of ~0.031. The book is not underpaying.
  STEP 3 — what is left after fees?
      BTC post-change  ALL  55.5% at 0.527  ->  +1.00c/sh  [lower -2.36c]
      ETH post-change  ALL  54.5% at 0.528  ->  -0.05c/sh  [lower -3.41c]
      BTC pre-change   ALL  52.6% at 0.508  ->  -0.02c/sh   (placebo)
  Nothing. The single cell that looked alive — BTC 0.5-1.0bp, +12.4c/sh,
  lower bound +1.05c, n=73 — does not replicate on ETH (-4.69c) and is
  non-monotonic against the 1.0-2.0bp cell beside it. That is the exact
  shape of the four cells already killed out of sample this week. Not
  wiring it.
  WHY THE FEE DECIDES THIS. 0.07*p*(1-p) is 1.75c/share at 0.50 and 0.14c
  at 0.98 — 12.5x. Trading at the open means paying the worst fee on the
  board, so the edge must clear ~2 points of win rate before it clears
  zero. The tilt's whole unpriced residue is smaller than that.
  FEED NOISE IS NOT HIDING THE SIGNAL, which was the obvious objection to
  using Binance as a stand-in for Chainlink. If basis noise were eating it,
  the win rate scored against REAL Polymarket settlement would fall below
  the self-settled one. It does not: 55.5% against 55.2%. bot/open_join.py
  reruns the whole thing on the droplet's genuine Chainlink grid anyway.

- 2026-08-10 THE OLD MECHANISM IS GONE FROM THE ENTIRE SHORT-HORIZON BOOK.
  The edge that produced the record was resolver-feed != book-feed. Checked
  every up-down family on gamma: btc, eth, sol, xrp and doge, at both 5m
  and 15m, all ten now read data.chain.link/streams/<coin>-usd-twap-*. Not
  one family was left behind on the spot rule. There is no corner of this
  complex where the original trade is still legal.
  STANDING CONCLUSION FOR 5m/15m: measured at the close (asks 0.98-0.99
  where our call is perfect), in the middle (divergence refuted), and now
  at the open (tilt priced at or above its worth, both coins). Three
  independent looks, same answer. These markets are priced against us at
  every point in the window we can measure. Any further work on this
  complex needs a NEW mechanism, not a better estimator.

- 2026-08-10 THE PARITY AUDIT, BUILT — AND A PREDICTION MADE BEFORE IT RUNS.
  Confirmed by reading the code, not by inference: snipe_price_floor
  defaults to 0.0, so the post-latency recheck in bot/strategies/snipe.py
  accepts ANY collapse. A book that falls 0.97 -> 0.01 during the 0.5s
  latency gate still clears _ask_ok, and PaperExecutor.take then sweeps the
  wreckage from the cheapest level up.
  WHAT LIVE ACTUALLY DOES, stated precisely so the counterfactual is right.
  Live skips the sleep, sizes and prices off the PRE-latency book, and sends
  a marketable limit at snipe_ask_max. That order does reach the collapsed
  book — an FAK at 0.97 sweeps a 0.29 ask — so the fill is not fictional.
  What is optimistic is the RACE: paper takes the top of the collapsed
  ladder deterministically, while a real order lands a quarter-second late
  into the exact moment every other taker is grabbing the same cheap shares.
  A live fill price therefore sits between the collapsed ask and our limit.
  MY EARLIER PARTIAL DISSENT WAS HALF RIGHT AND I WANT IT ON THE RECORD AS
  HALF WRONG. Right: live does sweep the same collapsed book, so this is
  not a pure paper-only artifact. Wrong: I treated that as making the issue
  minor. It does not. Winning the race is the whole difference between
  paying 0.29 and paying 0.97 on the trades that carry 81% of the profit.
  THE PREDICTION, WRITTEN DOWN FIRST. Under the old rule the book tracked
  the exchange TAPE and we settled on CHAINLINK. A collapse to 0.29 on a
  side our oracle rated fv>=0.995 is the tape saying we are wrong while
  Chainlink says we are right — which IS the feed-divergence trade, in its
  purest form. So I expect collapse fills to show a HIGH win rate, not a
  low one, and the audit's real finding to be about PRICE, not accuracy.
  If instead collapse fills win less than clean ones, my whole account of
  the old mechanism is wrong and it must be retracted.
  bot/parity_audit.py joins the depth telemetry (ask ladder at signal time
  and after the latency gate) to the settled takes and reports: the win
  rate of collapse vs clean fills, which needs no assumption about live at
  all; then RECORDED (won every race), AT SIGNAL (lost every race), and
  DROPPED (collapse path removed), decomposed by entry price. Live lies
  between the first two. NO CODE CHANGED — the price floor stays 0.0 until
  the number says what it is worth.

- 2026-08-10 NEXT MECHANISM, SCOPED NOT BUILT. The thing that paid was two
  feeds disagreeing about one event. That is gone from Polymarket's whole
  short-horizon complex, but it is not gone from the world. Checked from
  here: Kalshi is reachable and its BTC series settles on CF BENCHMARKS
  BRTI, while Polymarket's untouched 1h family settles on BINANCE via UMA
  and the 5m/15m complex settles on CHAINLINK. Three different resolvers,
  one underlying, overlapping clocks. That is the same shape as the trade
  that produced the record, in a place the rule change did not reach.
  This is a scoping note, not a result. Nothing is built and nothing is
  claimed. It goes ahead of task #18 (the stale Kalshi overlay) only after
  the parity audit reports, because the parity number decides whether this
  project's historical record can be trusted as a template at all.

- 2026-08-10 THE BINANCE PROXY WAS WRONG AND THE REAL FEED IS BETTER.
  RETRACTING the 08-10 entry above titled "already priced". The droplet ran
  the same tests on the genuine Chainlink grid and the key input differs by
  a factor of 3.6:
      Binance 1s last trade   0.384 bp/s   -> 6.6bp per 5m window
      Chainlink 1s grid       0.108 bp/s   -> 1.9bp per 5m window  (btc)
                              0.185 bp/s   -> 3.2bp                (eth)
  Binance last-trade prints carry bid-ask bounce; the Chainlink grid does
  not. So most of what I measured as "tilt" on Binance was microstructure
  noise, and two conclusions built on it fall:
  (1) "The tilt mean-reverts." Artifact. A noisy tilt reverts BY
      CONSTRUCTION — big measured values are mostly noise and regress. On
      the clean feed at 1-2bp the realised win rate is ABOVE the random
      walk (btc 88% vs model 78% at T+0), not below.
  (2) "The book pays ~63% of the model, so nothing is left." On the real
      feed it pays 48% (btc, +0.0979 vs +0.2033 per bp) and 35% (eth,
      +0.0434 vs +0.1227).
  MY DEFENCE OF THE PROXY WAS ALSO INVALID and that is the part worth
  remembering. I argued the proxy was safe because the win rate scored
  against real settlement (55.5%) matched the self-settled one (55.2%).
  Both used the SAME noisy tilt, so that comparison could not detect
  attenuation. It tested nothing. Correlated errors do not cross-check.
  WHAT THE REAL FEED SAYS, 5m, quote within 45s of the open (median 11s),
  ask taken as mid + 0.005, 1.47 days:
      btc  |tilt| 1.0-2.0bp  n=16  87.5% at 0.637  ->  +22.1c/sh  [lo -1.4c]
      eth  |tilt| 1.0-2.0bp  n=21  76.2% at 0.601  ->  +14.4c/sh  [lo -6.9c]
      btc  ALL              n=360  55.3% at 0.530  ->   +0.5c/sh  [lo -4.7c]
      eth  ALL              n=319  52.0% at 0.521  ->   -1.9c/sh  [lo -7.3c]
  The edge, if it is one, is NOT in the average window. It is the ~5-7% of
  windows that open more than 1bp off strike, roughly 15-20 a day per coin,
  and BOTH COINS AGREE THERE. That is the first time in this whole week
  that two independent samples have pointed the same way. It is also n=16
  and n=21 with lower bounds still under zero, so it is a lead, not a
  result. No bot change. Let the grid and the T+2/T+15/T+30 book recording
  accumulate and re-run; the ask is currently a mid plus an assumed half
  spread, and the recorders will replace that with the real number.

- 2026-08-10 PRICE_CURVE, FIRST 9h OF BOOK DATA. Signal accuracy against
  what was charged, btc 5m, whole population:
      lead 120  77.1% quoted 98%  ask 0.758      lead 30  95.8% q67% 0.878
      lead  60  82.1% quoted 87%  ask 0.797      lead 20  97.6% q32% 0.902
      lead  45  89.6% quoted 82%  ask 0.846      lead  6  97.5% q12% 0.715
  The quote rate falling from 98% to 12% as the close approaches is the
  drought, now measured on the full population rather than inferred.
  Every properly-conditioned cell in the by-price table has a NEGATIVE
  lower bound. The one cell the tool flagged positive is n=3. Nine hours is
  not a sample; this is a baseline to re-run against, nothing more.

- 2026-08-10 PARITY AUDIT COULD NOT RUN — MY PATH BUG, NOW FIXED. Each unit
  keeps its own ledger at bot/data/<unit>/paper.db; the tool looked for
  bot/data/paper.db and exited. It now discovers every unit ledger under
  bot/data/*/paper.db, prints per-unit coverage, and pools. The prediction
  recorded before it runs stands unchanged.

- 2026-08-10 THE AHL MOMENTUM SIGNAL DOES NOT WORK HERE — AND THE REASON
  GENERALISES. Owner brought a ManAHL multi-horizon trend strategy: score =
  sum of sign(close - close[n]) over four lookbacks, position scaled by
  1/vol. Adapted faithfully to the btc 5m binary (lookbacks in 5m BARS, so
  the lookback-to-hold ratio matches the daily original) and tested on 21
  days of 1s data, 6,006 windows:
      score  n     mean fwd move   t      P(up)
        -4  1246      +0.021bp   +0.08   51.6%
        +4  1277      -0.233bp   -0.95   47.1%
      regression: -0.028bp per point of score, t = -0.77
  The binary version: the score's side wins 49.6% [48.2, 51.0] over 4,861
  windows. Vol-scaled strongest quartile 51.4%. Nothing.
  SWEPT 8 lookback sets x 2 directions x 2 coins with a train/test split.
  Best pooled |t| in 32 cells = 1.66 (eth reversal), and no cell clears 52%
  in BOTH halves. Momentum is dead here and so is its mirror.
  THE ARITHMETIC THAT KILLS IT, and it applies to every weak signal anyone
  brings to this market: AHL's annual Sharpe of ~1 is 0.0031 per 5m window,
  which as a binary win rate is +0.12 percentage points. The taker fee at
  0.50 is 1.75 points. You need 14x their edge to break even. They hold for
  weeks and pay the toll a few times a month; a 5m binary pays it 288 times
  a day. NOTHING with a weak directional edge can ever clear that bar here.
  Only two shapes can: near-certainty at a high price where the fee is
  0.07-0.20 points, or a LARGE mispricing at a middling price.

- 2026-08-10 BUT THE OTHER HALF OF AHL'S RULE IS THE BEST RESULT SINCE THE
  CUTOVER. "Position = signal / vol" is not a forecast, it is a statement
  about what a signal is WORTH, and that transfers exactly. The opening
  tilt is a distance in basis points; its value is that distance in
  standard deviations. Identical only if vol is constant, and it is not.
  ESTABLISHED FIRST ON THE LARGE SAMPLE (6,000 windows/coin), which is the
  right order — mechanism before pricing. Ranking by |tilt|/vol beat
  ranking by |tilt| in 8 of 8 matched slices across both coins. Same tilt
  in bp, split by regime:
      btc  calm third 66.3%  middle 60.5%  wild third 56.0%
      eth  calm third 61.9%  middle 58.4%  wild third 56.2%
  THEN PRICED against real Polymarket quotes, Aug 7-10, 847 windows/coin:
      btc  by raw |tilt|   top 5%  69.0% at 0.584 -> +8.93c  [lo -6.14c]
      btc  by |tilt|/vol   top 5%  83.3% at 0.637 -> +17.97c [lo +4.03c]
      btc  by |tilt|/vol   top 10% 75.0% at 0.608 -> +12.49c [lo +2.27c]
      eth  by |tilt|/vol   top 5%  73.8% at 0.581 -> +13.96c [lo -0.92c]
  Two btc slices with a POSITIVE 95% lower bound. First time since the rule
  change that anything has cleared that bar.
  AND THE MECHANISM IS STRUCTURAL, not a slice. Regressing the book's quote
  and the realised outcome on tilt, split by vol regime:
      btc calm  book +0.0975/bp  true +0.2342/bp  -> book pays 42%
      btc wild  book +0.0320/bp  true +0.0480/bp  -> book pays 67%
      eth calm  book +0.0368/bp  true +0.0742/bp  -> book pays 50%
      eth wild  book +0.0144/bp  true +0.0094/bp  -> book pays 154%
  The book DOES move more per bp when calm (0.0975 vs 0.0320) — it is not
  blind — but it under-adjusts, and the shortfall is concentrated exactly
  where the tilt is large relative to vol. That is an anchoring error on a
  fixed bp-to-probability mapping, and it is the precise error AHL's rule
  is built to harvest.
  CAVEATS THAT KEEP THIS A LEAD. n=42 and n=84 on the priced slices. The
  tilt here is measured on BINANCE, which is 3.6x noisier than the oracle
  grid, so this UNDERSTATES — but it is still the wrong feed. The ask is
  mid + 0.005, assumed. Four slice sizes x two rankings were examined.
  bot/vol_tilt.py reruns all of it on the real Chainlink grid. NO BOT
  CHANGE until that confirms and a second disjoint day agrees.

- 2026-08-10 VOL SCALING: RETRACTED ON THE REAL FEED, SAME DAY. The droplet
  ran bot/vol_tilt.py on the Chainlink grid and the result does not hold.
      btc  n=265   vol-scaled beats raw in 3 of 4 slices, but LOSES the top
                   slice (raw 92.3% vs scaled 84.6%)
      eth  n=188   raw beats vol-scaled in 4 of 4 slices
  On Binance it was 8 of 8. On the real feed it is 3 of 8. The mechanism
  table inverts on eth too: book pays 81% in calm and 50% in wild, against
  50%/154% on the proxy. Only one of sixteen cells has a positive lower
  bound (+0.12c, btc scaled top 10%), which is zero dressed up.
  ROOT CAUSE, AND IT IS THE SAME MISTAKE TWICE. Binance 5m-bar vol is 7.0bp
  against the grid's 2.8bp. I ranked windows by |tilt|/vol where BOTH terms
  came from the noisy proxy — a noisy signal divided by a noisy scaler. The
  ranking was largely sorting on noise level. I had already established
  that Binance overstates the tilt and wrote that it would merely
  UNDERSTATE the result; I never considered that it also corrupts the
  volatility estimate doing the ranking. That is the second retraction from
  the same proxy in two days.
  WHAT SURVIVES. The raw opening tilt, on the real feed, both coins:
      btc  raw top 5%   n=13  92.3% at 0.665 -> +24.3c  [lo -1.3c]
      btc  raw top 10%  n=26  76.9% at 0.655 ->  +9.9c  [lo -9.1c]
      eth  raw top 5%   n=12  66.7% at 0.563 ->  +8.6c  [lo -19.0c]
      eth  raw top 20%  n=37  64.9% at 0.595 ->  +3.7c  [lo -12.4c]
  Positive point estimates in the top slices on both coins, every lower
  bound negative, n between 12 and 37 on 1.53 days of grid. Unchanged in
  status: a lead. The AHL refinement is not part of it.
  STANDING METHOD RULE FROM HERE: no signal result on this market counts
  until it is measured on the oracle grid. Binance may generate hypotheses
  and may never confirm one. bot/proxy_calib.py exists to test whether a
  SMOOTHED Binance can be trusted for the archive — the decisive column is
  how often the proxy picks the wrong SIDE on the windows we would trade,
  not the correlation. Above ~5% wrong-side, the archive is unusable and
  research waits on the grid.

- 2026-08-10 THE PROXY IS CALIBRATED, AND IT CLOSES THE OPEN-TILT LEAD.
  bot/proxy_calib.py on 36.8h of overlap: with a 5s trailing-mean spot, the
  Binance tilt agrees with the grid's at corr 0.896, regression slope 0.944
  (near-unbiased), and — the column that decides — it picks the WRONG SIDE
  on 0.0% of |tilt|>1bp windows. 15s and 30s smoothing blow the slope up to
  1.23 and 10.6 because the smoothed spot converges onto the strike itself,
  which is the sanity check passing. The archive is usable at 5s.
  Note the 1s vol ratio over this overlap is only 1.2x (grid 0.127 vs
  binance 0.152 bp/s), not the 3.6x measured over 21 days — the last day
  and a half is an unusually calm stretch. That fact matters below.
  SIGNAL AT SCALE, 21 days, 6,047 windows per coin, self-settled:
      btc  0-0.5bp 53.2%  0.5-1 59.0%  1-2 58.8%  2-3 62.7%  3+ 63.4%
      eth  0-0.5bp 52.5%  0.5-1 55.0%  1-2 57.0%  2-3 59.4%  3+ 60.6%
      btc >=1bp  60.6% [58.1,62.9] n=1577 | thirds 55.2 / 59.2 / 67.2
      eth >=1bp  58.3% [56.2,60.3] n=2167 | thirds 56.8 / 58.0 / 60.0
  THE OPENING TILT IS REAL. Monotone on both coins, every third above 55%,
  tight intervals on thousands of windows. That is no longer in question.
  AND IT IS PRICED TO WITHIN 0.6 POINTS. Pooled against real quotes at
  |tilt|>=1bp: 134/206 = 65.0% at 0.569, needing 58.6%, EV +6.42c/share
  with a lower bound of -0.31c. But the priced sample is three days inside
  the calm stretch noted above, and the 21-day truth is 59.3%, not 65%:
      btc  priced 67.4%  vs 21-day 60.6%  -> +6.8 points of regime luck
      eth  priced 63.2%  vs 21-day 58.3%  -> +4.9 points
  Against a break-even of 58.6%, the realistic edge is +0.6 points. It is
  gone at mid+0.015 and negative at mid+0.020, and mid+0.005 already
  assumes the tight 1c spread. RETRACTING my own expectations from earlier
  today: the 92.3% / 87.5% / 83.3% top-slice cells were small samples drawn
  from a calm regime, and I quoted them as if they were the edge.
  THE WEEK'S UNIFYING RESULT. Every signal reachable at 5m scale is worth
  less than the toll. AHL momentum: +0.12 points against a 1.75-point fee.
  The opening tilt: +0.6 points against the same wall. The old edge was
  never a signal — it was a feed discrepancy worth 19 points, which is why
  it cleared. Beating this market requires something structurally large,
  not something statistically real.
  STILL OPEN, in order: (1) bot/parity_audit.py has never run, and it
  decides whether the historical record is trustworthy at all; (2) the real
  ask at T+2/T+15 from the recorders, which turns the +0.6 into a number
  rather than an assumption; (3) Kalshi KXBTC15M — CONFIRMED to run the
  SAME rule as Polymarket's 15m (60s average at close vs 60s average at
  open) on the SAME :00/:15/:30/:45 clock, settled on CF Benchmarks BRTI
  instead of Chainlink, with 800-1900 contracts of depth. Two venues, one
  event, two resolvers. That is a price gap, not a signal, so it is the one
  remaining candidate not capped by the fee wall — a cross-venue gap can be
  10 points where a forecast is worth 0.6.

- 2026-08-10 PARITY AUDIT RAN. MY PREDICTION FAILED, AND THE AUDIT CANNOT
  ANSWER THE QUESTION IT WAS BUILT FOR. Both of those need saying.
  THE PREDICTION, AS WRITTEN, IS REFUTED. I said collapse fills should win
  MORE than clean ones, and that if they won less my account of the old
  mechanism was wrong. Measured:
      collapse   16 takes  2,027 sh   38% won  paid 0.457 saw 0.607  -$258.70
      clean     169 takes 18,170 sh   86% won  paid 0.859 saw 0.829  -$179.30
  -48 points. When the book collapsed, the book was right and our fv>=0.995
  was wrong. On this evidence the collapse path is informed flow and the
  cheap fills are adverse selection, not a feed edge.
  BUT THE COVERAGE MAKES IT A DIFFERENT TEST THAN THE ONE I SPECIFIED, and
  this is a limitation, not a defence. The depth tap was added 2026-07-31
  (commit 27c6600). The 185 joined takes carry -$438 of a +$3,199 record.
  The snipe unit alone is +$2,225.52 over 181 takes while its 108 joined
  takes carry -$76.36 — so essentially the entire record predates the
  telemetry and this audit is silent on it. The prediction was about
  PRE-CUTOVER behaviour; the covered window is mostly at or after the point
  where the record stopped being made. Tool now splits at 08-07 so the
  07-31 -> 08-06 slice, which IS covered and IS pre-change, can answer it
  properly. Until that prints, treat the mechanism account as UNDER
  CHALLENGE rather than either confirmed or retracted.
  A DEFECT IN MY OWN TOOL, FOUND BY ITS OUTPUT. AT SIGNAL came out BETTER
  than RECORDED (-$226.84 vs -$438.00), which made no sense for a
  pessimistic counterfactual. Cause: it repriced every fill at the BEST
  pre-latency ask, silently assuming the whole clip filled at the touch.
  Clean fills paid 0.859 having seen 0.829 — the sweep walks UP the ladder —
  so my "worst case" was cheaper than reality for 169 of 185 takes. It now
  sweeps the observed 5-level ladder for the actual share count. The
  -$226.84 figure is void.
  WHAT STANDS REGARDLESS. (1) The collapse path lost $258.70 on 16 takes and
  wins 38%; snipe_price_floor=0.0 is what lets it through. (2) The
  0.95-1.01 bucket lost $415.82 on 81 takes — the single worst line in the
  covered period, and the same region the loss audit found earned nothing
  historically. (3) A "when the money was made" table is now printed first,
  because the striking fact here is not fill fidelity — it is that the
  record appears to have stopped being made BEFORE the rule change, which
  would mean 08-07 is not the explanation for the drought.

- 2026-08-10 THE RECORD IS FOUR DAYS. This is the most important number
  produced this week and it reframes the whole month.
  Whole fleet, 291 settled takes over 19 trading days, +$3,199.13:
      07-31   30 takes  +1,604.83
      07-29   21 takes    +928.89
      07-30    9 takes    +511.73
      07-23    8 takes    +478.60
      -------------------------------------------------------------
      68 takes (23% of all takes)  +3,524.05  =  110% of the record
      the other 223 takes, 15 days:  -$324.92
  One day, 07-31, is half of it. Peak equity +$4,044.26 on 08-01; the fleet
  is -$845.13 since.
  AND THE SLIDE STARTS 08-02, FIVE DAYS BEFORE THE RULE CHANGE:
      08-02 .. 08-06 (pre-change)   -$197.46 over 5 days
      08-07 .. 08-09 (post-change)  -$647.67 over 3 days
  The post-change days are worse per day, but a large part of that is our
  own doing — 08-07 ran the un-migrated oracle against the new rule and
  08-09 includes the $85.89 spot-vs-TWAP unit mismatch. Strip our bugs and
  the venue is not the story. The strategy stopped making money BEFORE
  Polymarket changed anything.
  I HAVE SPENT THIS WEEK EXPLAINING THE WRONG DISCONTINUITY. Every study
  since 08-07 was built on "it worked, then the rule changed, so find what
  the rule change took away". The ledger says it stopped working on 08-02.
  Worse, the thing being restored was 68 takes on four days. The earlier
  audit already said this in another form — top 20 of 283 windows > 100% of
  profit, day-block bootstrap 5th percentile $35/day — and I did not let it
  change the goal. Four days is not evidence of an edge. It is consistent
  with one, and equally consistent with a good week.
  PARITY, RESOLVED FOR THE COVERED WINDOW AND IMMATERIAL THERE. With the
  ladder-sweep fix, RECORDED -$438.00 vs AT SIGNAL -$480.44 on 185 takes:
  live lies in a $42 band. The collapse path is real but small. The
  "paper buys wreckage" concern is worth $42 here, not the record. It still
  says nothing about the +$3,637 earned before the telemetry existed.
  MY PREDICTION IS REFUTED ON ITS OWN GROUND. Split at the cutover, the
  PRE-CHANGE slice — covered, and exactly the period the claim was about:
      pre-change   collapse 15 takes 40% won | clean 159 takes 89% won
  I predicted collapse fills would win MORE. They win 49 points less, on
  the pre-change data. The DERIVATION was wrong: a 5c+ collapse in half a
  second is a large real move both feeds see, not the sub-basis-point
  disagreement the mechanism is about, so this was never the right test of
  it. But I wrote the prediction, it failed, and the escape hatch is only
  worth as much as the direct test — rescoring the old trades against the
  Binance close, which flips +$2,536 to -$2,082 and still supports the
  mechanism. One direct test for, one derived test against.
  WHAT THIS CHANGES. Stop treating +$2,225 as a benchmark to restore; it is
  four days. The open-tilt (+0.6 points against a 1.75-point fee) and AHL
  momentum (+0.12) both died at the fee wall, and that wall is the real
  constant here. The only remaining candidate whose edge could be
  structurally large rather than statistically real is the Kalshi
  KXBTC15M / Polymarket 15m pair: same rule, same clock, two resolvers.

- 2026-08-10 KALSHI PAIR: THE SETUP IS REAL, THE EDGE IS NOT DEMONSTRATED,
  AND 1-MINUTE DATA CANNOT SETTLE IT.
  CONFIRMED STRUCTURE. Kalshi KXBTC15M and Polymarket btc-updown-15m ask
  the identical question — 60s average at close >= 60s average at open — on
  the identical :00/:15/:30/:45 clock, settled on CF Benchmarks BRTI and
  Chainlink respectively.
  OUTCOME AGREEMENT, the number the whole idea rests on:
      PRE-change  (different rules) 1308 windows  93.6% agree
      POST-change (SAME rule)        291 windows  96.9% agree, 6/3 split
  The rule alignment itself lifted agreement 93.6% -> 96.9%, which confirms
  the rules really did converge. The 9 disagreements are symmetric within
  noise, so a paired position pays exactly 1.00 in 97% of windows and the
  mismatches cancel in expectation rather than bleed.
  LIQUIDITY IS NOT THE PROBLEM. 800-1900 contracts quoted, 100k-400k
  contracts traded per MINUTE. The volume=None in the markets listing is an
  unpopulated field, not an empty market.
  PRICED OFF HISTORY, 291 windows, both legs, both venues' fees at
  0.07p(1-p), profit averaged within window then across windows so the unit
  of independence is the WINDOW and not the quote:
      candle-close pairing         1700 opps  +1.54c/pair  t=1.90  [-0.05,+3.14]
      worst quote inside the minute 401 opps  +0.73c/pair  t=0.55  [-1.87,+3.33]
  Neither significant. The conservative pass discards 76% of the
  opportunities as timing artifacts, which is the finding: inside a single
  minute Kalshi's ask moved 0.52 -> 0.31, so pairing a quote from one venue
  with one from the other up to 60s away cannot distinguish a real
  cross-venue gap from two prices sampled at different moments.
  ALSO NOTE the raw payoff distribution was 64 zeros against 7 twos while
  the outcome disagreements were 6 and 3. The cheap packages cluster in the
  windows that break — the same adverse selection found everywhere else
  this week. Any live version needs a rule that refuses the cheapest
  packages, not one that seeks them.
  NOT DEAD, NOT PROVEN, AND UNANSWERABLE FROM PUBLIC HISTORY. Neither venue
  publishes sub-minute books retrospectively and it cannot be back-filled.
  bot/pair_record.py samples both books at the same instant every 20s and
  stores the round-trip time so the simultaneity of each sample is auditable
  after the fact. Read-only, no keys, no ledger. This is the only remaining
  candidate whose edge could be structurally large rather than
  statistically real, and it is the same shape as the mechanism that
  actually paid. Record first, decide later.

- 2026-08-10 THE 5m BOOK RECORDERS WERE DEAD FOR TWO HOURS. MY BUG.
  polybot-bookrec and polybot-bookrec-eth-5m: FAILED. Traceback:
      sqlite3.OperationalError: table book has 8 columns but 9 values supplied
  When the audit added the `err` column on 08-09 I changed the CREATE TABLE
  and the INSERT together. CREATE TABLE IF NOT EXISTS is a NO-OP on a
  database that already exists, so the two 5m recorders — whose tables
  predate the change — kept their 8-column shape and threw on every single
  write. They died at 23:02, systemd burned its 20 restarts in 60 seconds,
  hit StartLimitBurst and stopped trying. Nothing alerted. The btc 15m unit
  survived only because its database was created after the change.
  COST: leads 298/285/270 (T+2/T+15/T+30) have ZERO rows. The open — the
  measurement the recorders were changed to collect, and the one I have
  been telling the owner to wait for — was never recorded at all.
  FIXED: db_open() now ALTERs in a missing `err` column, and the writer uses
  named columns instead of positional so the next schema change cannot
  couple to the column count. Verified against a synthetic 8-column table:
  migrates, preserves the old rows, accepts a 9-value insert.
  TWO PROCESS FAILURES WORTH MORE THAN THE BUG. (1) A schema change shipped
  to running recorders with no migration and no post-restart check — I ran
  `systemctl restart` and never looked at whether rows were still arriving.
  (2) price_curve's default LEADS never included 298/285/270, so the first
  run after the change reported on the old leads and looked normal. Two
  independent reasons the same silence went unnoticed for two hours. Any
  future recorder change: restart, wait one window, count rows.

- 2026-08-10 WHAT THE OPEN LEADS SAY WITH THE DATA THAT DID SURVIVE.
  Leads 240 and 180 (T+60, T+120) were added in an earlier service edit and
  collected 30 windows before the crash:
      lead 298 (T+2)    signal 50.5% [41,60]   no book data
      lead 285 (T+15)   signal 61.9% [52,71]   no book data
      lead 270 (T+30)   signal 63.5% [54,72]   no book data
      lead 240 (T+60)   signal 62.9%  quoted 100%  ask 0.672  needs 68.7%  -5.86c
      lead 180 (T+120)  signal 68.8%  quoted 100%  ask 0.782  needs 79.4% -10.64c
  Where the open IS priced, it is priced AGAINST us — the book charges 6 to
  11 points more than our accuracy justifies, and it quotes 100% of the
  time, so there is no drought to blame. This is consistent with the 21-day
  proxy result (+0.6 points of edge, inside the spread) and points the same
  way. T+15 and T+30 remain the only unmeasured leads, and given T+60 is
  -5.9c and T+120 is -10.6c, the prior on them should be low.

- 2026-08-10 bot/pair_report.py added — the reader for the two-venue
  recorder, which had been collecting with nothing able to read it.
  Reports in two parts. First the GAP, which needs no outcomes and is
  meaningful immediately: the cheapest package per sample with both venues'
  fees, how often it prices under 1.00, and the size at the binding leg.
  Second the REALISED profit, averaged within window before across windows,
  with the payoff mix printed so the adverse-selection pattern from the
  history run (64 zeros vs 7 twos against symmetric 3.1% feed disagreement)
  is visible rather than buried in a mean.
  MAX_DT drops samples whose two legs were further apart than the given
  number of seconds; the recorder stores each sample's round-trip so
  simultaneity is measured, not assumed. This is the whole reason the
  recorder exists — the candle version could only pair quotes up to 60s
  apart, and 76% of its opportunities disappeared once that was handled
  honestly.
  A SIZING ERROR CAUGHT IN THE SMOKE TEST: the $/day line first counted
  every SAMPLE as a tradeable position, so 40 observations of one window
  read as 40 bets and produced a $10k/day figure from synthetic data. It
  now sizes at one package per WINDOW. The same mistake in the significance
  test is what the window-level averaging already guards against.

- 2026-08-10 PAIR RECORDER LOGGED 1 SAMPLE IN 9.5 HOURS. MY BUG, AGAIN, AND
  THE SAME SHAPE AS THE LAST ONE. k_ticker() queried Kalshi with
  status=open. Kalshi markets sit as `initialized` and only flip to `open`
  partway into their window, so at the window boundary — which is exactly
  when the recorder looks — the lookup returned nothing. That None was then
  cached for the whole window, so one failed lookup cost 45 samples.
  FIXED: no status filter (verified live — resolves the current and next two
  windows), and a failed lookup is now RETRIED every 20s inside the window
  instead of writing the window off. Same class of fault as the book
  recorder yesterday: a value fetched once at a boundary, cached, and never
  re-checked. Both are now retry-on-failure.
  THE PROCESS LESSON, TWICE IN TWO DAYS: a recorder that is `active` is not
  a recorder that is recording. systemctl said active for 9.5 hours while
  the table gained one row. Every recorder change from here gets a row-count
  check one window later, not a status check.

- 2026-08-10 THE BOOK RECORDERS ARE BACK AND THE OPEN IS FINALLY CAPTURED.
  After the schema migration both 5m units are writing. Leads 298/285/270 —
  T+2, T+15, T+30 — hold 228-230 rows each, roughly 114 windows, where they
  had zero. That is the measurement this whole line of work has been waiting
  on and it could not be back-filled. bot/price_curve.py now has real book
  data at the open for the first time.

- 2026-08-10 bot/health.py — THE CHECK THAT SHOULD HAVE EXISTED BEFORE
  EITHER RECORDER BUG. Owner's complaint is correct: twice in two days I
  shipped a broken recorder, told him to run it and wait hours, and the
  breakage was only found when a downstream report looked thin. The waiting
  was wasted both times because the damage was already done before the
  clock started.
  systemctl cannot catch this. Both dead recorders reported `active` — one
  was throwing on every insert, the other had cached a failed lookup. A
  process can be perfectly alive and writing nothing. The only honest test
  is whether ROWS ARE ARRIVING.
  health.py checks every recorder and ledger for row count, age of the last
  write, and rows in the last hour against a rough expected rate, and exits
  non-zero if anything is wrong. Verified against fixtures reproducing BOTH
  real failures: a recorder silent for 3h reads DEAD, and one writing 4 rows
  where 150/h is expected reads THIN — which is exactly the shape the pair
  recorder made while logging one row in 9.5 hours.
  STANDING RULE, now enforceable in one second rather than by waiting:
  after ANY recorder or schema change, run bot.health. Before reporting a
  result from any table, run bot.health. `active` is not evidence.
  AND A RULE FOR ME: verify the code path against the live venues HERE
  before handing over a command. The pair recorder's sampling path was run
  end to end this way afterwards — three real samples, both books, sane
  prices (pm_up 0.79 vs kalshi yes 0.77-0.78, package 1.014 after fees, no
  gap at that instant). That check took one minute and would have caught
  the bug before it cost nine hours.

- 2026-08-10 THE OPEN IS MEASURED. 227 windows, 21.1h, real book data at
  T+2/T+15/T+30 for the first time. Quoted 100% at every open lead, which
  matters methodologically: accuracy and price come from the SAME rows, so
  unlike the late leads this EV is NOT an upper bound.
      lead    n     hit     ask   needs      EV     EV lo
      T+2   177   57.6%   0.563   58.0%   -0.42c   -7.76c
      T+15  177   63.8%   0.602   61.9%   +1.92c   -5.34c
      T+30  176   65.3%   0.624   64.0%   +1.26c   -5.99c
      T+60  176   67.6%   0.687   70.2%   -2.61c   -9.82c
      T+120 176   69.3%   0.766   77.9%   -8.55c  -15.70c
  I PREDICTED T+15 AND T+30 WOULD BE NEGATIVE. They are mildly positive.
  Recording that because the prediction was written down; the direction was
  wrong even though the magnitude is inside the noise.
  THE SHAPE IS CONSISTENT AND WORTH KEEPING: the book underprices the tilt
  in the first half-minute and OVERPRICES it from T+60 on. By T+120 it is
  charging 77.9% for a 69.3% event. Whatever edge exists lives in the first
  30 seconds and is worth 1-2 points.
  THE 0.50-0.70 BUCKET LOOKS BETTER AND IS NOT. Pooling that bucket across
  the four open leads gives 145/220 = 65.9% at 0.579, +6.30c/share with a
  lower bound of -0.19c. But those 220 rows are four leads over ~55 windows
  — the same windows counted four times. At an effective n of 55 the lower
  bound is -7.36c. Nothing established.
  VERDICT UNCHANGED and now measured rather than assumed: the opening tilt
  is real, it is worth 1-2 points, and the fee plus spread is 1.75. The
  21-day proxy said +0.6 points; the real book at the real leads says +1.3
  to +1.9. Same answer, better data. Not wiring it.

- 2026-08-10 health.py EARNED ITS KEEP ON THE FIRST RUN — and had a bug.
  CAUGHT: twapcal/sol_1s.db dead for 18.9 hours. Nothing else was watching
  the SOL grid and nothing downstream had complained yet. That is exactly
  the class of silent loss that cost two days this week.
  CONFIRMED: the pair recorder is writing again after the ticker fix (29
  rows in the last hour where it had managed one in 9.5 hours), and all
  three book recorders are at their expected ~300-360 rows/h.
  MY BUG IN IT: an empty bot ledger (snipe-doge, never traded) read DEAD.
  Zero rows is broken for a RECORDER and normal for a ledger with no fills.
  Fixed — only sources carrying a rate expectation can be dead from
  emptiness.

- 2026-08-10 THE SOL GRID HAD NO SERVICE AT ALL. health.py flagged
  twapcal/sol_1s.db dead 19h; `systemctl restart polybot-twaprec-sol`
  matched nothing because that unit was never written — only btc and eth
  twaprec units exist. The SOL grid was recorded by hand at some point and
  stopped when whatever ran it stopped. Added the unit.
  IMPACT IS RESEARCH-ONLY: the twapcal grids feed twap_verify, open_offset,
  price_curve and friends. The bots' oracle subscribes to the live feed and
  never reads these files, so no trading was affected. Worth having anyway
  — every replication test this week has leaned on btc and eth, which
  correlate 0.809, and a third coin is the cheapest way to stop pooling two
  near-identical samples and calling it independent.
  PAIR RECORDER IS WRITING BUT UNDER-SAMPLING: 59 rows/h where EVERY=20s
  should give 180. That is roughly one sample per minute, i.e. each cycle is
  taking ~60s rather than ~3s. The likely cause is fetch timeouts — get()
  uses tries=2 with a 12s timeout, so one bad leg costs 24s and three legs
  can cost 72s — on a 512MB box that is already at 54% memory and 25% swap.
  bot/pair_report prints fetch failures and the leg-separation distribution,
  which distinguishes "slow but simultaneous" from "slow and skewed". The
  second is fatal to the measurement; the first only costs sample count.
  health.py's expected rate for the pair now derives from EVERY rather than
  a hardcoded 150, so THIN means a precise thing.

- 2026-08-10 THE PAIR, MEASURED SIMULTANEOUSLY AT LAST — AND IT IS SMALL.
  The recorder's whole reason for existing is settled: leg separation is
  0.24s median / 0.27s p90 on the droplet (1.05s on my sandbox), against
  the 60s the candle data forced. Zero fetch failures. The timing ambiguity
  that made the historical pair analysis meaningless is gone.
  POOLED, 98 samples over 6 windows (droplet 4, mine 2):
      gap present in 28% of samples (droplet 19/77, mine 8/21)
      mean gap when present  1.72c per pair
      binding-leg size       ~104 contracts median, p10 = 0
      median cost with no gap 1.0158 — normally you pay 1.6c ABOVE par
  ECONOMICS AT FACE VALUE: 1.72c x 104 = $1.78 per tradeable window;
  ~26 of 96 daily windows qualify; **$47/day GROSS**.
  NOT IN THAT NUMBER: legging risk (two venues, two order sends, and a
  half-filled pair is a naked directional position, not an arbitrage), the
  3.1% feed-disagreement variance, capital parked on both venues, and the
  p10 binding size of ZERO meaning a tenth of the "opportunities" have no
  size at all.
  CONTEXT FOR THE DECISION: the whole historical record was +$3,199, and
  four days of it made 110%. $47/day gross from a two-venue build with KYC,
  USD on Kalshi, crypto on Polymarket and cross-venue execution risk is not
  obviously worth building, and 6 windows is not a sample.
  PRE-REGISTERED DECISION RULE, written before the data arrives so it
  cannot be rationalised afterwards: after ONE full day (~96 windows), if
  the gross is under $100/day, the two-venue build is not worth it and this
  line closes. If it is over $100/day AND the realised section (which prices
  the feed-disagreement risk rather than assuming it away) is positive at
  its lower bound, it graduates to an execution-feasibility study. Anything
  in between stays a recorder.

- 2026-08-10 THE PRE-OPEN ENTRY. Owner proposed buying a side seconds BEFORE
  the window opens and resting a +5c limit. Tested, and the enabling facts
  are measured rather than assumed.
  1. THE SIDE IS CALLABLE BEFORE THE OPEN. The strike is the 30s mean ending
     at T, so at T-3 we know 27 of its 30 seconds and extrapolate the rest
     from the last print. Sign agreement with the true T+0 tilt, 6,047
     windows per coin:
         lead   btc all   btc |tilt|>=1bp   eth |tilt|>=1bp
         T-3s    86.9%        98.86%            97.42%
         T-5s    81.8%        95.37%            92.94%
         T-10s   73.6%        82.56%            79.70%
         T-20s   62.3%        64.17%            62.07%
     T-3 is the right lead and T-10 is already too late. This vindicates the
     3-second figure in the proposal exactly.
  2. THE PRE-OPEN BOOK IS FLAT AND DEEP. Sampled live 176s before an open:
         up   ask 0.51 x 599   bid 0.50
         down ask 0.50 x 146   bid 0.49
     Symmetric, ~0.50 both sides, hundreds of shares. It carries no
     information because the strike does not exist yet.
  3. THE SNAP IS ALREADY MEASURED. The tilt side's ask is 0.563 at T+2 and
     0.602 at T+15 (177 windows). So the book moves ~5.8c in two seconds and
     a +5c limit rests INSIDE that move.
  THE ARITHMETIC, from measured inputs only, entry 0.505, taker fee
  0.07p(1-p) in and maker fee ZERO out:
      ALL windows   T-3 side wins 55.6%  -> hold +3.36c/sh  [lo -4.24c, n=177]
      |tilt|>=1bp   T-3 side wins 60.4%  -> hold +8.12c/sh  [lo +5.68c, n=1577]
      exit at +5c instead of holding: +3.25c/sh whenever it fills
  A POSITIVE LOWER BOUND ON A LARGE SAMPLE. First one on this project.
  WHY IT WAS MISSED FOR A WEEK: every recorder here starts at T+2. The
  0.505 -> 0.563 repricing happens in the first two seconds and no
  instrument ever sampled before it. The edge is not a better forecast —
  it is the SAME forecast, bought three seconds earlier at a flat book.
  Every negative result this week (open tilt +0.6 points, AHL +0.12 points)
  priced entry at 0.563 because that is where the data started.
  NOT YET MEASURED, and any of the three can still kill it:
   (a) whether the pre-open book LEANS toward the tilt side. One sample
       showed a 1c lean (up 0.51 / down 0.50). If that lean tracks the tilt
       the edge shrinks by it, and if it tracks it fully the edge is gone.
   (b) whether the book moves between T-20 and T-3.
   (c) whether a +5c limit actually FILLS or the snap jumps past it.
  Recorders now sample PREOPEN=20,10,5,3 on the NEXT window, stored as
  lead = WINDOW + n (so a 5m T-3 is lead 303), which price_curve already
  reads as spot(C-L) with no change. Collection starts immediately because
  an unsampled window is lost forever.
  NOTE A KNOWN LOOKAHEAD TO FIX BEFORE TRUSTING price_curve ON THESE LEADS:
  at T-3 only 27 of the strike's 30 seconds exist, but price_curve builds
  the strike from all 30. That overstates the pre-open signal slightly. The
  sign-agreement table above is the honest version and is what the
  arithmetic uses; price_curve needs a truncated strike for leads > WINDOW.

- 2026-08-10 PRE-OPEN, FIRST WINDOW EVER SAMPLED — AND IT FOUND THE REAL
  CONSTRAINT. My scratch sampler died after one window (third recorder lost
  this week; the droplet's is the one that matters and it IS collecting —
  leads 303/305/310/320 confirmed writing). That single window:
      T-20   up 0.50 x160    dn 0.51 x55
      T-10   up 0.52 x  5    dn 0.49 x323
      T-5    up 0.52 x  5    dn 0.49 x20
      T-3    up 0.53 x 14    dn 0.49 x16
      T+2    up 0.52 x182    dn 0.49 x199
      T+5    up 0.67 x227    dn 0.34 x108      <- the snap
      T+15   up 0.70 x140    dn 0.30 x455
  THREE THINGS IN ONE WINDOW:
  (1) THE SNAP IS REAL AND BIG — 0.52 -> 0.67 -> 0.70 between T+2 and T+15.
      A +5c limit sits well inside that.
  (2) THE SNAP IS NOT AT T+2, IT IS AT T+5. At T+2 the book was still 0.52.
      The "0.563 average at T+2" from price_curve mixes windows that had
      snapped with windows that had not.
  (3) THE LIQUIDITY VANISHES EXACTLY WHEN THE SIGNAL SHARPENS. 160 shares
      at 0.50 at T-20; FOURTEEN at 0.53 by T-3. Makers pull into the open.
      This is the binding constraint and I had not considered it:
          T-20  size ~160  sign accuracy 64%
          T-3   size ~14   sign accuracy 99%
      The strategy's value is edge x size, and those move in opposite
      directions across the entry lead. n=1, so this is a hypothesis about
      shape, not a measurement.
  bot/preopen_report.py reads the droplet's pre-open rows and reports
  exactly the four quantities that decide it: LEAN (does the book already
  charge more for our side), SIZE (median shares at the touch), SNAP (T+2
  and T+15 prices), WIN (settlement rate and hold EV with a lower bound).
  NO LOOKAHEAD: at T-n it builds the strike from only the 30-n seconds that
  exist, imputing the tail from the last print — exactly what a live bot
  would have. price_curve uses all 30 and therefore overstates the pre-open
  signal, which is why this is a separate tool rather than another lead in
  that table. Verified against a fixture with a known tilt and a
  deliberately flat book: reports lean +0.00c and recovers the right side.

- 2026-08-10 RETRACTED WITHIN THE HOUR: "LIQUIDITY VANISHES INTO THE OPEN"
  IS FALSE. Owner challenged the 14-share figure. He was right to. top()
  records only the BEST level, and I read one window's touch size as the
  tradeable size. The full ask ladder, sampled live across a real open:
      T-30  up  0.51x206 0.52x315 0.53x560 0.54x526 0.55x998  -> 2605 <=0.55
      T-20  up  0.51x168 0.52x329 0.53x580 0.54x536 0.55x1008 -> 2621
      T-10  up  0.52x193 0.53x445 0.54x352 0.55x714           -> 1703
      T-5   up  0.52x194 0.53x449 0.54x324 0.55x692           -> 1658
      T-3   up  0.52x425 0.53x449 0.54x324 0.55x692           -> 1889
      T-1   up  0.52x415 0.53x439 0.54x324 0.55x692           -> 1869
  Depth barely moves from T-30 to T-1, on BOTH sides (down held 3642 ->
  2586). MAKERS DO NOT PULL. There are ~1,900-3,300 shares inside five
  cents at the entry moment, not fourteen. The edge-versus-size tradeoff I
  described does not exist, and T-3 — the lead with 99% sign accuracy — is
  also fully liquid. That is the strategy's best case, not its constraint.
  THE INSTRUMENT CAUSED THE ERROR, so the instrument is fixed: book_record
  now stores ask_cum, the size at or below best_ask + SWEEP (default 5c),
  which is what a marketable order actually takes. Migration verified on
  the live 9-column shape — adds the column, keeps old rows, accepts the
  new insert. price_curve gains load_book_depth() and preopen_report uses
  it, so SIZE now means executable depth everywhere.
  ALSO CONFIRMED, and it corrects an earlier claim of mine: the snap is at
  T+5, not T+2. That window sat at 0.51/0.50 at T+2 and moved to 0.47/0.55
  by T+5 and 0.37/0.64 by T+10 — an 18c move, so a +5c limit rests well
  inside it.
  AND A CLEAN NEGATIVE WORTH KEEPING: that window's tilt at T-3 was
  +0.07bp — the signal said UP, DOWN won. But 0.07bp is noise by
  construction; TILT_MIN=1 refuses it. It is a demonstration of why the
  gate exists, not evidence against the signal.

- 2026-08-10 PRE-OPEN PAPER BOT BUILT (bot/strategies/preopen.py), OFF BY
  DEFAULT. At T-3 it computes the tilt from the oracle grid using ONLY the
  seconds that exist at that instant, gates on |tilt| >= 1bp, and takes the
  tilt side if the book has not already leaned past preopen_max_px.
  MATH VERIFIED BEFORE DEPLOYMENT, not after:
    - NO LOOKAHEAD: with the unseen tail flat at 100 and with it spiked to
      500, the strike is IDENTICAL (100.0000). A peeker would have used
      126.6667. The unseen seconds cannot reach the calculation.
    - SIGN: spot 100.5 over a 100.0 mean picks UP; 99.5 picks DOWN.
    - REFUSAL: 25% grid coverage returns None rather than guessing.
    price_at was checked directly and only searches BACKWARD, so the
    tolerance window cannot pull a future print either.
  WHY BUY-AND-HOLD RATHER THAN THE +5c RESTING SELL. Holding is worth more
  (+8.12c vs +3.25c after the entry fee), and a resting SELL would need
  fill machinery PaperExecutor does not have — machinery whose first
  version would be untested on exactly the path that decides the result.
  So it holds, and writes a preopen_mark event at T+15 with the book and
  whether a +5c limit would have been reachable, which prices the exit
  variant later from data instead of from a fill model.
  SEPARATE LEDGER (bot/data/preopen-btc) with SNIPE_ENABLED=0 and
  TOLL_ENABLED=0, so this unit's PnL, risk breakers and fills cannot be
  confused with the snipe's.
  WHAT THE LEDGER SETTLES THAT NO RECORDER CAN: the price we actually pay
  at T-3 (the LEAN risk), whether the order lands in time at all, and a
  real settled PnL. The recorders answer the first only in aggregate and
  the second not at all.

- 2026-08-10 PRE-OPEN BOT IS LIVE (paper) AND WAS INVISIBLE. It started
  clean — feeds up, 4-5 markets discovered including the NEXT window, ws on
  8-10 tokens, oracle 0.1-1.1s stale, no mismatches — but the STATUS line
  said nothing about it. The `pre[on=0 placed=0 ...]` field in that line is
  the SNIPE's pre-positioning counter, not this strategy. And a skipped
  window logged nothing at all, so "no fills yet" and "completely broken"
  looked identical. That is exactly the failure mode that cost two
  recorders this week, so it is fixed before the data matters:
    - STATUS now carries preopen[evals= in= skip= why={...}]
    - every skip logs its reason and the numbers behind it, e.g.
      "w… preopen skip: flat_tilt (+0.31bp < 1.0bp)" or
      "w… preopen skip: book_leans (up ask 0.610 > 0.56, tilt +1.8bp)"
  CHECKED AND FINE: risk.halted("preopen") returns False for an unscoped
  strategy name, so the risk gate is not silently blocking it.
  NO FILLS AFTER ~6 MINUTES IS EXPECTED, NOT A FAULT. The 1bp gate passes
  roughly a quarter of windows (1,577 of 6,047 measured), so one or two
  windows with nothing is the common case. The `why` histogram is what
  distinguishes "correctly waiting" from "never firing" — if evals climbs
  and flat_tilt dominates, it is working as designed.

- 2026-08-10 THE GATE WAS COSTING MONEY. I set PREOPEN_TILT_MIN_BP=1.0 by
  guess. Measured DIRECTLY on the T-3 signal (truncated strike, no
  lookahead), 6,078 btc windows over 21 days, entry 0.505, break-even
  52.25%:
      gate     trades/day   win%    edge     $/day    lower-bound floor
      0.25bp      147      58.3%   6.03c    2,213         1,573
      0.50bp      107      59.1%   6.87c    1,843         1,296
      0.75bp       80      60.8%   8.59c    1,727         1,256
      1.00bp       60      60.2%   7.94c    1,186           779
  The 1bp gate threw away nearly half the trades and $500/day of floor.
  Moved to 0.5bp. NOT to 0.25bp, even though it scores higher: the proxy
  calibration only verified the sign down to 1bp, so below ~0.5 the
  measurement rests on a tilt the calibration does not cover. Taking the
  measured optimum inside the trusted range rather than the global one.
  ETH WANTS A DIFFERENT GATE, AND FOR A MECHANICAL REASON. Its 0.25-1.0bp
  buckets sit at 52-54% against the same 52.25% break-even — nothing — and
  it only separates above 1bp (1.0-1.5 57.1%, 1.5-2.0 59.6%, 2.0-3.0
  61.1%). Cause: eth's 1s vol is 0.185 bp/s against btc's 0.108, so a basis
  point of tilt is worth less there. Per-coin gates, not one number.
      eth at 1.0bp: 85 trades/day, 58.2%, $1,262/day, $773 floor
  eth deployed as its own unit with its own ledger.
  NOTE THE BUCKET DETAIL IS NOISY (btc 0.50-0.75 wins 54.0% while 0.75-1.00
  wins 62.7%). The fine structure is sampling noise; only the CUMULATIVE
  gate rows are stable, which is why the gate is chosen from those.

- 2026-08-10 SCALP vs HOLD, SETTLED ON THE REAL TAPE (121 btc / 75 eth / 43
  sol windows, post-rule-change, Polymarket data-api trades joined to the
  Chainlink grid). The user's specified strategy — buy at T-3, rest a limit
  sell at +5c — was measured properly for the first time and it does NOT beat
  holding to settlement.

    btc  HOLD        66.9% settle, EV +13.28c/share, 95% floor +4.53c
         SCALP 7c@T+5   fills 64%,  EV  +9.47c/share, 95% floor +3.86c

  WHY THE HOLD WINS, and it is structural rather than a sample artifact: an
  exit at +Xc CAPS the upside at X while a loser still costs the full entry.
  Holding collects the whole 1.00 on a winner. At a 67% win rate that cap is
  expensive. The tell is in the output — the windows that have NOT jumped by
  T+5 still settle 70%, so the scalp is selling winners cheap. The scalp is
  the right instrument only where the settlement edge is weak; here it is not.

  THE JUMP ITSELF IS REAL AND IT SCALES. Wrong-way rate (our side never
  printing above what we paid) is 1/121 = 0.8% within T+15 on btc. Median peak
  within T+15, by tilt bucket: 0.5-1bp -> +11.15c, 1-2bp -> +16.64c,
  2-4bp -> +18.00c. So the tilt predicts the SIZE of the repricing and not
  only its direction, and a fixed +5c exit captures roughly a third of the
  move it could have.

  THE 0.5bp GATE I WIDENED TO ON 08-10 IS NOT SUPPORTED BY THIS. Break-even
  at the observed 0.5187 entry is 53.62%.
    0.5-1bp  n=72  settles 62%  CI [51.0, 72.8]  floor -2.66c  does NOT clear
    1-2bp    n=40  settles 72%  CI [57.2, 83.9]  floor +3.55c  CLEARS
  The earlier measurement that motivated widening came from price_curve, which
  never joined settled outcomes to pre-open TRANSACTED prices; this one does.
  NOT reverted, deliberately: it is paper, the marginal bucket is positive in
  expectation (+8.38c) and merely unproven, and the wider gate is what
  generates the sample that will settle it. This must be decided before any
  real money.

  SOL IS DEAD. 51.2% settle (floor 36.8%), and 51% of windows had not printed
  above entry even at T+5 — the jump is both weaker and slower. Do not deploy.
  ETH IS NOT ESTABLISHED: 64.0% settle but the floor is -0.05c, i.e. exactly
  break-even. Keep collecting; do not size up.

  METHOD NOTE — three measurement errors preceded this result and all had the
  same shape, an easy number standing in for the hard one. (1) exit_curve read
  the book at three instants, but a resting sell fills on a TOUCH, so its fill
  rates were lower bounds by an unknown margin; the user caught this. (2) The
  first tape run collected prints with no upper time bound, so "peak" measured
  the contract converging to 0.98 by the close rather than the opening jump —
  it read +45c above a 0.51 entry. (3) The live tracker seeded its peak at the
  entry price and only ratcheted up, so a jump the WRONG way was invisible.
  Every number above is horizon-bound and every one of those is fixed.

- 2026-08-10 LIMIT-UNTIL-RESOLUTION, AUDITED (123 btc / 77 eth / 45 sol
  windows, real tape, post-rule-change). The user's spec — rest a sell at
  entry+X and LEAVE IT until the window resolves — measured against holding,
  with per-window P&L, clustering-discounted bands, and an out-of-sample split.

    btc  HOLD                +11.91c/share  band [+2.09, +21.73]  n_eff 91
         LIMIT +40c to close +11.76c/share  band [+4.14, +19.38]  fills 64%
         LIMIT +25c to close  +7.27c/share  band [+1.61, +12.92]  fills 79%
         LIMIT  +5c to close  +0.27c/share  band [-2.12,  +2.66]  fills 95%

  THE STRATEGY IS SOUND BUT +5c WAS AN ORDER OF MAGNITUDE TOO SMALL. A limit
  at +X surrenders (48c - X) of upside on a winner to rescue (X + 52c) on a
  loser that happens to spike through. At X=5c that trade is terrible; it only
  becomes worthwhile as X approaches the settlement value. At +40c the limit
  matches the hold's mean to within 0.15c and has a TIGHTER band on both ends,
  because capping the win at 40c and rescuing the occasional spike-then-fail
  loser trims both tails. That is arithmetic, not a data-mined cell.

  NOTHING BEAT HOLDING OUT OF SAMPLE. Ranking 60 cells on the first half of
  the clock and scoring on the second: btc's best (+30c by T+30s) returned
  +14.77c against the hold's +13.44c, on 62 windows and an 11% fill rate —
  i.e. it IS the hold, plus a rare take-profit. Every eth and sol candidate
  lost to the hold out of sample. No change is compelled by this.

  WHAT IS NOW ESTABLISHED. btc HOLD clears zero at the 95% band even after
  discounting the sample for autocorrelation (n_eff 91 of 123): +11.91c/share,
  ~$1,727/day at a 250 clip. eth HOLD does NOT clear zero (band [-0.87,
  +23.03]) — it is unproven in either direction, not disproven. sol is dead:
  51.1% settle, band [-18.67, +14.67], 35% of its windows have no pre-open
  prints at all, and 49% had not traded above entry even at T+5.

  THE JUMP, HORIZON-CORRECTED. Median peak within T+15s scales cleanly with
  the tilt: btc 0.5-1bp -> +11.15c, 1-2bp -> +15.88c, 2-4bp -> +18.00c. Wrong-
  way rate within T+15 is 1/123 on btc, 4/77 on eth, 5/45 on sol.

  AUDIT FINDINGS ON MY OWN TOOL, in the order they would have misled. \$/day
  was 100x too large (cents never converted to dollars). n_eff could exceed
  the row count, claiming more precision than the raw sample. The exit sweep
  stopped at 15c and so never reached the range where a limit is competitive
  at all. Every window used the sample MEDIAN entry for its own fee and payoff.
  Silent drops were invisible (sol loses 35% of windows to missing pre-open
  prints — both a liquidity signal and a selection bias). No mechanical
  invariants were asserted. Fifty cells were searched with no out-of-sample
  guard. Two of these were introduced in the same rewrite that fixed the
  others, and were caught only because the tool is now smoke-tested against
  synthetic data with a known answer before it touches real data.

  STILL UNMODELLED: queue position. A print at our price is counted as a fill,
  so every LIMIT row is the optimistic bound while HOLD carries no such
  assumption — the comparison is tilted in the limit's favour and it still did
  not win. The live bid tracker is the pessimistic bound on the same question.

- 2026-08-11 VOL-SCALED GATE, RETRACTED A SECOND TIME — now on the real grid.
  Ranking windows by |tilt|/vol instead of |tilt|, priced at the actual
  pre-open entry from the tape (bot/vol_gate.py, matched trade counts so
  neither ranking gets to pick its own threshold).

  btc, top 20 of 138: scaled settles 85.0% for +28.68c/share against fixed's
  70.0% for +16.16c, and its 95% floor is +11.41c where fixed is -4.21c. On
  the point estimate it is not close. OUT OF SAMPLE IT DIES: first half
  +31.05c, second half +0.63c, losing to fixed in both splits tried. That is
  the signature of a fitted ranking, not an edge.

  THE MECHANISM DOES NOT REPRODUCE AT OUR ENTRY PRICE. vol_tilt reports a
  clean calm 66.3% > middle 60.5% > wild 56.0% ordering, but it prices at the
  FIRST QUOTE AFTER THE OPEN (avg px 0.61-0.68, break-even 66%). At the
  pre-open entry the same split is btc 69.6% / 45.7% / 63.0% and eth
  54.5% / 57.6% / 50.0% — non-monotonic on both coins, with the MIDDLE
  bucket worst. A regime effect cannot have that shape.

  AND THE COINS DISAGREE BACKWARDS. eth is the one that holds out of sample
  (+4.56c, +5.33c) — and eth is where the mechanism is absent by vol_tilt's
  own numbers: true slope +0.0303 in both regimes, book paying 139% in calm.
  btc, where the mechanism looked strongest, is where the ranking fails. A
  result that appears only where its explanation does not apply is noise.
  Every eth lower bound is negative regardless; best is -0.33c.

  This is the SECOND retraction of this idea. The first (08-09) won 8 of 8
  slices on Binance and 3 of 8 on the real feed, because both terms of the
  ratio came from the same noisy proxy. That defect is gone — both terms now
  come from the Chainlink grid the market settles on — and the idea still
  fails. Do not try a third time without a materially larger sample AND a
  monotonic regime split to justify it.

  WHAT THE SAME RUN CONFIRMED, and this is the part worth keeping: the FIXED
  gate priced at the real pre-open entry clears its floor in three cells —
  n=34 +16.03c [floor +0.29c], n=55 +16.00c [+3.81c], n=82 +12.46c [+2.08c] —
  and stays positive in BOTH out-of-sample halves (+6.46c, +7.45c). The
  configuration already running is the one supported.

  btc 15m could not be scored: only 19 windows clear a 0.6bp pool gate in the
  grid so far. Revisit when the archive is roughly three times longer.

- 2026-08-11 THE BOT WAS SILENTLY DROPPING A FIFTH OF ITS WINDOWS. Counting
  evaluations against windows elapsed over 19 hours:

      btc 5m    78% of its windows evaluated   (22% never looked at)
      eth 5m    65%                            (35% never looked at)
      btc 15m  101%
      eth 15m  104%

  (>100% is the restart re-evaluating the window in flight, not double
  trading — `done` still gates entry.)

  CAUSE — THIS ATTRIBUTION IS WRONG, SEE THE 08-11 HALT ENTRY BELOW. What I
  wrote at the time: "The run loop fired only inside a 0.6-second slot; the
  loop polls at 20Hz, so missing that slot takes twelve consecutive
  iterations of event-loop stall, and the 5m units reconcile three times as
  often as the 15m units, which is exactly the split." It is not. The real
  split was HALTED vs NEVER-HALTED, and the two units that scored 101%/104%
  are the two that never tripped the daily-loss breaker. There was no stall.
  The band was genuinely narrow and the silence was genuinely real, so the
  widening stands on its own — but it did not cause these numbers, and
  nothing downstream should cite them as evidence of event-loop pressure.

  THE WORST PART WAS NOT THE LOSS, IT WAS THE SILENCE. A dropped window
  incremented nothing — not `evals`, not `skips`, not `why`. Every diagnostic
  in this repo reads those counters, so a fifth of the sample vanished with
  no line of any kind, and it was found by counting windows against wall
  clock rather than by any alarm.

  FIX (bot/strategies/preopen.py `_when`). Fire anywhere between the target
  lead and a floor, `PREOPEN_MIN_LEAD_S=0.5`, which is the time an order
  still needs to reach the book before the open. Past the floor the window is
  refused and LOGGED as `too_late` with how late the wake was. Nothing is
  silent any more.

  WHAT A LATE FIRE COSTS, and this is why widening is safe rather than a
  compromise: a shorter lead holds MORE of the strike, not less — at T-3 the
  30s window has 27 elapsed seconds, at T-1 it has 29 — so the tilt gets
  more accurate as the lead shrinks. The measured risk runs the other way,
  through the book, and it is already bounded: depth is unchanged from T-30
  to T-1, and `preopen_max_px` refuses anything that has leaned, so a
  repricing book costs a skipped trade rather than a bad fill.

  THE FLOOR IS A PAPER FLOOR. 0.5s is fine when the executor fills instantly.
  Before real money it must be re-set from measured round-trip latency to the
  CLOB, or a late fire posts into a window that has already opened.

  AND IT MAKES THE LEAD QUESTION ANSWERABLE. Each entry now records the lead
  it ACTUALLY used (ledger `lead`, log `lead %.2fs`, STATUS
  `lead[med= worst= tgt= n=]`, and a LEAD ACTUALLY ACHIEVED block in
  tilt_parity). Every fill is now an observation of whether later is better,
  from real entries instead of book snapshots — which is what the T-3 vs T-1
  decision has been waiting on. `worst` is the SMALLEST achieved lead
  deliberately: that is the number that slides toward the floor when the box
  gets busy, and it would have shown this bug on day one.

  UNCHANGED BY THIS: target lead still 3s, all four gates, coverage floor,
  entry ceiling, sizing. One restart, tests first.

- 2026-08-11 A HALTED BOT AND A BROKEN BOT PRINTED THE SAME LINE. Ten minutes
  after deploying the firing-window fix, btc 5m showed 2 windows elapsed, 0
  evals, and — the tell — zero log lines in 25 minutes, including from the
  process BEFORE the restart. The other three were clean (eth 5m 2/2, both
  15m fired w1786449600 on the new code).

  In bot/strategies/preopen.py the halt check was a bare continue:

      if self.risk.halted("preopen"):
          await asyncio.sleep(1.0)
          continue

  No counter, no log. So a bot stopped by the daily-loss breaker printed
  `preopen[evals=0 in=0 skip=0 lead[n/a] why={}]` — character for character
  what a bot with a dead run loop prints. Same defect class as the dropped
  windows, different branch: a decision the code makes and does not record.
  It now increments `skips` and reports `why={'halted': N}`.

  AND THE LAST SILENT PATH IS CLOSED TOO. Widening the firing band does not
  cover a stall spanning the approach AND the open: `nxt` has already
  advanced by the time the loop breathes, so even too_late never sees that
  window. `done` now means ACCOUNTED FOR — entered, refused, declined — and
  `_roll()` reports any window that rolls past without entering it as
  `why={'missed': N}`. There is no longer any path through the loop that
  loses a window without saying so.

  `done` pruning moved out of the entry path into `_roll`, because a bot
  halted all day never reaches the entry path and would grow the set without
  bound on exactly the days it is already unhappy.

  THE SUSPICION THIS RAISES, NOT YET CONFIRMED. risk.py halts scope "all" on
  `pnl < -max_daily_loss`, and in paper mode that lifts only at the next UTC
  day. If the pre-open paper bots have been halting on bad days, then every
  sample quoted from their ledgers is CENSORED — bad days truncated at the
  breaker, good days recorded in full — which would bias every live win rate
  and every drawdown figure optimistically. A capital-preservation rule on a
  measurement instrument destroys the measurement. Confirm from the HALT
  events before trusting any live pre-open number, and decide separately
  whether a PAPER research bot should carry a daily breaker at all.

- 2026-08-11 THE HALT IS CONFIRMED, AND IT CENSORED THE SAMPLE. The ledgers:

      btc 5m   HALT 08-11 07:15:09  daily loss -308.68 < -250.0  (still dark)
      eth 5m   HALT 08-10 17:12:30  daily loss -367.07 < -250.0
               lifted 08-11 00:00:00                    -> 6h47m dark
      btc 15m  none  (pnl_today -142.02)
      eth 15m  none  (pnl_today 0.00)

  THIS RETRACTS THIS MORNING'S MISSED-WINDOW DIAGNOSIS. I attributed the 22%
  btc / 35% eth shortfall to an event-loop stall stepping over a 0.6s firing
  band, and cited the 5m units' reconcile cadence as the mechanism. Wrong.
  eth 5m was dark 6h47m; over a 19h measurement that is 36% against the 35%
  measured. btc 5m halted at 07:15 against a window ending near midday, about
  25% against the 22% measured. The split was HALTED vs NEVER-HALTED, and the
  two units at 101%/104% are the two that never tripped. There was no stall.
  The band was genuinely narrow and the widening is still right on its own
  merits, but nothing downstream may cite those percentages as evidence of
  event-loop pressure.

  WHAT THE HALT DID TO THE NUMBERS, and this is the part that matters. The
  breaker fires exactly when a day is going badly, so every halt TRUNCATES A
  LOSING RUN while every winning run records in full. That biases the ledgers
  in one direction:
    - win rates are UPPER bounds (the losses after the breaker are absent
      from numerator and denominator both)
    - drawdowns are LOWER bounds. btc 5m's "-$580 from peak" means it stopped
      losing because it stopped trading, not because the strategy turned. The
      real path is unobserved.
  So the +$163/56% figure for btc 5m, and everything else quoted from these
  four ledgers, is a censored statistic. The 08-10 tape backtests are NOT
  affected — they read the archive, not the ledger — which is why the tape
  numbers are the ones to trust.

  THE FIX: IN PAPER THE DAILY STOP IS NOW RECORDED, NOT ENFORCED.
  `PAPER_SHADOW_DAILY_STOP=1` (default). A trip writes one SHADOW_HALT event
  per UTC day and logs it; trading continues. An uncensored record can always
  be censored in analysis — partition on the SHADOW_HALT timestamp and the
  live-equivalent P&L is exactly reconstructible — while a censored record can
  never be repaired. A capital-preservation rule on a measurement instrument
  destroys the measurement, and in paper there is no capital to preserve.

  DELIBERATELY UNCHANGED, and tested (scripts/test_risk_shadow_stop.py, 21
  assertions):
    - mode=live still halts on the daily loss, still STICKY, and the paper
      flag cannot reach it
    - the MISMATCH halt still bites in paper. That one means our oracle
      disagrees with the exchange about who won: a correctness signal, not a
      P&L signal, and it must stop everything in every mode
    - PAPER_SHADOW_DAILY_STOP=0 restores the old enforcing behaviour exactly

  bot/halt_audit.py reports dark time and windows lost per ledger, so the
  censored intervals can be re-priced from the tape rather than guessed at.

- 2026-08-11 CORRECTION TO halt_audit's FIRST OUTPUT. Its first run reported
  1,022 dark hours and 12,270 windows never evaluated across the fleet. That
  number is wrong and was mine: the tool ran every halt with no recorded lift
  forward to the present instant, so RETIRED units — the snipe-* ledgers,
  whose services were removed in early August — reported 601h, 225h, 106h and
  77h apiece for time in which they did not exist. A halt with no lift means
  the bot never recorded coming back; it does not mean the bot is still
  sitting there halted.

  The real figure, across the two bots that exist and were censored:
      preopen-btc  5.02h   60 windows
      preopen-eth  6.79h   81 windows
      TOTAL              141 windows
  Two orders of magnitude below the first print. The conclusion is unchanged
  — those ledgers are censored and their win rates are upper bounds — but the
  scale is not, and 141 windows is a re-pricing job rather than a crisis.

  A halted bot writes nothing, so file mtime cannot tell a live-but-halted
  bot from a deleted one; only the service manager can. The tool now asks
  systemctl and reports three states: active (dark runs to now), stopped
  (dark bounded at the last ledger write), and unknown — no systemctl or an
  unrecognised unit name — which prints a RANGE rather than picking an end
  and calling it a measurement.

  WORTH KNOWING, NOT WORTH CHASING: two retired snipe ledgers carry sticky
  "oracle/exchange winner mismatch" halts, snipe-sol from 08-02 and
  snipe-btc15 from 08-08 06:50. The latter is thirty-one hours after the
  TWAP rule change, which is what a pre-migration oracle reading spot against
  a TWAP settlement looks like; scripts/clear_rule_change_mismatches.py
  exists for exactly this. The current pre-open bots are unaffected — 0
  mismatches across 214 settled windows, 135 with an oracle opinion.

- 2026-08-11 RESOLVED, ALL FOUR PRE-OPEN BOTS TRADING. btc 5m logged
  "SHADOW daily stop: loss -308.68 < -250.00" at 12:17 and evaluated its next
  window at 12:19:57 (flat_tilt -0.34bp < 0.5bp). Achieved leads across the
  fleet on the new firing window: btc 2.99s, eth 2.96s, eth 15m 2.95s,
  btc 15m 3.00s, against a 3s target — so the widened band is not being
  leaned on, it is simply no longer dropping the windows it used to.
  _mismatches: 0 on all four.

  ONE MORE halt_audit DEFECT, FOUND IN ITS OWN OUTPUT: it still showed btc as
  STILL DARK -> now, five hours and counting, while the bot was visibly
  trading. A halt cleared by a RESTART writes no HALT_LIFTED — the in-memory
  halt set is simply gone — so an unlifted halt had no end marker and grew by
  an hour every hour. A halt now ends at the first ledger row a halted bot
  could not have written (preopen_entry, preopen_mark, SHADOW_HALT, start).
  The settlement healer is deliberately NOT in that list: it writes while a
  bot is halted, and counting it would end the dark period early, which
  understates censoring — the direction that makes a bad ledger look usable.

  This is the third correction to a tool built in one afternoon to audit
  trustworthiness, so it now has scripts/test_halt_audit.py holding the
  pairing rules. Three suites gate the pre-open deploy:
      scripts/test_preopen_strike.py     73 assertions
      scripts/test_risk_shadow_stop.py   21 assertions
      scripts/test_halt_audit.py          9 assertions

  STANDING FLEET STATE at 12:23 UTC (censored, per the entry above):
      btc 5m    76 fills   +163.16   pnl_today -308.68  (shadow-tripped)
      btc 15m   32 fills   +634.79   pnl_today -142.02
      eth 5m    60 fills   -511.46   pnl_today -144.39
      eth 15m    0 fills      0.00   (1.7bp gate, nothing has cleared it)

  NEXT, AND NOT TONIGHT: re-price the 142 censored windows from the tape
  archive so the live ledgers can be compared against an uncensored estimate.
  Until that is done the btc 5m +$163/56% figure is an upper bound.

- 2026-08-11 halt_audit, FOURTH CORRECTION — `start` IS NOT PROOF OF LIFE.
  Its own output gave it away: btc 5m's outage closed at 11:50 when the
  journal plainly shows a re-halt at 11:51:15 and no trading until 12:17. A
  restart writes a `start` row whether or not the bot then resumes, and this
  bot restarted straight back into the same losing day. It shortened the real
  outage by 27 minutes, and did the same to snipe-sol (165.74h -> 15.16h) and
  snipe (180.55h -> 60.49h).

  ENDING A DARK PERIOD EARLY UNDERSTATES CENSORING, which is the direction
  that makes a bad ledger look usable — the opposite of what this tool is
  for. The revival list is now only rows a HALTED bot provably cannot write:
  preopen_entry, preopen_mark, and SHADOW_HALT (which the risk manager writes
  only when it has decided NOT to halt). When in doubt, leave a kind out and
  let the period run long.

  THE SAME PASS FIXED A BUG IN THE OTHER DIRECTION. Collapsing consecutive
  HALT rows into one outage — correct, since every restart re-derives the
  same losing day and writes another HALT — swallowed a GENUINE second
  outage whole when the bot had traded in between. The revival is what
  separates them: a HALT after a revival opens a new period, a HALT without
  one continues the old.

  This tool has now been wrong four times in one afternoon, in both
  directions, while being the instrument that decides how much to trust
  everything else. Its pairing rules are pinned by 15 assertions covering
  every case that has actually bitten. Do not change `alive` without adding
  one.

- 2026-08-11 VERIFIED FINAL STATE. halt_audit and the journal now agree to the
  minute: preopen-btc 5.03h dark, 08-11 07:15 -> 12:17 "restart", against
  journal HALT 07:15:09 and SHADOW 12:17. 60 windows of 5m = 5.00h, so the
  window count is consistent with the span rather than derived separately.

      LIVE CENSORED TOTAL   11.82h   141 windows   (btc 60, eth 81)

  THE ONE COMMAND FOR A ROUTINE CHECK, replacing the four-paste ritual:

    cd /opt/polymarketstrat && echo "=== FLEET ===" && systemctl is-active \
      polybot-preopen-btc polybot-preopen-btc-15m polybot-preopen-eth \
      polybot-preopen-eth-15m | tr '\n' ' ' && echo && for u in btc btc-15m \
      eth eth-15m; do printf "\n-- %s\n" $u; journalctl -u polybot-preopen-$u \
      --since -3min --no-pager | grep STATUS | tail -1 | \
      sed 's/.*preopen\[/preopen[/'; done && echo && \
      venv/bin/python -m bot.halt_audit | head -8

  WHAT TO READ IN IT, in order of what has actually gone wrong:
    why={'halted': N}   the breaker is enforcing — should never appear now
                        that paper shadows it, so it means live mode or the
                        flag is off
    why={'missed': N}   a window rolled past unaccounted for. Was invisible
                        until today; if it appears, the loop is stalling
    why={'too_late': N} woke inside the 0.5s floor. Harmless once or twice,
                        a pattern means the box is loaded
    lead worst=         the smallest achieved lead. Slides toward the floor
                        before anything else breaks — the leading indicator
    _mismatches         non-zero halts everything in every mode, correctly

  NEXT, AND NOT A TONIGHT JOB: re-price the 141 censored windows from the
  tape archive. Until then btc 5m's +$163 / 56% is an upper bound and its
  -$580 drawdown is a lower bound.

- 2026-08-11 22:55 OVERNIGHT CHECK — THE FIRING FIX HOLDS, AND A MISMATCH
  HALT FIRED. All four bots active, all four achieving lead med 2.96-2.97s
  worst 2.95s against a 3s target, and NOT ONE `missed` or `too_late` in any
  why histogram. The band widening did its job and is not being leaned on.

      btc 5m    evals 85  in 29  130 fills  +471.38   _mismatches 1
      btc 15m   evals 42  in 12   63 fills  +574.99   _mismatches 0
      eth 5m    evals 31  in  4   69 fills  -542.30   _mismatches 2
      eth 15m   evals 42  in  2    9 fills   -46.29   _mismatches 0

  btc is HALTED since 19:21 on "oracle/exchange winner mismatch" (3.57h and
  counting), eth shows why={'halted': 97}. This is the halt deliberately left
  biting in paper, working as designed — but it is now the single most
  important open question in the project, because a mismatch means our read
  of who won disagrees with the exchange's, and that would make every
  backtest number here a fiction.

  THE TRIPWIRE IS ALREADY GUARDED, which is what makes this serious rather
  than routine: main.py suppresses windows decided by under ORACLE_TIE_BPS
  (0.3), and twap_winner refuses on thin coverage or when the two boundary
  conventions disagree. A flagged window was DECIDED.

  THE HYPOTHESIS, AND IT IS A DEFECT WE ALREADY KNOW THE SHAPE OF. The
  settlement path calls oracle.twap_at, which averages only the seconds that
  are PRESENT — the RESCALING estimator. The ENTRY path calls
  oracle.twap_carry, which carries the last print into each hole, and was
  measured 4x more accurate on btc and 5.5x on eth with rescaling drifting up
  to 0.377bp in its worst bucket. ORACLE_TIE_BPS is 0.3. So rescaling's own
  error can EXCEED the band meant to keep marginal windows from being
  flagged: a window decided by ~0.35bp could be miscalled by our imputation,
  clear the tie filter, and register as a mismatch that no real disagreement
  caused. THE BOT TRADES ON ONE ESTIMATOR AND AUDITS ITSELF WITH A WORSE ONE.

  bot/mismatch_audit.py tests it. It re-derives every flagged window from the
  grid archive under BOTH estimators and, because three windows cannot
  separate two estimators, scores every settled window under both against the
  official outcome. Smoke-tested against a synthetic window built so the two
  land on opposite sides of the open at exactly the 0.9 coverage floor:
  rescaled 50%, carried 100%, as constructed.

  DO NOT CLEAR THE HALT BEFORE READING IT. If carry-forward agrees with the
  exchange, the fix is to settle with the estimator we trade with. If it also
  disagrees, the hypothesis is dead and the problem is real.

  ALSO FIXED: halt_audit called the live preopen-btc15 unit "stopped".
  `systemctl is-active` answers "inactive" for a unit that does not exist,
  which is indistinguishable from one that exists and is stopped, so the
  first candidate name settled it. It now reads LoadState first and skips
  not-found names.

- 2026-08-11 THE ESTIMATOR HYPOTHESIS IS DEAD, AND THE REAL SIGNAL IS SIGNED.
  bot/mismatch_audit.py on 411 btc and 398 eth settled windows:

      estimator    agrees  WRONG  no call  accuracy
      btc rescaled    256      4      151    98.5%
      btc carried     260      5      146    98.1%
      eth rescaled    266      4      128    98.5%
      eth carried     273      6      119    97.8%

  The two estimators differ by a median 0.007bp (btc) / 0.014bp (eth) and
  NEVER by more than 0.247bp — under the 0.3bp tie band on every one of 530
  windows. So the choice of estimator cannot flag a single mismatch, and
  carry is if anything marginally worse. DO NOT change the settlement path
  to twap_carry; that idea is closed.

  WHAT IS LEFT IS A ONE-SIDED ERROR. All three flagged windows say the same
  thing — we called DOWN by a hair, the exchange called UP:
      btc w1786475400  -0.310bp      eth w1786459500  -0.303bp
      eth w1786475400  -0.421bp
  Two sit essentially ON the 0.3bp threshold, and w1786475400 (19:10 UTC)
  mismatched on BTC AND ETH AT THE SAME INSTANT, which no per-coin price
  error can produce. The rule is "TWAP close >= TWAP open", so TIES BREAK UP:
  a small systematic NEGATIVE bias in our close-open pushes near-ties to
  "down" for us and "up" for them, which is exactly this signature.
  Reconstruction noise is symmetric. This is not.

  TWO NUMBERS THAT NEED ANSWERING, both visible above and neither previously
  noticed:
    98.5% IS BELOW THIS PROJECT'S OWN GATE. twap_verify says "do NOT migrate
    the oracle on a rule that scores below ~99%". The 100%-on-76-windows
    result that authorised the migration was a small sample; at 260 called
    windows it is 98.5%. At that rate the mismatch tripwire fires about every
    65 decided windows, which is not a viable configuration — it will keep
    halting the fleet no matter what else is fixed.
    37% OF WINDOWS GET NO CALL (151 of 411 btc). Unexplained. It is either
    coverage or the two boundary conventions disagreeing, and which one it is
    changes what to do about it.

  bot/twap_align.py tests the bias hypothesis properly: it scans boundary
  offsets (-4..+4s) and window lengths (N-2..N+2) against official outcomes,
  picks on the first half and reports on the second, and — the part that
  gives it any power — runs on the NEAR-TIE windows only. A window decided by
  5bp is called correctly by every alignment in the scan; shifting a boundary
  by a second moves the mean by hundredths of a bp and can only flip windows
  already inside that margin, which is precisely where the mismatches live.
  Scanning everything dilutes the comparison to a dead heat.

  scripts/test_twap_align.py holds it to both answers: it recovers a planted
  +2s misalignment (100% out of sample vs an 86.9% baseline) AND reports
  "current alignment stands" on a clean fixture with the same 260 near-tie
  windows and 45 cells to fish in. A scanner that only ever says yes is worse
  than no scanner, because its answer arrives attached to a settlement-path
  change.

  IF THE SCAN FINDS NOTHING, the residual is genuine sub-basis-point
  reconstruction noise against a feed we sample rather than receive, and the
  answer is a tie band matched to the MEASURED error rather than a guessed
  0.3 — but that is a decision to take on evidence, after the scan, not a
  way to make the alarm stop.

- 2026-08-11 ALIGNMENT RULED OUT TOO, AND I OVER-READ THE THREE MISMATCHES.
  twap_align on both coins, chosen on the first half of the near-tie windows
  and reported on the second: btc "nothing beats the current alignment out of
  sample (94.2% vs 92.5%)", eth the same (92.5% vs 92.5%). No boundary offset
  and no window length reads the settlement feed better. Estimator ruled out,
  alignment ruled out.

  THE ONE-SIDEDNESS I FLAGGED IS NOT ESTABLISHED. On the FULL error set
  rather than the three the tripwire happened to flag:
      btc   3 down/up vs 2 up/down   60%
      eth   4 down/up vs 0 up/down  100%  (n=4)
      combined 7 of 9, p ~ 0.18 — not significant
  The flagged set is selected BY MAGNITUDE — only errors above 0.3bp are
  flagged at all — so it was never a fair sample of directions. btc's two
  up/down errors were +0.050bp and +0.015bp and were invisible to the
  tripwire by construction. I read a bias into a filtered sample; it is
  suggestive at most and n=9 cannot establish it.

  WHAT DOES HOLD, AND IT IS THE STRONGER RESULT:

      ZERO ERRORS ON WINDOWS DECIDED BY MORE THAN 1bp, 809 windows, 2 coins.
      All 9 errors lie within 0.421bp of a tie.
      btc |error| median 0.050 max 0.310    eth median 0.303 max 0.421

  The reconstruction is sound. THE TIE BAND IS THE DEFECT: 0.3bp sits BELOW
  the measured residual of 0.421bp, so the tripwire fires on our own
  arithmetic. The config asserts "against a reconstructed TWAP the
  measurement error is ~0.05bp" — that is wrong by roughly 8x and is the
  line that produced this. We approximate a published TWAP STREAM with a
  uniform mean of 1s samples; a few tenths of a bp is structural and does not
  go away without subscribing to the stream itself.

  So 98.5% was never the right accuracy number either. Accuracy ABOVE the
  band is what the tripwire actually needs, and that is 100%.

  twap_align section 3 prices the band as a measurement instead of a
  preference, showing BOTH costs per candidate: how many windows the band
  blinds, and how many errors survive it. That second column is the guard
  against the obvious failure — audit D10 found 2.0bp switched the tripwire
  off on 34-71% of windows, and a sweep reporting only "no false alarms"
  would happily recommend 2.0. Smoke-tested against a fixture with six
  errors planted only below 0.45bp: it reports 0.4 as the first clean band
  and 100% above it, as constructed.

  SET ORACLE_TIE_BPS FROM THAT TABLE, on the two coins agreeing, then clear
  the mismatch and restart btc. Do not pick a band because it silences the
  alarm; pick the smallest one with no false alarms and accept the blind
  fraction it costs.

- 2026-08-11 ORACLE_TIE_BPS 0.3 -> 0.6 FOR THE 5m FAMILY, SET FROM THE
  MEASUREMENT. Both coins' sweeps, on the windows the tripwire actually
  calls:

      band   btc blind   btc errors left | eth blind   eth errors left
      0.3       9.8%           1         |    5.7%           2     <- was
      0.4      11.3%           0         |    7.5%           1
      0.5      12.8%           0         |    9.6%           0
      0.6      13.5%           0         |   10.7%           0     <- now
      2.0      38.7%           0         |   28.1%           0

  btc is clean at 0.4, eth at 0.5, largest residual anywhere 0.421bp. 0.6 is
  one notch above the stricter of the two: it costs about one extra point of
  blindness and buys headroom, because a band sitting AT the observed maximum
  re-trips on the next slightly fatter residual. Above the band the tripwire
  reads 100% on all 809 windows, so nothing is given up in sensitivity to
  REAL defects — there are none up there to find. Compare audit D10, where
  2.0bp blinded 34-71%; at 0.6 it blinds 10-14%.

  15m IS UNMEASURED and stays at 0.3. A 60s mean should carry a smaller
  residual than a 30s one, but should is not measured, and both 15m units are
  at _mismatches 0 so nothing forces the question. Run the same sweep on
  bot/data/preopen-btc15 with FAMILY=15m before touching it.

  scripts/clear_subband_mismatches.py acknowledges ONLY rows whose recomputed
  margin is inside the band. Anything outside it, or any window the archive
  cannot re-price, is REFUSED with a non-zero exit so a chained restart
  cannot run behind it — "cannot check" is not "fine". Winner and
  oracle_winner are never touched; only the flag, and every change writes a
  mismatch_cleared row carrying the measured margin.

  scripts/test_clear_subband.py exercises the refusal paths hardest, because
  that is where the value is: dry run writes nothing, outside-band refuses
  and exits 1, unpriceable refuses, and ONE bad row in a batch fails the
  whole run even though the good rows cleared — the dangerous shape being a
  genuine defect riding along with explainable ones while a zero exit lets
  trading resume on it. The clearer takes REPO_ROOT so those paths can be
  tested against fixtures instead of the live ledgers; code whose whole job
  is refusing must be exercised doing it.

  FIVE SUITES NOW GATE A PRE-OPEN DEPLOY:
      test_preopen_strike  test_risk_shadow_stop  test_halt_audit
      test_twap_align      test_clear_subband

- 2026-08-11 RESOLVED AND TRADING. Dry run said 3 rows, 0 refused; apply
  matched exactly; btc and eth restarted at _mismatches 0, evaluating at lead
  2.96s, and `halted` is gone from every why histogram.

      preopen-btc w1786475400  -0.310bp  cleared
      preopen-eth w1786459500  -0.303bp  cleared
      preopen-eth w1786475400  -0.421bp  cleared
      both 15m ledgers: clean

  FLEET AT THE CLOSE OF THIS INVESTIGATION
      btc 5m   130 fills  +471.38      btc 15m   63 fills  +574.99
      eth 5m    69 fills  -542.30      eth 15m    9 fills   -46.29
      net +457.78, and from here the record is uncensored on the daily stop.

  WHAT THE WHOLE INVESTIGATION CONCLUDED: the oracle is fine. Estimator ruled
  out, alignment ruled out on both coins out of sample, zero errors on any
  window decided by more than 1bp across 809 windows. The tripwire had been
  calibrated to a GUESSED 0.05bp error against a real 0.421bp one, so it was
  flagging our own arithmetic and halting the fleet for it.

- 2026-08-11 STILL OPEN, and worth its own session. THE COVERAGE REFUSALS ARE
  UNEXPLAINED: twap_align reports 146 of 412 btc windows (35%) and 119 of 400
  eth (30%) refused for coverage below 0.9. That is a THIRD of all windows
  where the mismatch tripwire has no opinion at all, for a reason entirely
  separate from the tie band — and it was not noticed until the align scan
  printed it. Refusing is the safe direction so nothing is mis-settled, but
  a third of the sample being unpriceable bounds what any settlement-based
  analysis can ever say, and it is not obviously consistent with the "6.5%
  btc / 10.1% eth blackout" figure recorded earlier. Either the blackout rate
  is worse than measured, or coverage is thin for a different reason. Do not
  raise or lower ORACLE_TWAP_MIN_COVERAGE before knowing which.

- 2026-08-11 THE TWO P&L NUMBERS IN THE STATUS LINE ARE NOT THE SAME NUMBER,
  and reading one as the other is easy:

      pnl_today=       ledger.realized_pnl_today(), ts >= UTC midnight.
                       RESETS AT 00:00 UTC. This is what the breaker reads.
      fills={'pnl':}   ledger.summary(), SUM(pnl) over the fills table with
                       NO time filter. LIFETIME since the ledger began.

  Also: preopen[evals= in= skip= why=] are IN-MEMORY counters that reset to
  zero on every restart, while fills={} comes from the database and persists.
  `evals=1` beside `fills=130` right after a restart is not a contradiction.

  And P&L only lands when a window SETTLES, roughly ten minutes after close,
  so it moves in lumps rather than continuously — a bot that has entered
  nothing for an hour shows a frozen total and is working correctly.

  bot/pnl_daily.py gives the PATH: per UTC day, fills, win rate, day P&L,
  running cumulative, RUNNING peak and drawdown from it, with halted days
  marked as censored. It exists because on 08-10 I quoted +$260, +$311 and
  +$163 at three check-ins and called btc 5m flat, when the path had peaked
  at +$743.34 and given back $580.18 — 78% of peak. No cumulative number
  quoted at intervals can show that, and the drawdown column is the one that
  decides whether a strategy can be sized.

  Smoke-tested against exactly that shape; it reports peak +743.34, worst
  drawdown -580.18, 78% of peak, and marks the losing day HALTED. The first
  version printed the FINAL peak on every row, so an early row claimed a peak
  it had not reached yet and its drawdown column was unreadable — the running
  peak is the only one that means anything per row.

- 2026-08-11 pnl_daily's DRAWDOWN WAS WRONG ON ITS FIRST RUN, and wrong in
  the flattering direction. It reported btc 5m "worst drawdown -0.46 (0% of
  peak)" when the real figure is -580.18 from a +743.34 peak — the number I
  had already reported correctly on 08-10 and then contradicted with a tool
  built to show exactly this.

  CAUSE: it aggregated by day and measured drawdown on DAY-END equity. The
  08-10 round trip started and finished inside one day, which closed at
  +471.84, so a daily series cannot see it at all. Drawdown is the entire
  reason the tool exists, and it was computed on the one series that cannot
  show it.

  It now walks the fill-level curve and prints BOTH, with the gap called out
  when the intraday figure is materially worse. Smoke-tested against that
  exact shape: day-end DD +0.00, intraday peak +743.34, worst -580.18, 78%.

  GENERAL LESSON, and it is the third time this pattern has appeared today:
  a summary statistic computed at the wrong granularity is not a smaller
  version of the right one, it is a different number that happens to look
  plausible. Rate-averaged feed gaps hid an 18-minute blackout; day-averaged
  equity hid a 78% drawdown; cumulative-at-check-in hid the same thing
  earlier. Whenever a number is meant to catch a bad episode, compute it at
  the granularity the episode happens at.

- 2026-08-11 ALL-TIME P&L, and what it is worth. Four bots, TWO DAYS of data:
      btc 5m   130 fills  +471.38    btc 15m   63 fills  +574.99
      eth 5m    69 fills  -542.30    eth 15m    9 fills   -46.29
      TOTAL                                             +457.78
  Reasons not to read that as an edge yet:
    - two days. Not a sample.
    - btc 15m's entire result is one 91%-of-22-fills day (+776.81) followed
      by 46% of 41 (-201.82). The first day is not repeatable and the second
      is what the strategy looks like without luck.
    - both eth bots have never been above water.
    - every pre-08-11 day is censored by the daily breaker.
  btc 5m at 56% on 130 fills against a ~53.6% break-even is the only cell
  that is both positive and plausible, and 130 fills cannot separate 56% from
  break-even. The tape backtest, not these ledgers, is still the instrument
  with any power.

- 2026-08-11 THE INTRADAY DRAWDOWNS, once measured fill by fill, are LARGER
  THAN THE PROFITS in every case:

      bot        lifetime   peak      worst DD        trough
      btc 5m     +471.38   +743.34   -898.53 (121%)   -155.19  went underwater
      btc 15m    +574.99   +883.36   -653.88  (74%)   +229.48
      eth 5m     -542.30   +122.09   -744.36 (610%)   -622.27
      eth 15m     -46.29     never   -117.25

  The day-end view showed btc 5m at -0.46. It actually round-tripped from
  +743 to -155 and back to +471. Nothing can be sized against a P&L total
  whose drawdown exceeds it, and two days cannot even establish the total.

  pnl_daily now closes with the only question a total cannot answer — is
  this distinguishable from zero — using a Wilson interval against the fee-
  adjusted break-even (px + 0.07*px*(1-px); 0.5187 -> 53.62%, NOT 50%), plus
  the fills needed to separate the observed edge from break-even.

  btc 5m: 73/130 = 56.2%, 95% CI [47.6, 64.4] against break-even 53.62. It
  STRADDLES. At the observed +2.54pp edge, separating them needs about 1,471
  fills — roughly 21 more days at the current rate. That is the honest
  answer to "is the edge there": the live ledger cannot say yet, and no
  amount of staring at the running total will change that. The tape
  backtest, with 7,493 backfilled windows, remains the instrument with
  power; the ledger is confirmation, not discovery.

  Hand-checked before use: Wilson(50,100) = [40.4, 59.6] against the
  textbook value, break-even(0.5187) = 53.62% against the figure derived on
  08-10, and n=0 returns (0,0) rather than dividing by zero.

- 2026-08-11 THE ENTRY PRICE IS THE STORY, AND THE CEILING IS TOO LOOSE.
  Fleet results ranked by average entry price, and they rank together:

      bot       entry    break-even   win rate   verdict
      btc 15m   0.5061     52.35%      61.9%     best bar, best result
      btc 5m    0.5244     54.19%      56.2%     straddles
      eth 5m    0.5338     55.12%      42.0%     BELOW b/e at 95%
      eth 15m   0.5471     56.45%      55.6%     worst bar

  The bar moves 52.35% -> 56.45%, a four-point swing, LARGER THAN ANY EDGE
  BEING MEASURED. The strategy docstring says the pre-open book is "flat and
  symmetric, ~0.50 a side"; we are paying up to 0.5471.

  PREOPEN_MAX_PX IS 0.56 ON ALL FOUR UNITS. Break-even at 0.56 is 57.73% —
  above the best settle rate this strategy has ever measured (60.4%, and that
  was the whole-sample figure, not the marginal one). The ceiling is
  admitting trades that cannot pay, and every one of them drags the average
  entry up and the measured win rate down.

  bot/entry_ceiling.py sweeps it on the recorded fills. This is a legitimate
  backtest rather than curve-fitting: the entry price is observable BEFORE
  the trade — the bot already compares best_ask to the ceiling — so dropping
  fills above a tighter ceiling is a question we could have answered in
  advance, not a selection on the outcome. The sweep can only go DOWN from
  0.56 because no data exists above it. Best cell picked on the first half of
  each ledger, reported on the second.

  Smoke-tested against a planted price effect (cheap fills 60%, dear fills
  50%): it recovers the tight ceiling, and the tighter ceiling earns MORE
  total P&L on half the fills because the expensive ones are net negative.

  THE FIRST STATISTICALLY SIGNIFICANT RESULT IN THE FLEET, and it is a
  negative one: eth 5m is 29/69 = 42.0%, 95% CI [31.1, 53.8] against a
  55.12% break-even. The upper bound is BELOW the bar. That is a losing
  configuration, not an unlucky one, and it agrees with the 08-10 tape
  finding that eth's floor was -0.05c and did not clear. Everything else in
  the fleet still straddles.

  A CAUTION ON "37 more fills, roughly 1 day" FOR btc 15m: that projection
  assumes the observed 61.9% is the true rate, and 61.9% comes from a 91%-of-
  22-fills day followed by 46% of 41. The two days are inconsistent at
  p ~ 0.0005. Do not expect resolution tomorrow; expect the rate to fall.

- 2026-08-11 THE CEILING HYPOTHESIS IS DEAD, AND IT FAILED BACKWARDS ON BTC.
  I predicted tightening PREOPEN_MAX_PX would help because a 0.56 fill needs
  57.73% to break even. On btc the effect runs the OTHER WAY:

      btc 5m    ceiling 0.50: 24 fills, 45.8%, edge -4.66c, P&L -276.00
                ceiling 0.56: 130 fills, 56.2%, edge +1.97c, P&L +471.38
      btc 15m   ceiling 0.50: 34 fills, 47.1%, edge -2.68c, P&L  -60.76
                ceiling 0.56:  63 fills, 61.9%, edge +9.55c, P&L +574.99

  THE CHEAPEST FILLS LOSE MONEY ON BOTH BTC BOTS. eth 5m runs the opposite
  way (0.50 -> 62.5%, 0.56 -> 42.0%), so the coins disagree and no single
  ceiling rule is supported. Do not change PREOPEN_MAX_PX.

  WHAT THE BTC DIRECTION SUGGESTS, and it contradicts the strategy's stated
  premise. preopen.py refuses a leaning book on the theory that "the makers
  priced the tilt first and the edge shrinks one-for-one". The data says the
  opposite: a HIGH ask on our side means the book already agrees with the
  tilt, and those are the fills that win. A LOW ask means we are buying the
  side the book thinks will lose. If that holds up it is a signal, not a
  cost — but it is 2 days on one coin with the other coin disagreeing, and
  it is exactly the shape of thing this project has retracted twice. Test it
  properly on the tape archive before touching anything.

  NOT ONE CELL IN THE WHOLE SWEEP HAS A POSITIVE 95% LOWER BOUND. Four bots,
  seven ceilings each: every '95% lo' is negative. No configuration of any
  bot has a demonstrable edge yet.

- 2026-08-11 "IT STARTED REALLY WELL, WHAT HAPPENED" — NOTHING HAPPENED, AND
  HERE IS THE ARITHMETIC. From btc 5m's own recorded parameters (137.5 shares
  at 0.5244, win +$62.98, loss -$74.48, 65 fills/day):

      per fill    mean +$2.71   sd $68.21
      per day     mean  +$176   sd  $550
      over 2 days mean  +$352   sd  $778     observed +471.38

  A +$471 two-day result is 0.61 standard deviations from ZERO. Day 1
  (+471.84) and day 2 (-0.46) had the SAME 56% win rate; the difference
  between them is entirely which trades happened to win. The daily noise band
  is +/-$550 around a +$176 expectation, so a good first day and a flat
  second are both unremarkable draws from the same distribution.

  At this edge the cumulative needs about 39 DAYS to sit two standard
  deviations from zero. That is the whole answer: the strategy did not
  degrade, it was never yet measurable, and no amount of watching the running
  total will change that before roughly mid-September.

  btc 15m IS the one that genuinely collapsed: in-sample edge +21.45c,
  out-of-sample -1.99c. That is a 91%-of-22-fills day reverting, not a
  strategy breaking. entry_ceiling now prints the in-sample vs out-of-sample
  edge even when the incumbent ceiling wins, because "the current ceiling is
  already the best cell" was hiding that collapse behind a reassuring line.

- 2026-08-11 WHAT NOW: the plan, in priority order.

  1. TEST THE BOOK-LEAN INVERSION ON THE ARCHIVE (bot/lean_test.py, built).
     This is the only live observation that could change the strategy rather
     than just measure it. preopen.py refuses a leaning book because "the
     makers priced the tilt first and the edge shrinks one-for-one"; the
     ledger says the dearest fills are the ones that WIN on both btc bots.
     If a high ask means the book AGREES with the tilt, the ceiling is
     discarding the good half and the rule should invert.
     The ledger cannot settle it — 130 fills, eth disagreeing, every lower
     bound negative. lean_test joins the book ARCHIVE to the tape outcomes,
     which is many times the sample AND free of the ceiling's own selection,
     because the archive holds the windows the bot REFUSED as well.
     Smoke-tested against a planted effect: recovers dear 65.0% (+8.76c,
     lower bound +1.93) vs cheap 45.5% (-5.75c), in both halves.
     GATE ON ACTING: both time halves must agree AND both coins. One half or
     one coin is the vol-scaled-gate shape, retracted twice.

  2. LET THE FLEET RUN. Nothing to tune. From today the record is
     uncensored, no windows are dropped and the tripwire no longer fires on
     our own arithmetic. btc 5m needs ~39 days to sit 2sd from zero.

  3. eth 5m IS DEMONSTRABLY LOSING (CI entirely below break-even) and agrees
     with the 08-10 tape finding. Left running deliberately: it costs nothing
     in paper and it is the control that makes btc's numbers meaningful.
     Do NOT read its P&L as a fleet loss.

  4. LATER, and each is a session: explain the 35%/30% coverage refusals
     (a third of windows the tripwire cannot see, unexplained and not
     obviously consistent with the 6.5%/10.1% blackout rate); re-price the
     141 censored windows from the tape.

  lean_test's out-of-sample split originally printed NOTHING when the asks
  tied at the median — the "dear" half came out empty and the row was
  skipped by a `continue`. Same silent-failure shape as the dropped windows
  and the bare halt `continue`. It splits by RANK now, and says so when a
  split is impossible rather than vanishing.

- 2026-08-11 THE LEAN INVERSION FAILS THE COIN TEST. Refreshed the tape (214
  windows each; the cache was stale, which was costing HALF the sample) and
  ran lean_test on the book archive:

      btc 5m, 115 gated windows   57.4% settle, edge +3.55c, 95% lo -5.58
        first half   cheap 46.4% -5.25c   dear 62.1% +7.87c   DEAR ahead
        SECOND half  cheap 55.2% +3.77c   dear 65.5% +7.52c   DEAR ahead

      eth 5m,  93 gated windows   57.0% settle, edge +3.12c, 95% lo -7.02
        first half   cheap 60.9% +10.16c  dear 47.8% -8.65c   cheap ahead
        SECOND half  cheap 69.6% +18.21c  dear 50.0% -6.77c   cheap ahead

  EACH COIN IS STABLE ACROSS TIME AND THE TWO COINS POINT OPPOSITE WAYS. The
  pre-registered gate was "both halves AND both coins"; it fails, so the
  ceiling rule is NOT inverted. Note also that not one bucket in either table
  has a positive 95% lower bound, and two coins each agreeing with themselves
  across a split is a 25% coincidence, not evidence. Closed unless a much
  larger sample revives it.

  THE FUNNEL EARNED ITS KEEP: the drop was 432 -> 218 at "settled outcome",
  i.e. the TAPE CACHE WAS STALE, not thin coverage as I predicted. One
  tape_backfill run took btc from 61 gated windows to 115 and eth from below
  the floor to 93. Re-run it before any archive analysis; it only fetches
  what it lacks. Coverage prices 90-95% here, so the 35% tripwire refusal
  rate is a different question after all.

- 2026-08-11 THE SAME RUN FOUND SOMETHING BIGGER: WE ARE NOT PAYING THE QUOTE.

      quoted at T-3 (archive)   paid (live fills)    gap
      btc      0.5210                0.5244        -0.34c
      eth      0.5212                0.5338        -1.26c

  The archive says eth's gated windows are available at 0.5212 and worth
  about +3.12c; the live eth bot paid 0.5338 and lost $542. On a 3c edge,
  1.26c is nearly half of it — and it is not a signal problem at all, since
  the tilt picked the same windows either way.

  SUSPECTED MECHANISM: executor.take() sends a marketable limit at
  PREOPEN_MAX_PX for PREOPEN_CLIP=250 shares. A limit priced at the CEILING
  walks the book as far as the ceiling to fill the clip, so whenever the
  touch holds fewer than 250 shares the remainder fills higher. That is a
  size problem with a size fix, and a smaller clip costs NO signal — the same
  windows are entered, just smaller.

  bot/slippage.py tests it by joining each preopen_entry to the book
  recorder's T-3 snapshot for the same window and side. The discriminator is
  DEPTH: sweeping makes the gap grow as the touch thins below the clip; a
  merely stale quote does not care about depth. Smoke-tested against a
  planted effect (thin touch 2c, deep touch 0c): reports +2.00c thin,
  +0.00c deep, verdict "the clip is sweeping", as constructed.

  IF IT CONFIRMS, this is the largest lever found today and it is a one-line
  change. If slippage does not track depth, the clip is innocent and the
  cause is timing between the snapshot and the fire — a different fix, and
  worth knowing before touching anything.

- 2026-08-11 SLIPPAGE CONFIRMED, AND IT IS THE LARGEST DRAG FOUND SO FAR.

      btc  quoted 0.5106  paid 0.5253  +1.47c/share, above quote on 93% (65/70)
      eth  quoted 0.5153  paid 0.5388  +2.35c/share, above quote on 95% (18/19)

  Against edges of +3.55c (btc) and +3.12c (eth) measured on the archive,
  that is 42% and 75% of the edge lost between the quote and the fill. It is
  NOT a signal problem — the tilt selected the same windows either way.

  THE DEPTH RELATIONSHIP IS MONOTONIC, which a stale quote cannot produce:

      touch under 125   n=28   +1.93c
      touch 125-250     n=15   +1.75c
      touch 250-500     n=22   +0.92c
      touch over 500    n=5    +0.49c
      thinner than the clip +1.86c   deeper than the clip +0.84c

  So the clip IS walking the book. All 19 eth entries had a touch under 125
  shares against a 250 clip — eth's book is simply thinner, which is a
  sufficient explanation for why eth is the losing bot without any appeal to
  its signal being worse.

  THE +0.49c FLOOR ON DEEP BOOKS IS NOT SWEEPING. That is drift between the
  T-3 snapshot and the fire, and no clip change touches it. Only the excess
  above it is a size problem.

  DO NOT REFLEXIVELY CUT THE CLIP. Total EV is clip x (edge - slippage), so a
  smaller clip buys a better price on fewer shares and can earn LESS. The
  tool now prices $/trade across clip sizes from the per-window swept price,
  backed out of what we actually paid (cost = touch x eff_ask + rest x swept,
  one unknown) rather than from a fill model.

  A DEFECT IN THAT SECTION, CAUGHT BY ITS OWN SMOKE TEST: the first version
  showed clips of 350 and 500 earning more. Every observation comes from a
  250 clip, so the ladder above 250 is unobserved, and modelling it pinned
  the marginal share at the AVERAGE swept price instead of letting it rise —
  "bigger is always better" was a property of the arithmetic, not the book.
  It now refuses to show any clip above the largest observed fill. Fourth
  instance today of a tool being confidently wrong in the flattering
  direction; the smoke test with a known answer is what caught every one.

- 2026-08-12 THE CLIP STAYS AT 250, AND THE SLIPPAGE FINDING IS GOOD NEWS.

      clip   avg price   edge¢/sh   $/trade
        25     0.5197      +2.77c     +0.69
        50     0.5201      +2.73c     +1.37
       100     0.5212      +2.62c     +2.62
       150     0.5224      +2.50c     +3.75
       200     0.5236      +2.38c     +4.77
       250     0.5252      +2.22c     +5.55   <- current, best observed

  $/trade rises monotonically to 250: the marginal share is still positive
  because the per-share edge falls only 2.77 -> 2.22c while the position
  grows tenfold. CUTTING THE CLIP WOULD HAVE COST MONEY, which is exactly
  where "slippage is 1.47c, shrink it" leads. The price column alone must
  never decide a size question.

  (The table stops at 250 because that is the largest fill observed; a
  larger clip may or may not still be marginally positive and there is no
  data either way. Do not raise it on this table.)

  THE PART THAT MATTERS FOR REAL MONEY. Live size is NOT set by
  PREOPEN_CLIP, it is set by the per-trade cap of 10% of bankroll at the
  limit price:

      bankroll $150 ->  27 shares      $1200 -> 214 shares
                $300 ->  54                  $2400 -> 429
                $600 -> 107

  Every rung below $1200 trades FEWER shares than paper does, so it sweeps
  less and pays closer to the touch. At 27 shares the modelled price is
  0.5197 against paper's 0.5252 — the per-share edge is 2.77c rather than
  2.22c, a QUARTER better. The paper ledger is therefore a CONSERVATIVE
  estimate of the per-share edge for a small live account, not an optimistic
  one. That is the first thing found in this whole project that makes live
  look better than paper rather than worse.

  STILL OPEN AND WORTH A SESSION: the +0.84c that remains when the touch is
  DEEPER than the clip. That is not sweeping — it is the book moving between
  the recorder's T-3 REST snapshot and the bot's fire, or the two sampling
  different instants. On a 3.5c edge it is 24%, so it is the largest
  remaining execution question, and it is a TIMING question rather than a
  size one. eth cannot be diagnosed yet: all 19 of its matched entries had a
  touch under 125 shares, so there is no deep-book band to compare against.

  NOTHING IS BEING CHANGED ON ANY OF THIS. Clip unchanged, ceiling
  unchanged, gates unchanged. The fleet's configuration is now supported by
  measurement at every parameter that was questioned today.

- 2026-08-12 btc 15m IS NOT THE BEST BOT. ITS RESULT IS ONE DAY.

      08-10   20/22 = 90.9%   +776.81
      08-11   19/41 = 46.3%   -201.82
      pooled  39/63 = 61.9%   CI [49.6, 72.9]   break-even 52.35%  straddles

  P(>=20 of 22) at the pooled 61.9% rate is 0.00267 — a one-in-375 day
  against its OWN best estimate. The two days are not consistent with a
  single rate, and the whole +574.99 rests on the extreme one. The second
  day, which has nearly twice the fills, is 46.3% with a CI of [32.1, 61.3]:
  point estimate BELOW break-even.

  So its headline +575 does NOT make it better than btc 5m's +471. It makes
  it noisier. Read them as: both straddle, btc 5m on 130 fills and btc 15m on
  63 of which 22 are the outlier day. (The 0.00267 is post-hoc — with two
  days the more extreme one will always look extreme — but 20 of 22 is
  remarkable however it is framed.)

  ONE REAL ADVANTAGE THE 15m FAMILY DOES HAVE: entry price. btc 15m fills at
  0.5061 against btc 5m's 0.5244, so its break-even is 52.35% rather than
  54.19% — nearly two points lower a bar, structurally, because the pre-open
  book is flatter when the window is longer. That is worth more than any gate
  tuned today, and it is the reason to keep the 15m family running.

- 2026-08-12 eth 15m IS STARVED BY DESIGN, AND THAT IS PROBABLY CORRECT.
  9 fills, 5/9 = 55.6%, CI [26.7, 81.1] against a 56.44% break-even. Useless,
  and it will stay useless for a while: the 1.7bp gate passes about 4.5 fills
  a day, so 100 fills is 22 days and 200 is 44.

  DO NOT LOWER THE GATE TO COLLECT FASTER. The 1.7 came from the measured
  rule gate ~= 0.31 * vol * sqrt(window) (0.31 * 0.185 * 30 = 1.72), and the
  08-10 bucket work found eth separates only ABOVE 1bp — 1.0-1.5 57.1%,
  1.5-2.0 59.6%, 2.0-3.0 61.1%. Lowering the gate would buy volume by
  trading exactly the windows eth is known to be bad at. Slow and correct
  beats fast and wrong; leave it.

  NOT YET RUN ON THE 15m FAMILY: lean_test and slippage. Both need
  FAMILY=15m, and their tape caches are STILL STALE — only the 5m caches
  were backfilled. Note 15m accumulates a THIRD of the windows per day, so
  lean_test will likely sit under its 60-window floor for another week;
  slippage should work now on btc 15m's ~60 entries.
