"""S2 — basis-corrected oracle-lag terminal snipe.

From T-6s, every 200ms: take the spot close at (now - signal_lag), correct it by
the rolling oracle/spot basis, and price the binary against the exact oracle
open K. If P(win) >= 0.995 while a standing ask <= 0.97 with size remains on
that side, cross it — capped at min(ask, 250 shares): bigger late asks are
systematically informed (EV collapses above the cap in validation).

The signal lag is enforced deliberately: the backtest was validated on 1s-old
data, so acting on anything fresher would make paper results *optimistic*.
"""
import asyncio
import logging
import math
import time

log = logging.getLogger("snipe")


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


class SnipeStrategy:
    def __init__(self, cfg, clob, oracle, spot, executor, ledger, risk):
        self.cfg, self.clob, self.oracle, self.spot = cfg, clob, oracle, spot
        self.exec, self.ledger, self.risk = executor, ledger, risk
        # telemetry: proves "evaluated, no edge" vs "not evaluating at all"
        self.evals = 0          # ticks where an fv was actually computed
        self.no_data = 0        # ticks skipped for missing feed data
        self.near_misses = 0    # fv extreme but no tradeable ask on that side
        self.signals = 0        # takes attempted
        self.recheck_fail = 0   # ask vanished during the live-latency recheck
        self.retries = 0        # 2nd+ attempts within one window
        self.last_fv = None
        self._wstate = None     # (wts, {attempts, shares, cost}) per-window budget
        self._started = time.time()

    async def run(self):
        T = self.cfg.window_secs
        while True:
            now = time.time()
            wts = int(now - now % T)
            eval_start = wts + T + self.cfg.snipe_eval_from_s
            if now < eval_start:
                await asyncio.sleep(min(eval_start - now, 1.0))
                continue
            if now >= wts + T + self.cfg.snipe_eval_until_s:
                await asyncio.sleep(0.2)
                continue
            if self.cfg.snipe_enabled and not self.risk.halted("snipe"):
                try:
                    if await self._evaluate(wts):
                        # window budget exhausted: sleep to the next one
                        await asyncio.sleep(max(0.0, wts + T - time.time()))
                        continue
                except Exception as e:  # noqa: BLE001
                    log.exception("snipe eval failed: %s", e)
            await asyncio.sleep(self.cfg.snipe_poll_s)

    def _window_budget(self, wts):
        if self._wstate is None or self._wstate[0] != wts:
            self._wstate = (wts, {"attempts": 0, "shares": 0.0, "cost": 0.0})
        return self._wstate[1]

    async def _evaluate(self, wts):
        """Returns True when this window's budget is exhausted (stop polling)."""
        T = self.cfg.window_secs
        w = self._window_budget(wts)
        if (w["attempts"] >= self.cfg.snipe_max_attempts
                or w["shares"] >= self.cfg.snipe_max_clip - 1
                or w["cost"] >= self.cfg.snipe_window_max_cost):
            return True
        mk = self.clob.market_for(wts)
        if mk is None or self.oracle.degraded:
            return False
        # cold-start guard: immature vol/basis estimators produce
        # garbage-confident signals in the first minutes after a (re)start
        if (time.time() - self._started < self.cfg.snipe_warmup_s
                or len(self.spot.basis_samples) < 45):
            self.no_data += 1
            return False
        # exact open print from the resolution feed (tolerate <=2s backfill lag)
        k = self.oracle.price_at(wts, exact=True, tolerance=2)
        if k is None or k <= 0:
            return False
        sig_t = time.time() - self.cfg.snipe_signal_lag_s
        s_lag = self.spot.close_at(sig_t)
        vol = self.spot.vol()
        basis = self.spot.basis()
        if s_lag is None or vol is None or basis is None:
            self.no_data += 1
            return False
        tau = wts + T - sig_t
        if tau <= 0:
            return False
        s_adj = s_lag * basis
        fv = norm_cdf(math.log(s_adj / k) / (max(vol, self.cfg.snipe_vol_floor) * math.sqrt(tau)))
        self.evals += 1
        self.last_fv = fv
        side = "up" if fv >= self.cfg.snipe_fv_min else (
            "down" if fv <= 1 - self.cfg.snipe_fv_min else None)
        if side is None:
            return False
        token = mk.token_up if side == "up" else mk.token_down
        st = self.clob.state(token)
        if (st is None or not st.book_fresh(self.cfg.book_max_age_s)
                or st.best_ask is None
                or st.best_ask > self.cfg.snipe_ask_max
                or st.best_ask < self.cfg.snipe_price_floor
                or st.best_ask_size < self.cfg.snipe_min_ask_size
                or st.best_ask_size > self.cfg.snipe_skip_ask_above):
            self.near_misses += 1
            return False
        w["attempts"] += 1
        if w["attempts"] > 1:
            self.retries += 1
        if self.cfg.snipe_take_recheck_s > 0 and self.cfg.mode != "live":
            # PAPER-ONLY live-fidelity gate: a real order needs ~network + 250ms
            # exchange hold to arrive; only fill if the ask is still there
            # afterwards (~82% of instantly-visible asks are gone by then —
            # audited). A failed recheck consumes an attempt, like a missed FAK.
            # In live mode the latency is REAL, so the sleep must not run:
            # the FAK goes out immediately and the exchange decides the race.
            await asyncio.sleep(self.cfg.snipe_take_recheck_s)
            st = self.clob.state(token)
            if (st is None or not st.book_fresh(self.cfg.book_max_age_s)
                    or st.best_ask is None
                    or st.best_ask > self.cfg.snipe_ask_max
                    or st.best_ask_size < self.cfg.snipe_min_ask_size):
                self.recheck_fail += 1
                return False
        remaining = min(self.cfg.snipe_max_clip - w["shares"],
                        (self.cfg.snipe_window_max_cost - w["cost"]) / max(st.best_ask, 0.01))
        if w["attempts"] == 1:
            remaining = min(remaining, self.cfg.snipe_first_clip)
        order = self.exec.take(wts, "snipe", token, self.cfg.snipe_ask_max, remaining)
        if order:
            self.signals += 1
            w["shares"] += order.filled
            w["cost"] += order.filled * order.price
            log.info("w%s SNIPE %s att=%d fv=%.4f avg=%.3f x%.1f (S=%.2f K=%.2f "
                     "basis=%.6f vol=%.2e tau=%.1f)", wts, side, w["attempts"], fv,
                     order.price, order.filled, s_adj, k, basis, vol, tau)
        return False
