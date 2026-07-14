# Polymarket BTC 5m paper-trading bot

Runs the two OOS-validated strategies (post-close toll + basis-corrected
terminal snipe) against **live** exchange data. Paper mode simulates fills from
the real tape/book; live mode reuses the identical code path with a signing
executor (deliberately fail-closed until the paper phase qualifies).

## Deploy (Ubuntu droplet)

```bash
apt-get update && apt-get install -y python3-venv git
git clone <this-repo> /opt/polymarketstrat        # or scp the repo over
cd /opt/polymarketstrat
python3 -m venv venv && venv/bin/pip install aiohttp
cp bot/deploy/polybot.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now polybot
journalctl -u polybot -f                          # watch it trade
```

Daily check: `cd /opt/polymarketstrat && venv/bin/python -m bot.report 7`

## What to verify before going live (the qualification gate)

1. **≥14 days** of paper trading with the ledger's EV within ~30% of the
   backtest (toll ≈ +0.7–0.8c/sh; snipe ≈ +3–6c/sh) and zero winner mismatches.
2. `queue_share` calibration: compare paper toll fills vs the visible dump flow;
   live fills will settle the real number — start live at 1/4 clip sizes.
3. Fee reality check: first live fills show actual maker/taker fees; update
   `TAKER_FEE_MULT` / `MAKER_FEE_MULT` if the exchange changed the schedule
   (metadata currently hints at a possible maker fee — margin survives 0.07
   but re-run bot/report to confirm net EV).
4. **US geo**: live order placement is blocked from US IPs (this droplet is
   NJ). Paper is read-only and fine; the live host must be non-US and you
   should confirm your own eligibility under Polymarket's terms.
5. Keys via environment only (`PM_PRIVATE_KEY`, `PM_API_KEY`,
   `PM_API_SECRET`, `PM_API_PASSPHRASE`, optional `PM_FUNDER`) — never in git.
   Then remove the fail-closed guard in `engine/executor.py::LiveExecutor`
   consciously, reading its comment.

## Config

Everything is in `bot/config.py`; env vars override (BOT_MODE, SPOT_FEED,
TOLL_FLOAT, MAX_DAILY_LOSS, ...). Data lands in `bot/data/paper.db` (sqlite).

## Architecture

- `feeds/oracle.py` — Chainlink BTC/USD @ Polygon, 300ms poll, boundary sampler
- `feeds/spot.py` — Coinbase (default) or Binance trades → 1s bars, vol, basis
- `feeds/clob.py` — gamma discovery + CLOB ws books/prints/tick-size
- `engine/executor.py` — Paper (tape-replay fills) / Live (fail-closed)
- `engine/ledger.py` — sqlite; PnL marked from the exchange's own resolution
- `engine/risk.py` — daily-loss, snipe-winrate, feed-silence, mismatch halts
- `strategies/toll.py` — winner bid @ 0.992/0.99 with fill-share controller
- `strategies/snipe.py` — basis-corrected fv ≥ 0.995 → take min(ask, 250)
- `main.py` — orchestrator; `report.py` — daily rollup
