"""Spot BTC feed -> 1s bars, realized vol, and oracle/spot basis estimator.

Coinbase BTC-USD (matches channel) is the default: it is USD-quoted (tiny basis
vs Chainlink BTC/USD) and not geo-blocked on US servers. Binance aggTrade is
supported for non-US deployments. Both produce the same 1s close series the
strategy was validated on; the basis estimator absorbs feed-level offsets.
"""
import asyncio
import json
import logging
import math
import statistics
import time
from collections import deque

import aiohttp

log = logging.getLogger("spot")


class SpotFeed:
    def __init__(self, cfg, oracle_ref=None):
        self.cfg = cfg
        self.oracle_ref = oracle_ref          # set after Oracle construction
        self.last_price = None
        self.last_trade_ts = 0.0
        self.bars = deque(maxlen=cfg.vol_window_s + 120)   # (second, close)
        self._cur_sec = None
        self._cur_close = None
        self.basis_samples = deque(maxlen=cfg.basis_window_s)  # (second, oracle/spot)

    # -- public estimators -------------------------------------------------
    def close_at(self, second):
        """1s close at or before `second` (None if unknown)."""
        for s, c in reversed(self.bars):
            if s <= second:
                return c
        return None

    def vol(self):
        """Std of 1s log returns over the trailing window (per sqrt-second)."""
        closes = [c for _, c in self.bars][-self.cfg.vol_window_s:]
        if len(closes) < 60:
            return None
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))
                if closes[i - 1] > 0]
        if len(rets) < 30:
            return None
        return statistics.pstdev(rets)

    def basis(self):
        """Rolling median of oracle/spot over the basis window (1.0 if unknown)."""
        if len(self.basis_samples) < 10:
            return None
        return statistics.median(b for _, b in self.basis_samples)

    def silence(self):
        return time.time() - self.last_trade_ts if self.last_trade_ts else float("inf")

    # -- ingestion ----------------------------------------------------------
    def _on_trade(self, price, ts):
        self.last_price = price
        self.last_trade_ts = time.time()
        sec = int(ts)
        if self._cur_sec is None:
            self._cur_sec, self._cur_close = sec, price
        elif sec > self._cur_sec:
            self.bars.append((self._cur_sec, self._cur_close))
            # fill gaps so vol/back-sampling sees a contiguous grid
            for missing in range(self._cur_sec + 1, min(sec, self._cur_sec + 30)):
                self.bars.append((missing, self._cur_close))
            self._cur_sec, self._cur_close = sec, price
        else:
            self._cur_close = price
        # basis sample once per second
        o = self.oracle_ref
        if o and o.last_price and not o.degraded and price > 0:
            if not self.basis_samples or self.basis_samples[-1][0] != sec:
                self.basis_samples.append((sec, o.last_price / price))

    async def run(self):
        backoff = 1
        while True:
            try:
                async with aiohttp.ClientSession(trust_env=True) as s:
                    if self.cfg.spot_feed == "binance":
                        await self._run_binance(s)
                    else:
                        await self._run_coinbase(s)
            except Exception as e:  # noqa: BLE001
                log.warning("spot feed dropped (%s); reconnect in %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            else:
                backoff = 1

    async def _run_coinbase(self, session):
        async with session.ws_connect(self.cfg.coinbase_ws, heartbeat=15) as ws:
            await ws.send_json({"type": "subscribe", "product_ids": ["BTC-USD"],
                                "channels": ["matches"]})
            log.info("coinbase feed connected")
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                d = json.loads(msg.data)
                if d.get("type") in ("match", "last_match"):
                    ts = time.mktime(time.strptime(d["time"][:19], "%Y-%m-%dT%H:%M:%S"))
                    # coinbase timestamps are UTC; mktime assumes local -> use calendar
                    import calendar
                    ts = calendar.timegm(time.strptime(d["time"][:19], "%Y-%m-%dT%H:%M:%S"))
                    self._on_trade(float(d["price"]), ts)

    async def _run_binance(self, session):
        async with session.ws_connect(self.cfg.binance_ws, heartbeat=15) as ws:
            log.info("binance feed connected")
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                d = json.loads(msg.data)
                if "p" in d:
                    self._on_trade(float(d["p"]), d["T"] / 1000.0)
