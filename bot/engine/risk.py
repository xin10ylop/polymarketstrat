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
        tp, n = self.ledger.snipe_trailing_pnl(self.cfg.snipe_trailing_n)
        if n >= self.cfg.snipe_trailing_n and tp < self.cfg.snipe_trailing_pnl_min:
            self.halt("snipe", f"trailing {n}-fill pnl {tp:.2f} < "
                      f"{self.cfg.snipe_trailing_pnl_min}")
        if self.ledger.mismatches() > 0:
            self.halt("toll", "oracle/exchange winner mismatch detected")
        if self.ledger.unmarked_old_fills() > self.cfg.max_unmarked_fills:
            self.halt("all", "settlement reconciler falling behind "
                      f"({self.ledger.unmarked_old_fills()} unmarked fills)")
        # oracle staleness is checked at decision time by the strategies
