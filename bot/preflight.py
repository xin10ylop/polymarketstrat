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
    async with aiohttp.ClientSession(trust_env=True) as s:
        # gamma discovery
        now = int(time.time())
        wts = now - now % 300 + 300
        try:
            async with s.get(f"{CFG.gamma_url}/markets?slug={CFG.slug_prefix}-{wts}",
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                arr = await r.json()
            results.append((bool(arr), f"gamma market discovery ({CFG.slug_prefix}-{wts})"))
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
                await ws.send_json({"type": "subscribe", "product_ids": ["BTC-USD"],
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
        # chainlink RPC
        got_rpc = False
        for url in CFG.polygon_rpcs:
            try:
                body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                        "params": [{"to": CFG.chainlink_btc_usd, "data": "0xfeaf968c"},
                                   "latest"]}
                async with s.post(url, json=body,
                                  timeout=aiohttp.ClientTimeout(total=5)) as r:
                    res = await r.json()
                px = int(res["result"][2:][64:128], 16) / 1e8
                results.append((px > 1000, f"chainlink via {url}: BTC/USD={px:.2f}"))
                got_rpc = True
                break
            except Exception:  # noqa: BLE001
                continue
        if not got_rpc:
            results.append((False, "chainlink: ALL polygon RPCs unreachable "
                            "(bot would need ORACLE_SPOT_FALLBACK=1 -> degraded)"))
    ok = True
    for good, label in results:
        print((OK if good else BAD), label)
        ok &= good
    print("\npreflight:", "ALL GOOD" if ok else "FIX FAILURES BEFORE RUNNING")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
