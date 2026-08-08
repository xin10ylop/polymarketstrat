"""Discover Polymarket's Chainlink TWAP price feed.

  venv/bin/python -m bot.twap_probe            # btc
  COIN=eth venv/bin/python -m bot.twap_probe

WHY: on 2026-08-07 00:00 UTC Polymarket switched the 5m/15m crypto up-down
markets from the Chainlink spot data stream to ROLLING TWAP streams
(btc-usd-twap-30s for 5m windows, btc-usd-twap-60s for 15m) and changed the
comparison to "TWAP at close vs TWAP at open". Our oracle still reads the
spot stream, so it computes the wrong quantity. Before the oracle can be
fixed we must know the exact topic/symbol string that carries the TWAP
values on wss://ws-live-data.polymarket.com.

This probe subscribes to a matrix of candidate (topic, filter) pairs, one
fresh connection each, and reports which ones deliver frames plus the
distinct symbols seen. Read-only: subscribes and prints, nothing else.
"""
import asyncio
import json
import os
import time

import aiohttp

WS = "wss://ws-live-data.polymarket.com"
COIN = os.environ.get("COIN", "btc").lower()

TOPICS = [
    "crypto_prices_chainlink",
    "crypto_prices_chainlink_twap",
    "crypto_prices_chainlink_twap_30s",
    "crypto_prices_twap",
    "crypto_prices",
]
SYMBOLS = [
    None,                              # no filter: show me everything on the topic
    f"{COIN}/usd",
    f"{COIN}/usd-twap-30s",
    f"{COIN}/usd-twap-60s",
    f"{COIN}/usd_twap_30s",
    f"{COIN}usd-twap-30s",
    f"{COIN}/usd/twap/30s",
]

WAIT_S = 8.0


def sub_msg(topic, symbol):
    s = {"topic": topic, "type": "update"}
    if symbol is not None:
        s["filters"] = json.dumps({"symbol": symbol}, separators=(",", ":"))
    return {"action": "subscribe", "subscriptions": [s]}


async def attempt(session, topic, symbol):
    """Returns (n_frames, distinct symbols, one sample payload)."""
    seen, sample, n = set(), None, 0
    try:
        async with session.ws_connect(WS, heartbeat=15, receive_timeout=WAIT_S + 2) as ws:
            await ws.send_json(sub_msg(topic, symbol))
            end = time.time() + WAIT_S
            while time.time() < end:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=end - time.time())
                except (asyncio.TimeoutError, ValueError):
                    break
                if msg.type != aiohttp.WSMsgType.TEXT or not msg.data:
                    continue
                try:
                    d = json.loads(msg.data)
                except ValueError:
                    continue
                for ev in d if isinstance(d, list) else [d]:
                    p = ev.get("payload") or {}
                    if not p:
                        continue
                    n += 1
                    sym = p.get("symbol") or ev.get("symbol")
                    if sym:
                        seen.add(sym)
                    if sample is None:
                        sample = ev
    except Exception as e:  # noqa: BLE001
        return -1, {str(e)[:60]}, None
    return n, seen, sample


async def main():
    print(f"probing {WS} for TWAP feed (coin={COIN}, {WAIT_S:.0f}s per attempt)\n")
    hits = []
    async with aiohttp.ClientSession(trust_env=True) as s:
        for topic in TOPICS:
            for symbol in SYMBOLS:
                n, seen, sample = await attempt(s, topic, symbol)
                tag = f"{topic:34s} filter={str(symbol):22s}"
                if n > 0:
                    print(f"  HIT  {tag} frames={n:4d} symbols={sorted(seen)}")
                    hits.append((topic, symbol, sorted(seen), sample))
                elif n == 0:
                    print(f"  ---  {tag} (silent)")
                else:
                    print(f"  ERR  {tag} {list(seen)[0] if seen else ''}")

    print("\n=== sample payloads from hits ===")
    for topic, symbol, seen, sample in hits:
        print(f"\n{topic} filter={symbol}")
        print("  ", json.dumps(sample, separators=(",", ":"))[:500])
    if not hits:
        print("(nothing delivered — the TWAP values may not be on this socket; "
              "next step is the Chainlink stream itself or a REST resolver endpoint)")


if __name__ == "__main__":
    asyncio.run(main())
