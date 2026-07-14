"""Kill-switches. Anything that would page a human running this with real money
halts the strategy here first. Halts are sticky until the process restarts —
a restart is a deliberate human action.
"""
import logging
import time

log = logging.getLogger("risk")


class RiskManager:
    """Sticky halts (loss, winrate, mismatch) need a human restart; transient
    pauses (feed silence) clear themselves when the feed recovers."""

    def __init__(self, cfg, ledger, oracle, spot):
        self.cfg, self.ledger, self.oracle, self.spot = cfg, ledger, oracle, spot
        self._halts = {}
        self._started = time.time()

    def halt(self, scope, reason):
        if scope not in self._halts:
            self._halts[scope] = reason
            self.ledger.event("HALT", f"{scope}: {reason}")
            log.error("HALT %s: %s", scope, reason)

    def halted(self, scope):
        if scope in self._halts or "all" in self._halts:
            return True
        # transient: spot feed silent (only after startup warmup)
        if scope == "snipe" and time.time() - self._started > 60:
            if self.spot.silence() > self.cfg.feed_max_silence_s:
                return True
        return False

    async def run(self):
        import asyncio
        await asyncio.sleep(30)      # warmup: let feeds connect
        while True:
            try:
                self._check()
            except Exception as e:  # noqa: BLE001
                log.exception("risk check failed: %s", e)
            await asyncio.sleep(5)

    def _check(self):
        pnl = self.ledger.realized_pnl_today()
        if pnl < -self.cfg.max_daily_loss:
            self.halt("all", f"daily loss {pnl:.2f} < -{self.cfg.max_daily_loss}")
        wr, n = self.ledger.snipe_trailing(self.cfg.snipe_trailing_n)
        if wr is not None and n >= self.cfg.snipe_trailing_n and wr < self.cfg.snipe_min_winrate:
            self.halt("snipe", f"trailing winrate {wr:.2f} over {n}")
        if self.ledger.mismatches() > 0:
            self.halt("toll", "oracle/exchange winner mismatch detected")
        # oracle staleness is checked at decision time by the strategies; a long
        # outage only skips windows there (transient RPC blips recover)
