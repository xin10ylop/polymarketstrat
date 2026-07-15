"""Resolution oracle: Polymarket's own Chainlink BTC/USD data-stream feed.

These markets resolve on the Chainlink BTC/USD *data stream* (per the market
description: https://data.chain.link/streams/btc-usd), which Polymarket
publishes at wss://ws-live-data.polymarket.com under topic
"crypto_prices_chainlink". That feed is a 1-second grid of full-precision
prices whose historical archive matched the resolver bit-for-bit
(15,954/15,954 windows). This module consumes it directly:

  - every update carries the SAMPLE second (payload.timestamp, ms, on the 1s
    grid) — so boundary sampling is exact, immune to network/RPC latency;
  - on (re)connect the server sends a backfill of the last ~1-2 minutes,
    which heals short gaps around window boundaries automatically.

The previously-used on-chain Polygon aggregator publishes a round only every
~25-33s (deviation/heartbeat) and historically miscalls ~8.6% of windows —
it must never be used to call winners. It is kept only as telemetry.

degraded = feed silent for > cfg.oracle_max_staleness_s. Strategies must not
trade on a degraded oracle.
"""
import asyncio
import json
import logging
import time

import aiohttp

log = logging.getLogger("oracle")


class Oracle:
    def __init__(self, cfg, spot=None):
        self.cfg = cfg
        self.spot = spot                    # kept for interface compat; unused for pricing
        self.samples = {}                   # sample_second(int) -> price(float)
        self._order = []                    # insertion order for pruning
        self.last_price = None
        self.last_sample_s = 0
        self.last_rx = 0.0                  # wall clock of last received frame

    # ------------------------------------------------------------------
    async def run(self):
        backoff = 1
        # filters MUST be compact JSON: the server routes updates by exact
        # string match, and '{"symbol": "btc/usd"}' (spacey) gets backfill
        # but zero updates — a silent, open, dead subscription
        sub = {"action": "subscribe", "subscriptions": [{
            "topic": "crypto_prices_chainlink", "type": "update",
            "filters": json.dumps({"symbol": self.cfg.pm_price_symbol},
                                  separators=(",", ":")),
        }]}
        while True:
            try:
                async with aiohttp.ClientSession(trust_env=True) as s:
                    # receive_timeout: a connection that stops delivering frames
                    # (even if TCP-alive) is torn down and resubscribed
                    async with s.ws_connect(self.cfg.pm_live_ws, heartbeat=15,
                                            receive_timeout=10) as ws:
                        await ws.send_json(sub)
                        log.info("oracle feed connected (%s %s)",
                                 self.cfg.pm_live_ws, self.cfg.pm_price_symbol)
                        backoff = 1
                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                break
                            if not msg.data:
                                continue
                            self._handle(msg.data)
            except Exception as e:  # noqa: BLE001
                log.warning("oracle feed dropped (%s); reconnect in %ds", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15)

    def _handle(self, raw):
        try:
            d = json.loads(raw)
        except ValueError:
            return
        p = d.get("payload") or {}
        rows = p.get("data") if isinstance(p.get("data"), list) else [p]
        n = 0
        for r in rows:
            sym = r.get("symbol")
            if sym is not None and sym != self.cfg.pm_price_symbol:
                continue          # defense in depth if server-side filtering breaks
            ts_ms, val = r.get("timestamp"), r.get("value")
            if ts_ms is None or val is None:
                continue
            sec = int(ts_ms) // 1000
            if sec not in self.samples:
                self._order.append(sec)
            self.samples[sec] = float(val)
            if sec >= self.last_sample_s:
                self.last_sample_s, self.last_price = sec, float(val)
            n += 1
        if n:
            self.last_rx = time.time()
            if len(self._order) > 4000:      # prune to ~last hour
                for old in self._order[:-3600]:
                    self.samples.pop(old, None)
                self._order = self._order[-3600:]

    # ------------------------------------------------------------------
    def price_at(self, unix_second, exact=True, tolerance=0):
        """Price at the exact sample second (the resolver's own grid).

        exact=True returns None unless that second (or one within `tolerance`
        seconds BEFORE it) was sampled — callers must skip rather than guess.
        """
        unix_second = int(unix_second)
        for back in range(0, max(0, int(tolerance)) + 1):
            px = self.samples.get(unix_second - back)
            if px is not None:
                return px
        if exact:
            return None
        return None

    def staleness(self):
        return time.time() - self.last_rx if self.last_rx else float("inf")

    @property
    def degraded(self):
        return self.staleness() > self.cfg.oracle_max_staleness_s
