"""Record the 1-second Chainlink price grid so the TWAP rule can be verified.

  COIN=btc nohup venv/bin/python -m bot.twap_record > /tmp/twaprec-btc.log 2>&1 &
  COIN=eth nohup venv/bin/python -m bot.twap_record > /tmp/twaprec-eth.log 2>&1 &

Polymarket does not publish its TWAP streams on ws-live-data (probed
2026-08-08), so the resolver's 30s/60s TWAP has to be reconstructed from the
1s Chainlink grid at topic crypto_prices_chainlink — the same series the
oracle already consumes. This recorder persists that grid, and
bot/twap_verify.py then scores reconstructed rules against official market
outcomes. Nothing trades off this; it only writes samples.

Storage: bot/data/twapcal/<coin>_1s.db, table px(ts INTEGER PRIMARY KEY, v REAL).
~86,400 rows/day/coin, a few MB — negligible. Writes are batched once per
second-ish and INSERT OR IGNORE, so restarts and backfill overlap are safe.
"""
import asyncio
import json
import logging
import os
import sqlite3
import time

import aiohttp

COIN = os.environ.get("COIN", "btc").lower()
SYMBOL = os.environ.get("PM_PRICE_SYMBOL", f"{COIN}/usd")
WS = os.environ.get("PM_LIVE_WS", "wss://ws-live-data.polymarket.com")
DB_DIR = os.environ.get("TWAP_DIR", "bot/data/twapcal")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("twaprec")


def open_db():
    os.makedirs(DB_DIR, exist_ok=True)
    db = sqlite3.connect(os.path.join(DB_DIR, f"{COIN}_1s.db"))
    db.execute("CREATE TABLE IF NOT EXISTS px(ts INTEGER PRIMARY KEY, v REAL)")
    db.commit()
    return db


def samples(raw):
    """Yields (sample_second, price) from either message shape:
    single update {payload:{symbol,timestamp,value}} or the on-connect
    backfill {payload:{data:[{timestamp,value},...]}}."""
    try:
        d = json.loads(raw)
    except ValueError:
        return
    for ev in d if isinstance(d, list) else [d]:
        p = ev.get("payload") or {}
        if not p:
            continue
        rows = p.get("data")
        if isinstance(rows, list):
            for r in rows:
                t, v = r.get("timestamp"), r.get("value")
                if t and v:
                    yield int(t) // 1000, float(v)
        else:
            t, v = p.get("timestamp"), p.get("value")
            if t and v:
                yield int(t) // 1000, float(v)


async def main():
    db = open_db()
    have = db.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM px").fetchone()
    log.info("recording %s -> %s/%s_1s.db (existing rows=%d span=%s..%s)",
             SYMBOL, DB_DIR, COIN, have[0],
             time.strftime("%m-%d %H:%M", time.gmtime(have[1])) if have[1] else "-",
             time.strftime("%m-%d %H:%M", time.gmtime(have[2])) if have[2] else "-")
    sub = {"action": "subscribe", "subscriptions": [{
        "topic": "crypto_prices_chainlink", "type": "update",
        "filters": json.dumps({"symbol": SYMBOL}, separators=(",", ":")),
    }]}
    backoff, buf, last_commit, total = 1, [], time.time(), 0
    while True:
        try:
            async with aiohttp.ClientSession(trust_env=True) as s:
                async with s.ws_connect(WS, heartbeat=15, receive_timeout=20) as ws:
                    await ws.send_json(sub)
                    log.info("connected")
                    backoff = 1
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT or not msg.data:
                            continue
                        buf.extend(samples(msg.data))
                        now = time.time()
                        if buf and now - last_commit >= 2.0:
                            db.executemany(
                                "INSERT OR IGNORE INTO px VALUES(?,?)", buf)
                            db.commit()
                            total += len(buf)
                            buf.clear()
                            last_commit = now
                        if now % 300 < 0.05:
                            n, lo, hi = db.execute(
                                "SELECT COUNT(*), MIN(ts), MAX(ts) FROM px").fetchone()
                            log.info("rows=%d span=%.1fh (ingested %d)", n,
                                     (hi - lo) / 3600 if lo else 0, total)
        except Exception as e:  # noqa: BLE001
            log.warning("feed dropped (%s); reconnect in %ds", e, backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 15)


if __name__ == "__main__":
    asyncio.run(main())
