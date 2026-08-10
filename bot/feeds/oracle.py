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
            # retention must cover reconciliation of a FULL window plus its
            # grace period (audit M3: fixed 1h retention silently killed the
            # 1h family's open-sample lookup at reconcile time)
            keep = max(3600, 2 * self.cfg.window_secs + 1200)
            if len(self._order) > keep + 400:
                for old in self._order[:-keep]:
                    self.samples.pop(old, None)
                self._order = self._order[-keep:]

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

    # ---- rolling TWAP (2026-08-07 resolution-rule change) -------------
    # Polymarket now resolves the 5m/15m families on Chainlink's rolling TWAP
    # streams (30s for 5m windows, 60s for 15m): "TWAP at close >= TWAP at
    # open". No TWAP topic exists on the live-data socket, so we reconstruct
    # it from the same 1s Chainlink grid this feed already consumes —
    # verified against official outcomes at 100% on 76 BTC windows across
    # both families (bot/twap_verify.py, gate 99%).
    def twap_at(self, unix_second, n, edge="left"):
        """Rolling n-second TWAP as of `unix_second`.

        edge="left"  -> mean over [t-n, t)   |  edge="right" -> mean over (t-n, t]
        Both scored 100%; they can only differ when a single boundary sample
        moves the mean across the strike, i.e. in windows too close to call.
        Returns (value, coverage) where coverage = samples present / n.
        """
        t = int(unix_second)
        lo, hi = (t - n, t) if edge == "left" else (t - n + 1, t + 1)
        vals = [self.samples[s] for s in range(lo, hi) if s in self.samples]
        if not vals:
            return None, 0.0
        return sum(vals) / len(vals), len(vals) / float(n)

    def twap_winner(self, open_s, close_s, n, min_coverage=0.9):
        """The official rule's winner, or None when it is not callable.

        None means one of: thin sample coverage, or the two boundary
        conventions disagree — a sub-basis-point tie we must not adjudicate.
        Callers treat None as 'no opinion', never as a mismatch.
        """
        calls = []
        for edge in ("left", "right"):
            c, cov_c = self.twap_at(close_s, n, edge)
            o, cov_o = self.twap_at(open_s, n, edge)
            if c is None or o is None or min(cov_c, cov_o) < min_coverage:
                return None
            calls.append("up" if c >= o else "down")
        return calls[0] if calls[0] == calls[1] else None

    def twap_known(self, close_s, n, upto_s):
        """The part of the closing TWAP window that is ALREADY DETERMINED at
        `upto_s`: (sum, n_seconds_present, n_seconds_elapsed).

        This is what makes the new rule easier to call than the old one: at
        5s to go, 25 of a 30s average is already history. Missing seconds
        inside the elapsed range are reported so the caller can refuse on
        thin coverage rather than impute silently.
        """
        lo = int(close_s) - n
        hi = min(int(upto_s), int(close_s))       # exclusive
        if hi <= lo:
            return 0.0, 0, 0
        vals = [self.samples[s] for s in range(lo, hi) if s in self.samples]
        return sum(vals), len(vals), hi - lo

    def twap_carry(self, close_s, n, upto_s, tail=None, lookback=120):
        """The closing TWAP over [close_s-n, close_s) with holes CARRIED
        FORWARD: every second with no print of its own takes the most recent
        print at or before it. Returns (value, n_present, n_elapsed).

        WHY THIS EXISTS, MEASURED. The 1s grid is not complete — 62.6% of btc
        windows are missing at least one of the 27 seconds available at T-3,
        median 26 of 27. twap_known reports only a SUM, so its caller can do
        nothing with a hole but rescale the present seconds' mean across the
        whole window. That fills the gap with the window AVERAGE when what
        actually stood there was the price one second earlier, and over a
        window with any drift those are different numbers.

        Punching 377 real hole patterns into the 227 complete btc windows and
        comparing against the complete-grid tilt: rescaling is off by 0.036bp
        on average, carrying by 0.009bp — 4x, and 5.5x on eth (0.083 vs
        0.015). In the worst bucket rescaling drifts 0.377bp against a 0.5bp
        trading gate, which is enough to flip a marginal call on its own,
        while carrying stays at 0.008bp.

        (The mechanism is NOT what I guessed. I expected these to be
        deduplicated no-change reports, which would make carrying trivially
        correct. They are not: the price is identical across an isolated hole
        0.7% of the time versus 0.4% across a present second, i.e. no
        different. Carrying wins for a plainer reason — the print one second
        back is a local estimate and the window mean is a global one.)

        Reaches BACKWARD only, never forward, so nothing here can see a price
        that did not exist at upto_s.

        n_present counts seconds that had a print of their OWN in the elapsed
        range; it is the honest coverage number to gate on, and it is 0 for a
        feed blackout, which is the case that must always refuse.
        """
        lo = int(close_s) - n
        hi = min(int(upto_s), int(close_s))       # exclusive
        if hi <= lo:
            return None, 0, 0
        carry = None
        for back in range(1, int(lookback) + 1):  # seed from before the window
            if (lo - back) in self.samples:
                carry = self.samples[lo - back]
                break
        total, n_present = 0.0, 0
        for s in range(lo, hi):
            v = self.samples.get(s)
            if v is not None:
                carry = v
                n_present += 1
            if carry is None:
                return None, 0, hi - lo           # nothing to carry from yet
            total += carry
        if not n_present and tail is None:
            return None, 0, hi - lo
        # the not-yet-elapsed tail is the caller's latest print, the same
        # imputation the elapsed holes just got
        t = tail if tail is not None else carry
        total += (int(close_s) - hi) * t
        return total / n, n_present, hi - lo

    def staleness(self):
        return time.time() - self.last_rx if self.last_rx else float("inf")

    def clock_ok(self):
        """Free runtime clock-skew guard: the feed's sample second is stamped
        by the server on the 1s grid, so (local receive time - sample second)
        normally sits in [0, ~2]s. A drifting local clock shifts it. Outside a
        generous band, refuse to trade — the strategy's whole timing model
        (eval window, the -1.5s cutoff) rides on the local clock."""
        if not self.last_rx:
            return False
        lag = self.last_rx - self.last_sample_s
        return -0.75 <= lag <= 3.0

    @property
    def degraded(self):
        return self.staleness() > self.cfg.oracle_max_staleness_s
