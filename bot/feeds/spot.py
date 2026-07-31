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
        """(close, bar_second) at or before `second` ((None, None) if unknown).

        Returning the bar's own second lets the caller price staleness: on
        sparse tapes (SOL p90 inter-trade gap ~5s) the latest bar can be many
        seconds older than requested, and pricing it as fresh understates the
        true horizon (audit F1)."""
        for s, c, _real in reversed(self.bars):
            if s <= second:
                return c, s
        return None, None

    def vol(self):
        """Std of 1s log returns over trailing real (non-gap-filled) bars.

        Synthetic flat bars would inject zero returns and understate vol,
        inflating the snipe's confidence exactly when the tape is thin.
        """
        bars = list(self.bars)[-self.cfg.vol_window_s:]
        rets = []
        for i in range(1, len(bars)):
            s0, c0, r0 = bars[i - 1]
            s1, c1, r1 = bars[i]
            if r0 and r1 and s1 == s0 + 1 and c0 > 0:
                rets.append(math.log(c1 / c0))
        if len(rets) < 30:
            return None
        return statistics.pstdev(rets)

    def basis(self):
        """Rolling median of oracle/spot over the basis window.

        Age-filtered: the deque maxlen bounds sample COUNT, but on sparse
        tapes those samples can span far longer than the design window
        (audit F8a) — a stale correction is worse than none."""
        now = time.time()
        vals = [b for s, b in self.basis_samples if now - s <= 2 * self.cfg.basis_window_s]
        if len(vals) < 10:
            return None
        return statistics.median(vals)

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
            self.bars.append((self._cur_sec, self._cur_close, True))
            # gap-filled bars are flagged synthetic and excluded from vol
            for missing in range(self._cur_sec + 1, min(sec, self._cur_sec + 30)):
                self.bars.append((missing, self._cur_close, False))
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
                # clean server-side close: still pay a delay — a venue that
                # accepts-then-closes must not see a tight reconnect loop
                await asyncio.sleep(backoff)
                backoff = 1

    async def _run_coinbase(self, session):
        async with session.ws_connect(self.cfg.coinbase_ws, heartbeat=15) as ws:
            await ws.send_json({"type": "subscribe",
                                "product_ids": [self.cfg.coinbase_product],
                                "channels": ["matches"]})
            log.info("coinbase feed connected (%s)", self.cfg.coinbase_product)
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
