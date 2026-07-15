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

    async def run(self):
        T = self.cfg.window_secs
        while True:
            now = time.time()
            wts = int(now - now % T)
            eval_start = wts + T + self.cfg.snipe_eval_from_s
            if now < eval_start:
                await asyncio.sleep(min(eval_start - now, 1.0))
                continue
            if now >= wts + T:
                await asyncio.sleep(0.2)
                continue
            if self.cfg.snipe_enabled and not self.risk.halted("snipe"):
                try:
                    if self._evaluate(wts):
                        # one shot per window: sleep to the next one
                        await asyncio.sleep(max(0.0, wts + T - time.time()))
                        continue
                except Exception as e:  # noqa: BLE001
                    log.exception("snipe eval failed: %s", e)
            await asyncio.sleep(0.2)

    def _evaluate(self, wts):
        T = self.cfg.window_secs
        mk = self.clob.market_for(wts)
        if mk is None or self.oracle.degraded:
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
            return False
        tau = wts + T - sig_t
        if tau <= 0:
            return False
        s_adj = s_lag * basis
        fv = norm_cdf(math.log(s_adj / k) / (max(vol, self.cfg.snipe_vol_floor) * math.sqrt(tau)))
        side = "up" if fv >= self.cfg.snipe_fv_min else (
            "down" if fv <= 1 - self.cfg.snipe_fv_min else None)
        if side is None:
            return False
        token = mk.token_up if side == "up" else mk.token_down
        st = self.clob.state(token)
        if (st is None or not st.book_fresh(self.cfg.book_max_age_s)
                or st.best_ask is None
                or st.best_ask > self.cfg.snipe_ask_max
                or st.best_ask_size < self.cfg.snipe_min_ask_size):
            return False
        size = min(st.best_ask_size, self.cfg.snipe_max_clip)
        order = self.exec.take(wts, "snipe", token, self.cfg.snipe_ask_max, size)
        if order:
            log.info("w%s SNIPE %s fv=%.4f ask=%.3f x%.0f (S=%.2f K=%.2f basis=%.6f "
                     "vol=%.2e tau=%.1f)", wts, side, fv, order.price, order.filled,
                     s_adj, k, basis, vol, tau)
            return True
        return False
