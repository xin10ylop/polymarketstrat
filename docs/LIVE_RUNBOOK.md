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
- `bot/engine/live.py`: LiveExecutor (FAK marketable-limit takes, sweep-sized,
  real matched size/price recorded from the exchange response), Bankroll
  (manual `BANKROLL` only — the bot NEVER sizes off the account balance),
  hourly spend-side balance reconciler (halts on unexplained shortfall).
- Retry policy: up to `SNIPE_MAX_ATTEMPTS=4` takes per window while the signal
  persists, capped by clip (250 sh), per-window cost (`SNIPE_WINDOW_MAX_COST`)
  and bankroll per-trade cap. A missed FAK costs nothing and is retried.
- Three locks before any real order (ALL must be opened deliberately):
  1. `BOT_MODE=live` + `BANKROLL>0` + `PM_PRIVATE_KEY` present
  2. `LIVE_CONFIRM=I-UNDERSTAND-REAL-MONEY`
  3. `LIVE_SHADOW=0` (default is 1 = signs+sizes but never posts)

## Money rules (enforced in code, not by discipline)

| Rule | Value | Where |
|---|---|---|
| Per-trade cost | ≤ 10% of bankroll | live.py per_trade_cap |
| Position at a time | 1 | live.py _in_flight |
| Per-take size | ≤ 250 shares (audited: bigger = −EV) | config snipe_max_clip |
| Per-window cost | ≤ $300 across retries | config snipe_window_max_cost |
| Daily stop | −20% of bankroll → sticky halt | risk.py via bankroll |
| Trades/day | ≤ 40 | live.py |
| Live strategies | snipe only (toll NOT live-qualified) | LIVE_STRATEGIES |

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
   `LIVE_SHADOW=1`. Verify in journal: `[SHADOW] would take ...` lines at
   sensible sizes, balance readable at startup, zero errors. Compare shadow
   takes against the NYC paper bot's fills for the same windows — they should
   largely agree.
4. **Tier 0**: set `LIVE_SHADOW=0`, `LIVE_CONFIRM=I-UNDERSTAND-REAL-MONEY`,
   `BANKROLL=150`. Restart. 3–5 days. The goals are MEASUREMENTS, not profit:
   real fill-through rate vs paper's gate (expect roughly parity), the actual
   fee charged on fills (expect $0 maker / 0.07·p·(1−p) taker), zero
   reconciler alerts.
5. **Ladder** per the table above. Keep the paper fleet running forever as the
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
