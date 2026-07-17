"""S3 — post-close winner toll.

At window close the outcome is decided by the Chainlink data-stream print;
sellers keep dumping winner tokens below fair for ~20s until settlement. We
read the resolver's own feed at the EXACT boundary seconds, pick the winner,
and rest a bid at the best price the tick regime allows, sized by a feedback
controller within float and per-window-loss budgets.

Hard safety rules (each one individually prevented or bounded a real loss the
first paper night):
  - trade only on exact boundary samples from the resolution feed; a missing
    sample or a degraded feed skips the window — never guess;
  - skip windows decided by < toll_min_margin_usd (marginal windows carry all
    the residual risk for the same 0.8-1.0c of upside);
  - ties resolve UP ("greater than or equal", per the market description);
  - size <= toll_max_window_loss/price, so one wrong call can never exceed the
    per-window loss budget; any oracle/exchange mismatch halts the strategy.
"""
import asyncio
import logging
import time

log = logging.getLogger("toll")


class TollStrategy:
    def __init__(self, cfg, clob, oracle, spot, executor, ledger, risk):
        self.cfg, self.clob, self.oracle, self.spot = cfg, clob, oracle, spot
        self.exec, self.ledger, self.risk = executor, ledger, risk
        self.clip = float(cfg.toll_start_clip)
        self.oracle_calls = {}          # wts -> 'up'|'down'
        # Tier 2 pre-position telemetry
        self.pre_placed = 0
        self.pre_correct = 0
        self.pre_wrong = 0
        self.pre_marginal_cancel = 0

    async def run(self):
        T = self.cfg.window_secs
        while True:
            now = time.time()
            wts = int(now - now % T)
            close_ts = wts + T
            pre_ts = close_ts - self.cfg.toll_pre_lead_s
            pre = None
            if self.cfg.toll_pre_position and time.time() < pre_ts:
                await asyncio.sleep(max(0.0, pre_ts - time.time()))
                if not self.risk.halted("toll"):
                    try:
                        pre = await self._pre_evaluate(wts)
                    except Exception as e:  # noqa: BLE001
                        log.exception("toll pre-evaluate %s failed: %s", wts, e)
            await asyncio.sleep(max(0.0, close_ts - time.time()))
            if not self.cfg.toll_enabled or self.risk.halted("toll"):
                if pre is not None:      # never leak a pre-order into a halt
                    self.exec.cancel(pre[0].id)
                continue
            try:
                await self._trade_window(wts, pre)
            except Exception as e:  # noqa: BLE001
                log.exception("toll window %s failed: %s", wts, e)

    async def _read_boundaries(self, wts, poll_s=0.2):
        """Exact-second samples from the resolution feed, waiting briefly for
        the close print (frames arrive ~1s after the sampled second)."""
        T = self.cfg.window_secs
        deadline = wts + T + self.cfg.toll_place_delay_s + self.cfg.toll_boundary_wait_s
        while True:
            k_open = self.oracle.price_at(wts, exact=True)
            k_close = self.oracle.price_at(wts + T, exact=True)
            if k_open is not None and k_close is not None:
                return k_open, k_close
            if time.time() >= deadline:
                return k_open, k_close
            await asyncio.sleep(poll_s)

    async def _pre_evaluate(self, wts):
        """Tier 2: on a clearly-decided window, bid ~toll_pre_lead_s before
        close for queue priority. Only fires when the live-vs-open delta
        already clears both a flat $ floor and a vol-scaled sigma bar, so a
        wrong call should be rare; _trade_window confirms against the real
        close print and cancels instantly if the prediction was wrong."""
        mk = self.clob.market_for(wts)
        if mk is None:
            return None
        if self.oracle.degraded:
            return None
        k_open = self.oracle.price_at(wts, exact=True, tolerance=2)
        if k_open is None:
            return None
        cur = self.oracle.last_price
        if cur is None:
            return None
        if self.oracle.staleness() > 2.5:
            return None
        predicted_delta = cur - k_open
        vol = self.spot.vol()
        if vol is None:
            return None
        sigma_usd = (self.cfg.toll_pre_sigma * vol * cur *
                    ((self.cfg.toll_pre_lead_s + 2.0) ** 0.5))
        threshold = max(self.cfg.toll_pre_margin_usd, sigma_usd, self.cfg.toll_min_margin_usd)
        if abs(predicted_delta) < threshold:
            return None
        predicted_winner = "up" if predicted_delta >= 0 else "down"
        token = mk.token_up if predicted_winner == "up" else mk.token_down
        st = self.clob.state(token)
        tick = mk.tick if mk else (st.tick if st else 0.01)
        price = self.cfg.toll_price_fine if tick <= 0.0011 else self.cfg.toll_price_coarse
        size = min(self.clip, self.cfg.toll_max_clip,
                   self.cfg.toll_float_budget / price,
                   self.cfg.toll_max_window_loss / price)
        size = float(int(size))
        if size < max(self.cfg.toll_min_clip, mk.min_size):
            return None
        order = self.exec.place_limit(wts, "toll", token, price, size)
        self.pre_placed += 1
        log.info("w%s PRE-position %s (pred %+.2f >= thr %.2f) bid %.0f @ %.3f",
                 wts, predicted_winner, predicted_delta, threshold, size, price)
        return order, predicted_winner

    async def _trade_window(self, wts, pre=None):
        T = self.cfg.window_secs
        mk = self.clob.market_for(wts)
        if mk is None:
            log.info("w%s: market not discovered, skip", wts)
            return
        await asyncio.sleep(self.cfg.toll_place_delay_s)
        if self.oracle.degraded:
            self.ledger.event("toll_skip", f"w{wts} oracle degraded "
                              f"({self.oracle.staleness():.1f}s)")
            return
        poll_s = 0.1 if pre is not None else 0.2
        k_open, k_close = await self._read_boundaries(wts, poll_s)
        if k_open is None or k_close is None:
            if pre is not None:
                self.exec.cancel(pre[0].id)
            self.ledger.event("toll_skip", f"w{wts} missing exact boundary sample "
                              f"(open={k_open} close={k_close})")
            return
        delta = k_close - k_open
        if abs(delta) < self.cfg.toll_min_margin_usd:
            if pre is not None:
                self.exec.cancel(pre[0].id)
                self.pre_marginal_cancel += 1
            self.ledger.event("toll_skip", f"w{wts} margin {delta:+.2f} < "
                              f"{self.cfg.toll_min_margin_usd}")
            log.info("w%s skip: margin %+.2f too small", wts, delta)
            return
        winner = "up" if k_close >= k_open else "down"   # ties resolve Up
        self.oracle_calls[wts] = winner
        token = mk.token_up if winner == "up" else mk.token_down
        st = self.clob.state(token)
        tick = mk.tick if mk else (st.tick if st else 0.01)
        price = self.cfg.toll_price_fine if tick <= 0.0011 else self.cfg.toll_price_coarse

        pre_order, predicted_winner = pre if pre is not None else (None, None)
        if pre_order is not None and predicted_winner == winner:
            self.pre_correct += 1
            order = pre_order
            log.info("w%s winner=%s (%.2f->%.2f, %+0.2f) bid %.0f @ %.3f tick=%s "
                     "[pre-positioned]",
                     wts, winner, k_open, k_close, delta, order.size, order.price, tick)
        else:
            if pre_order is not None:
                self.pre_wrong += 1
                self.exec.cancel(pre_order.id)
                self.ledger.event("pre_wrong",
                                  f"w{wts} predicted {predicted_winner} actual {winner}")
                log.error("w%s PRE-position WRONG: predicted %s actual %s "
                          "(%.2f->%.2f, %+0.2f)",
                          wts, predicted_winner, winner, k_open, k_close, delta)
            size = min(self.clip, self.cfg.toll_max_clip,
                       self.cfg.toll_float_budget / price,
                       self.cfg.toll_max_window_loss / price)
            size = float(int(size))
            if size < max(self.cfg.toll_min_clip, mk.min_size):
                self.ledger.event("toll_skip", f"w{wts} size {size:.0f} below floor")
                return
            order = self.exec.place_limit(wts, "toll", token, price, size)
            log.info("w%s winner=%s (%.2f->%.2f, %+0.2f) bid %.0f @ %.3f tick=%s",
                     wts, winner, k_open, k_close, delta, size, price, tick)

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
