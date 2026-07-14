"""Chainlink BTC/USD oracle poller (Polygon).

Polls latestRoundData on the aggregator proxy every ~300ms across a rotation of
public RPCs, keeping a (timestamp -> price) ring so window open/close prints can
be sampled at exact boundary seconds — the same sampling that matched the
resolver bit-for-bit in the historical audit.

If every RPC is unreachable and cfg.oracle_allow_spot_fallback is set, the spot
feed's last price is served instead and every reading is flagged degraded=True.
Paper results produced in degraded mode must not be used to qualify the strategy.
"""
import asyncio
import json
import logging
import time
from collections import deque

import aiohttp

log = logging.getLogger("oracle")
_SELECTOR = "0xfeaf968c"  # latestRoundData()


class Oracle:
    def __init__(self, cfg, spot=None):
        self.cfg = cfg
        self.spot = spot                      # optional spot feed for fallback
        self.ring = deque(maxlen=4000)        # (unix_ts_float, price) ~20min at 300ms
        self.last_price = None
        self.last_ts = 0.0
        self.degraded = False
        self._rpc_idx = 0
        self._session = None

    async def run(self):
        self._session = aiohttp.ClientSession(trust_env=True)
        try:
            while True:
                t0 = time.time()
                await self._poll_once()
                await asyncio.sleep(max(0.0, self.cfg.oracle_poll_ms / 1000 - (time.time() - t0)))
        finally:
            await self._session.close()

    async def _poll_once(self):
        rpcs = self.cfg.polygon_rpcs
        for i in range(len(rpcs)):
            url = rpcs[(self._rpc_idx + i) % len(rpcs)]
            try:
                body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                        "params": [{"to": self.cfg.chainlink_btc_usd, "data": _SELECTOR}, "latest"]}
                async with self._session.post(url, json=body,
                                              timeout=aiohttp.ClientTimeout(total=2.5)) as r:
                    if r.status != 200:
                        raise RuntimeError(f"http {r.status}")
                    res = await r.json()
                raw = res["result"][2:]
                words = [raw[j:j + 64] for j in range(0, len(raw), 64)]
                price = int(words[1], 16) / 1e8
                now = time.time()
                self._rpc_idx = (self._rpc_idx + i) % len(rpcs)  # stick with a working RPC
                self._record(now, price, degraded=False)
                return
            except Exception as e:  # noqa: BLE001 - rotate on any RPC failure
                log.debug("rpc %s failed: %s", url, e)
        # all RPCs failed
        if self.cfg.oracle_allow_spot_fallback and self.spot and self.spot.last_price:
            self._record(time.time(), self.spot.last_price, degraded=True)
        else:
            log.warning("oracle: all RPCs failed, no reading this cycle")

    def _record(self, ts, price, degraded):
        if degraded and not self.degraded:
            log.warning("oracle DEGRADED: serving spot prices as pseudo-oracle")
        self.degraded = degraded
        self.last_price, self.last_ts = price, ts
        self.ring.append((ts, price))

    # -- sampling --------------------------------------------------------
    def price_at(self, unix_second):
        """Last oracle print at or before the given second (None if no data)."""
        best = None
        for ts, px in reversed(self.ring):
            if ts <= unix_second:
                best = px
                break
        # if the ring only has newer readings, we cannot know the boundary value
        return best

    def staleness(self):
        return time.time() - self.last_ts if self.last_ts else float("inf")
