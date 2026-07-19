# Snipe retry/sweep EV study (tick reconstruction, Jun 1 – Jul 13 2026)

Condensed findings; parameters applied are marked ✅. Trust: VERIFIED tables >
INFERRED scalings. Caveat everywhere: the reconstruction samples raw episodes
(median survival 30-50ms), which the live/paper poll-loop never sees — level
estimates are biased low vs the paper bot's realized EV; RELATIVE comparisons
(price bands, timing, episode index) are the actionable part.

## Retry value (Q1)
- After a missed first ask, an 83% chance another qualifying ask appears in
  the same window; retry EV positive. Retries add +$76-150/day (idealized) at
  0.3-0.65s round-trips. ✅ retry loop shipped.
- Marginal EV turns negative at the 3rd repeat attempt. ✅ SNIPE_MAX_ATTEMPTS=3.
- First-shot $/day FALLS with clip size (large fresh asks are informed);
  retry $/day RISES with clip. ✅ SNIPE_FIRST_CLIP=100, retries up to 250.
  ✅ SNIPE_SKIP_ASK_ABOVE=500 (also matches the earlier audit's >=250 bucket
  at -2.4c/sh).

## Sweep value (Q2)
- A marketable 0.97 limit (sweeping levels) adds ~+$29-53/day at 150-250 clips
  vs top-of-book-only, +2.2-2.3c/sh incremental. ✅ sweep semantics shipped
  (paper + live FAK).

## Price bands (Q3) — IMPORTANT, partially conflicted
- In this 43-day window only [0.90, 0.97] is +EV (+0.51c, 95% win); 0.97-0.985
  is significantly negative → ✅ ceiling stays 0.97, never raise.
- Deep asks (<=0.80) measure NEGATIVE here, vs +7.97c in the May-Jul
  validation and vs the LIVE PAPER BOT's realized +8c/sh at ~0.71 avg entries
  (Jul 15-19). CONFLICT — reconstruction bias vs regime change unresolved.
  ✅ SNIPE_PRICE_FLOOR added but OFF; WEEKLY WATCH ITEM: bucket paper fills by
  entry price; if <=0.80 fills bleed over a week, set SNIPE_PRICE_FLOOR=0.90.

## Timing (Q4)
- Sweet spot T-4..T-2; T-1 sharply negative (-5.6c) → ✅ SNIPE_EVAL_UNTIL_S=-1.5.
- Extending earlier than T-6 does not pay (T-10..T-7 all negative first-touch).
  Eval start stays -6.

## Multi-attempt risk (Q5)
- A lost window loses on EVERY attempt (one outcome per window — zero
  diversification). Unconstrained per-window firing risks $3-6k tails.
  ✅ per-window cost cap ($300) + attempts cap (3) bound the tail to ~2 losses.

## Net expectation
Idealized reconstruction +$71-150/day at 250 clips; after the audited 18-32%
live fill-through: ~$2-30/day from BTC snipe alone (wide honest band), before
multi-coin and speed upgrades. The paper fleet's gated results remain the
primary live forecast.
