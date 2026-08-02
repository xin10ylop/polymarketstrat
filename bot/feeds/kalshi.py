"""Kalshi market-data telemetry feed (btc/eth 15m families only).

READ-ONLY and UNAUTHENTICATED: api.elections.kalshi.com serves the market
list and full order book without auth (verified 2026-08-02, 0.2-0.5s).
The bot NEVER trades Kalshi. This feed exists purely to LOG Kalshi's
concurrent same-window price alongside our own take attempts — runbook
2026-08-02 addendum: across 5,445 matched Jun30-Jul29 windows, Kalshi
>=0.985 vs <0.95 separated 100%-win Polymarket 15m entries from 13-58%-win
ones. Phase 1 is telemetry only; any veto comes later, from evidence
measured on OUR OWN fills.

Failure isolation: every error degrades to state() -> None. The strategy
never waits on, retries for, or changes behavior because of this feed.
"""
import asyncio
import calendar
import logging
import time

import aiohttp

log = logging.getLogger("kalshi")

_SERIES = {"btc": "KXBTC15M", "eth": "KXETH15M"}


class KalshiFeed:
    def __init__(self, cfg):
        self.cfg = cfg
        self.series = _SERIES[cfg.coin]
        self.base = "https://api.elections.kalshi.com/trade-api/v2"
        self._ticker = None          # (close_epoch, kalshi ticker)
        self._state = None           # (close_epoch, yes_bid, yes_ask, mono_ts)

    def state(self, close_epoch):
        """(yes_bid, yes_ask, age_s) for the window closing at close_epoch,
        or None if we don't have a fresh matching read. Never raises."""
        try:
            s = self._state
            if s is None or s[0] != close_epoch:
                return None
            age = time.monotonic() - s[3]
            if age > 6.0:
                return None
            return {"k_bid": s[1], "k_ask": s[2], "k_age": round(age, 1)}
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _best(levels):
        """Best (highest) bid from a Kalshi orderbook side. Sides list resting
        BIDS as [price, size]; dollars endpoints use strings ('0.9900'),
        legacy uses integer cents. Empty/None side -> None."""
        best = None
        for lvl in levels or []:
            try:
                px = float(lvl[0])
            except (TypeError, ValueError, IndexError):
                continue
            if px > 1.0:          # integer cents
                px /= 100.0
            if not 0.0 < px < 1.0:
                continue          # sentinel/stub level, never a price
            if best is None or px > best:
                best = px
        return best

    async def _discover(self, s, close_epoch):
        url = (f"{self.base}/markets?series_ticker={self.series}"
               f"&status=open&limit=20")
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
            arr = (await r.json()).get("markets", [])
        for m in arr:
            ct = m.get("close_time", "")
            try:
                ep = calendar.timegm(time.strptime(ct.split(".")[0].rstrip("Z"),
                                                   "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                continue
            if ep == close_epoch:
                self._ticker = (close_epoch, m["ticker"])
                log.info("kalshi window bound: %s (close %s)", m["ticker"], ct)
                return
        self._ticker = (close_epoch, None)   # looked, not found — don't re-spam

    async def run(self):
        async with aiohttp.ClientSession(trust_env=True) as s:
            while True:
                delay = 5.0
                try:
                    now = time.time()
                    close_epoch = int(now - now % self.cfg.window_secs
                                      + self.cfg.window_secs)
                    sec_left = close_epoch - now
                    if self._ticker is None or self._ticker[0] != close_epoch:
                        await self._discover(s, close_epoch)
                    tkr = self._ticker[1] if self._ticker else None
                    if tkr is None:
                        # market not listed (yet) — retry discovery lazily
                        if sec_left < self.cfg.window_secs - 30:
                            self._ticker = None
                        await asyncio.sleep(5.0)
                        continue
                    url = f"{self.base}/markets/{tkr}/orderbook"
                    async with s.get(url, timeout=aiohttp.ClientTimeout(total=4)) as r:
                        ob = await r.json()
                    ob = ob.get("orderbook_fp") or ob.get("orderbook") or {}
                    yes_bid = self._best(ob.get("yes_dollars") or ob.get("yes"))
                    no_bid = self._best(ob.get("no_dollars") or ob.get("no"))
                    yes_ask = round(1.0 - no_bid, 4) if no_bid is not None else None
                    self._state = (close_epoch, yes_bid, yes_ask, time.monotonic())
                    # 1s cadence in the endgame (where our takes happen),
                    # relaxed earlier — polite to their public API
                    delay = 1.0 if sec_left <= 150 else 5.0
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    self._state = None
                    log.debug("kalshi poll failed: %s", e)
                    delay = 10.0
                await asyncio.sleep(delay)
