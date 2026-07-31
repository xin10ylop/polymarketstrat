"""Run on a new host before starting the bot: python3 -m bot.preflight

Checks every external dependency and the system clock, and prints PASS/FAIL.
"""
import asyncio
import json
import time

import aiohttp

from bot.config import CFG

OK, BAD = "  PASS", "  FAIL"


async def main():
    results = []
    arr = None
    async with aiohttp.ClientSession(trust_env=True) as s:
        # gamma discovery
        from bot.config import slug_for
        now = int(time.time())
        wts = now - now % CFG.window_secs + CFG.window_secs
        slug = slug_for(CFG, wts)
        try:
            async with s.get(f"{CFG.gamma_url}/markets?slug={slug}",
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                arr = await r.json()
            results.append((bool(arr), f"gamma market discovery ({slug})"))
        except Exception as e:  # noqa: BLE001
            results.append((False, f"gamma: {e}"))
        # CLOB ws
        try:
            if arr:
                toks = json.loads(arr[0]["clobTokenIds"])
                async with s.ws_connect(CFG.clob_ws, heartbeat=10) as ws:
                    await ws.send_json({"type": "market", "assets_ids": toks})
                    msg = await asyncio.wait_for(ws.receive(), timeout=10)
                    results.append((msg.type == aiohttp.WSMsgType.TEXT, "CLOB websocket"))
        except Exception as e:  # noqa: BLE001
            results.append((False, f"CLOB ws: {e}"))
        # spot ws + clock skew vs exchange server time
        try:
            async with s.get("https://api.exchange.coinbase.com/time",
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                st = await r.json()
            skew = abs(float(st["epoch"]) - time.time())
            results.append((skew < 0.5, f"clock skew vs coinbase: {skew*1000:.0f}ms "
                            "(install chrony if >500ms)"))
            async with s.ws_connect(CFG.coinbase_ws, heartbeat=10) as ws:
                await ws.send_json({"type": "subscribe", "product_ids": [CFG.coinbase_product],
                                    "channels": ["matches"]})
                got = False
                for _ in range(5):
                    msg = await asyncio.wait_for(ws.receive(), timeout=10)
                    if json.loads(msg.data).get("type") in ("match", "last_match"):
                        got = True
                        break
                results.append((got, "coinbase matches feed"))
        except Exception as e:  # noqa: BLE001
            results.append((False, f"coinbase: {e}"))
        # resolution feed: Polymarket's chainlink data-stream ws
        try:
            # compact separators REQUIRED: the server routes updates by exact
            # filter-string match; spacey JSON gets backfill but zero updates
            sub = {"action": "subscribe", "subscriptions": [{
                "topic": "crypto_prices_chainlink", "type": "update",
                "filters": json.dumps({"symbol": CFG.pm_price_symbol},
                                      separators=(",", ":"))}]}
            async with s.ws_connect(CFG.pm_live_ws, heartbeat=10) as ws:
                await ws.send_json(sub)
                got = None
                for _ in range(6):
                    msg = await asyncio.wait_for(ws.receive(), timeout=10)
                    if msg.type != aiohttp.WSMsgType.TEXT or not msg.data:
                        continue
                    d = json.loads(msg.data)
                    p = d.get("payload") or {}
                    rows = p.get("data") if isinstance(p.get("data"), list) else [p]
                    for r0 in rows:
                        if r0.get("value"):
                            got = float(r0["value"])
                            break
                    if got:
                        break
                results.append((bool(got and got > 0),
                                f"resolution feed (chainlink data stream): BTC/USD={got}"))
        except Exception as e:  # noqa: BLE001
            results.append((False, f"resolution feed ws: {e}"))
    ok = True
    for good, label in results:
        print((OK if good else BAD), label)
        ok &= good
    print("\npreflight:", "ALL GOOD" if ok else "FIX FAILURES BEFORE RUNNING")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
