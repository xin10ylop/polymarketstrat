"""Discover what Polymarket's live-data socket publishes.

  venv/bin/python -m bot.twap_probe            # btc
  COIN=eth venv/bin/python -m bot.twap_probe

WHY: on 2026-08-07 00:00 UTC Polymarket switched the 5m/15m crypto up-down
markets from the Chainlink spot data stream to ROLLING TWAP streams
(btc-usd-twap-30s for 5m windows, btc-usd-twap-60s for 15m) and changed the
comparison to "TWAP at close vs TWAP at open". Our oracle reads the spot
stream, so it computes the wrong quantity. Round 1 of this probe (08-08)
found NO twap topic under the obvious names — only crypto_prices_chainlink
(1s Chainlink grid, symbols btc/usd, eth/usd, ...) and crypto_prices
(exchange spot, btcusdt, ...). This version sweeps a wider topic list
cheaply: phase 1 asks each candidate topic WITHOUT a filter (a live topic
answers, a nonexistent one is silent), phase 2 lists the symbols carried by
whatever answered.

Read-only: subscribes and prints, nothing else.
"""
import asyncio
import json
import os
import time

import aiohttp

WS = "wss://ws-live-data.polymarket.com"
COIN = os.environ.get("COIN", "btc").lower()

TOPICS = [
    # known-good baselines (keep: they prove the probe itself works)
    "crypto_prices_chainlink",
    "crypto_prices",
    # twap candidates
    "crypto_prices_chainlink_twap",
    "crypto_prices_chainlink_twap_30s",
    "crypto_prices_chainlink_twap30s",
    "crypto_prices_chainlink_twap_60s",
    "crypto_prices_chainlink_30s",
    "crypto_prices_twap",
    "crypto_prices_twap_30s",
    "crypto_prices_twap30s",
    "chainlink_twap",
    "chainlink_prices_twap",
    "crypto_twap",
    "prices_twap",
    "twap_prices",
]

PHASE1_S = 6.0
PHASE2_S = 20.0


def sub_msg(topic, symbol=None):
    s = {"topic": topic, "type": "update"}
    if symbol is not None:
        s["filters"] = json.dumps({"symbol": symbol}, separators=(",", ":"))
    return {"action": "subscribe", "subscriptions": [s]}


async def listen(session, topic, symbol, seconds):
    """Returns (frames, symbols seen, one sample event) or (-1, {err}, None)."""
    seen, sample, n = set(), None, 0
    try:
        async with session.ws_connect(WS, heartbeat=15,
                                      receive_timeout=seconds + 3) as ws:
            await ws.send_json(sub_msg(topic, symbol))
            end = time.time() + seconds
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
        return -1, {str(e)[:70]}, None
    return n, seen, sample


async def main():
    print(f"probe {WS}  (coin={COIN})")
    print(f"phase 1: {len(TOPICS)} topics x {PHASE1_S:.0f}s, no filter\n")
    live = []
    async with aiohttp.ClientSession(trust_env=True) as s:
        for topic in TOPICS:
            n, seen, sample = await listen(s, topic, None, PHASE1_S)
            if n > 0:
                print(f"  LIVE     {topic:36s} frames={n:4d} symbols={sorted(seen)}")
                live.append((topic, sample))
            elif n == 0:
                print(f"  silent   {topic:36s} (topic does not exist / publishes nothing)")
            else:
                print(f"  ERROR    {topic:36s} {list(seen)[0]}")

        print(f"\nphase 2: {PHASE2_S:.0f}s on each live topic, full symbol census")
        for topic, _ in live:
            n, seen, sample = await listen(s, topic, None, PHASE2_S)
            print(f"\n  {topic}: frames={n} symbols={sorted(seen)}")
            if sample:
                print("   sample:", json.dumps(sample, separators=(",", ":"))[:400])
            twap = [x for x in seen if "twap" in str(x).lower()]
            print(f"   TWAP-looking symbols: {twap or 'NONE'}")

    print("\nVERDICT: if no topic/symbol carries TWAP, the resolver's TWAP must be")
    print("reconstructed from the 1s crypto_prices_chainlink grid we already get.")
    print("Next: bot/twap_record.py to capture that grid, then bot/twap_verify.py")
    print("to score the reconstruction against official outcomes before any")
    print("oracle change.")


if __name__ == "__main__":
    asyncio.run(main())
