"""S3 — post-close winner toll.

At window close the outcome is already decided by the oracle print; sellers keep
dumping winner tokens below fair for ~20s until settlement. We read the oracle
open/close, pick the winner, and rest a bid at the best price the current tick
regime allows (0.992 on 0.001 ticks, else 0.99), sized by a feedback controller
that chases a target fill share within a float budget.

Safety: skip the window unless we hold a fresh oracle print for BOTH boundary
seconds; any winner mismatch versus the exchange's own resolution (reconciler)
trips a hard halt — live, a wrong-side toll bid buys a zero at 0.992.
"""
import asyncio
import logging
import time

log = logging.getLogger("toll")


class TollStrategy:
    def __init__(self, cfg, clob, oracle, executor, ledger, risk):
        self.cfg, self.clob, self.oracle = cfg, clob, oracle
        self.exec, self.ledger, self.risk = executor, ledger, risk
        self.clip = float(cfg.toll_start_clip)
        self.oracle_calls = {}          # wts -> 'up'|'down' (for reconciler check)

    async def run(self):
        T = self.cfg.window_secs
        while True:
            now = time.time()
            wts = int(now - now % T)                     # window currently in flight
            close_ts = wts + T
            await asyncio.sleep(max(0.0, close_ts - time.time()))
            if not self.cfg.toll_enabled or self.risk.halted("toll"):
                continue
            try:
                await self._trade_window(wts)
            except Exception as e:  # noqa: BLE001
                log.exception("toll window %s failed: %s", wts, e)

    async def _trade_window(self, wts):
        T = self.cfg.window_secs
        mk = self.clob.market_for(wts)
        if mk is None:
            log.info("w%s: market not discovered, skip", wts)
            return
        # wait the configured delay, then read both boundary prints
        await asyncio.sleep(self.cfg.toll_place_delay_s)
        if self.oracle.staleness() > self.cfg.oracle_max_staleness_s:
            self.ledger.event("toll_skip", f"w{wts} oracle stale {self.oracle.staleness():.1f}s")
            return
        k_open = self.oracle.price_at(wts)
        k_close = self.oracle.price_at(wts + T)
        if k_open is None or k_close is None:
            self.ledger.event("toll_skip", f"w{wts} missing boundary print")
            return
        winner = "up" if k_close > k_open else "down"    # Up wins iff close > open
        self.oracle_calls[wts] = winner
        token = mk.token_up if winner == "up" else mk.token_down
        st = self.clob.state(token)
        tick = st.tick if st else mk.tick
        price = self.cfg.toll_price_fine if tick <= 0.0011 else self.cfg.toll_price_coarse
        size = max(self.cfg.toll_min_clip,
                   min(self.clip, self.cfg.toll_max_clip,
                       self.cfg.toll_float_budget / price))
        size = float(int(size))
        if size < mk.min_size:
            return
        order = self.exec.place_limit(wts, "toll", token, price, size)
        deg = " DEGRADED-ORACLE" if self.oracle.degraded else ""
        log.info("w%s winner=%s (%.2f->%.2f) bid %.0f @ %.3f tick=%s%s",
                 wts, winner, k_open, k_close, size, price, tick, deg)
        # poll fills until cancel time
        end = wts + T + self.cfg.toll_cancel_after_s
        while time.time() < end and order.status == "open":
            self.exec.poll_fills()
            await asyncio.sleep(0.25)
        self.exec.poll_fills()
        self.exec.cancel(order.id)
        self._update_controller(order)

    def _update_controller(self, order):
        share = order.filled / order.size if order.size else 0.0
        if share >= self.cfg.toll_target_fill_share:
            self.clip = min(self.clip * (1 + self.cfg.toll_controller_alpha),
                            self.cfg.toll_max_clip)
        else:
            self.clip = max(self.clip * (1 - self.cfg.toll_controller_alpha * 1.5),
                            self.cfg.toll_min_clip)
        log.info("w%s fill %.0f/%.0f (%.0f%%) -> next clip %.0f",
                 order.wts, order.filled, order.size, share * 100, self.clip)
