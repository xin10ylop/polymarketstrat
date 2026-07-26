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

    def halt(self, scope, reason, until=None):
        """until=None -> sticky (human restart required); otherwise the halt
        lifts itself at that unix time (e.g. a daily breaker at next UTC day)."""
        if scope not in self._halts:
            self._halts[scope] = (reason, until)
            self.ledger.event("HALT", f"{scope}: {reason}")
            log.error("HALT %s: %s%s", scope, reason,
                      f" (auto-lifts {time.strftime('%H:%M UTC', time.gmtime(until))})"
                      if until else "")

    def _scope_halted(self, scope):
        h = self._halts.get(scope)
        if h is None:
            return False
        reason, until = h
        if until is not None and time.time() >= until:
            del self._halts[scope]
            self.ledger.event("HALT_LIFTED", f"{scope}: {reason}")
            log.warning("halt lifted (%s): %s", scope, reason)
            return False
        return True

    def halted(self, scope):
        if self._scope_halted(scope) or self._scope_halted("all"):
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
            next_utc_day = (int(time.time() // 86400) + 1) * 86400
            self.halt("all", f"daily loss {pnl:.2f} < -{self.cfg.max_daily_loss}",
                      until=next_utc_day)   # a DAILY stop lifts with the new day
        tp, n = self.ledger.snipe_trailing_pnl(self.cfg.snipe_trailing_n)
        trail_floor = -self.cfg.snipe_trailing_pnl_frac * self.cfg.max_daily_loss
        if (n >= self.cfg.snipe_trailing_n and tp < trail_floor
                and (self.ledger.snipe_fills_since_trailing_halt()
                     >= self.cfg.snipe_trailing_rearm_fills)):
            self.halt("snipe", f"trailing {n}-fill pnl {tp:.2f} < {trail_floor:.2f}")
        if self.ledger.mismatches() > 0:
            self.halt("toll", "oracle/exchange winner mismatch detected")
        if self.ledger.unmarked_old_fills() > self.cfg.max_unmarked_fills:
            self.halt("all", "settlement reconciler falling behind "
                      f"({self.ledger.unmarked_old_fills()} unmarked fills)")
        # oracle staleness is checked at decision time by the strategies
