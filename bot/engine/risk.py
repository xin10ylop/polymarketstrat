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
        self._shadow_day = None      # UTC day whose shadow stop is recorded
        self._shadow_trail_day = None  # UTC day whose shadow trail-trip is
        # RE-DERIVE HALTS BEFORE ANYONE CAN TRADE (audit 2026-08-13). run()
        # sleeps 30s before its first _check, so a restarted bot traded for
        # ~30-35s before a mismatch or daily-loss halt re-armed — every crash
        # loop reopened the window. All _check inputs are ledger reads, so
        # doing it synchronously here costs milliseconds.
        try:
            self._check()
        except Exception:  # noqa: BLE001
            log.exception("startup risk check failed (continuing; the "
                          "5s loop will retry)")

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
            # paper: a DAILY stop lifts with the new day. live: STICKY — real
            # money resumes only when a human restarts (audit 2026-07-30 #7).
            next_utc_day = (int(time.time() // 86400) + 1) * 86400
            if self.cfg.mode != "live" and self.cfg.paper_shadow_daily_stop:
                # A CAPITAL RULE ON A MEASUREMENT INSTRUMENT DESTROYS THE
                # MEASUREMENT. In paper there is no capital to preserve, and
                # stopping on bad days censors the sample in one direction:
                # losing runs are truncated at the breaker while winning runs
                # record in full, biasing every win rate and every drawdown
                # optimistically. Found 2026-08-11 — btc 5m dark 5h, eth 5m
                # dark 6h47m, and the ledgers gave no hint either had stopped.
                # Recorded instead of enforced, because an UNCENSORED record
                # can always be censored in analysis while a censored one can
                # never be repaired. Every fill after this event is what the
                # breaker would have vetoed, so live-equivalent P&L stays
                # exactly reconstructible.
                day = int(time.time() // 86400)
                if self._shadow_day != day:
                    self._shadow_day = day
                    self.ledger.event(
                        "SHADOW_HALT",
                        f"all: daily loss {pnl:.2f} < -{self.cfg.max_daily_loss}")
                    log.warning(
                        "SHADOW daily stop: loss %.2f < -%.2f — live would have "
                        "halted here; paper keeps trading so the day is fully "
                        "recorded (PAPER_SHADOW_DAILY_STOP=0 to enforce)",
                        pnl, self.cfg.max_daily_loss)
            else:
                self.halt("all", f"daily loss {pnl:.2f} < -{self.cfg.max_daily_loss}",
                          until=None if self.cfg.mode == "live" else next_utc_day)
        if self.cfg.mode == "live":
            # cumulative drawdown: a dead edge losing one daily-stop at a time
            # never trips the daily/trailing breakers individually; this does.
            lt = self.ledger.lifetime_pnl()
            dd_floor = -self.cfg.live_max_drawdown_frac * self.cfg.bankroll
            if lt < dd_floor:
                self.halt("all", f"cumulative pnl {lt:.2f} < {dd_floor:.2f} "
                          f"({self.cfg.live_max_drawdown_frac:.0%} of bankroll)")
        tp, n = self.ledger.snipe_trailing_pnl(self.cfg.snipe_trailing_n)
        trail_floor = -self.cfg.snipe_trailing_pnl_frac * self.cfg.max_daily_loss
        if (n >= self.cfg.snipe_trailing_n and tp < trail_floor
                and (self.ledger.snipe_fills_since_trailing_halt()
                     >= self.cfg.snipe_trailing_rearm_fills)):
            self.halt("snipe", f"trailing {n}-fill pnl {tp:.2f} < {trail_floor:.2f}")
        # preopen fast-bleed breaker (launch blocker #2, 2026-08-19): a bad
        # half hour must not ride the whole daily stop down with real money.
        # The ledger window is bounded by the last preopen trailing HALT, so
        # after a human restart it cannot re-trip until N fresh decisions
        # exist. Paper records instead of enforcing (same reasoning as the
        # shadow daily stop): the SHADOW_TRAIL events ARE the calibration —
        # trip-days per week at this floor, measured before launch.
        tpp, npp = self.ledger.preopen_trailing_pnl(self.cfg.preopen_trailing_n)
        p_floor = -self.cfg.preopen_trailing_pnl_frac * self.cfg.max_daily_loss
        if npp >= self.cfg.preopen_trailing_n and tpp < p_floor:
            if self.cfg.mode == "live":
                self.halt("preopen", f"trailing {npp}-decision pnl "
                          f"{tpp:.2f} < {p_floor:.2f}")
            else:
                day = int(time.time() // 86400)
                if self._shadow_trail_day != day:
                    self._shadow_trail_day = day
                    self.ledger.event(
                        "SHADOW_TRAIL",
                        f"preopen: trailing {npp}-decision pnl {tpp:.2f} "
                        f"< {p_floor:.2f}")
                    log.warning(
                        "SHADOW fast-bleed: trailing %d-decision pnl %.2f < "
                        "%.2f — live would halt preopen here; paper keeps "
                        "trading and records the trip", npp, tpp, p_floor)
        if self.cfg.mode == "live":
            # LIVE STICKY HALTS MUST SURVIVE A RESTART (audit 2026-08-13).
            # Ambiguous fills, unexpected order errors and reconciler
            # breaches halted only in memory: systemd Restart= could lift a
            # halt whose own text says "reconcile before restarting". The
            # triggering events persist in the ledger; a human acknowledges
            # them with scripts/ack_incident.py after reconciling against
            # the exchange, and only that ack releases the halt.
            pending = self.ledger.needs_ack()
            if pending is not None:
                self.halt("all", "unacknowledged live incident on the ledger "
                          f"(event at {time.strftime('%m-%d %H:%M', time.gmtime(pending))} UTC) "
                          "— reconcile against the exchange, then run "
                          "scripts/ack_incident.py")
        if self.ledger.mismatches() > 0:
            # scope "all": a mismatch means our oracle read disagrees with the
            # exchange — EVERY strategy must stop, not just the toll (which is
            # not even running in live; audit 2026-07-30 finding #6)
            self.halt("all", "oracle/exchange winner mismatch detected")
        if self.ledger.unmarked_old_fills() > self.cfg.max_unmarked_fills:
            # TRANSIENT condition (gamma outage / late outcome) that the
            # settlement healer repairs — time-limited halt, re-trips while
            # the backlog persists, self-lifts once it clears
            self.halt("all", "settlement reconciler falling behind "
                      f"({self.ledger.unmarked_old_fills()} unmarked fills)",
                      until=time.time() + 900)
        # oracle staleness is checked at decision time by the strategies
