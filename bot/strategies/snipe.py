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
import json
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
        self.thin_gap = 0       # TWAP era: real data, margin below the floors
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
        if mk is None or self.oracle.degraded or not self.oracle.clock_ok():
            return False
        # cold-start guard: immature vol/basis estimators produce
        # garbage-confident signals in the first minutes after a (re)start
        if (time.time() - self._started < self.cfg.snipe_warmup_s
                or len(self.spot.basis_samples) < 45):
            self.no_data += 1
            return False
        # the strike: a rolling TWAP at the open since the venue's 2026-08-07
        # rule change, the exact open print before it (1h family, unchanged)
        n_twap = self.cfg.oracle_twap_s
        if n_twap:
            k, k_cov = self.oracle.twap_at(wts, n_twap)
            if k is None or k <= 0 or k_cov < self.cfg.oracle_twap_min_coverage:
                self.no_data += 1
                return False
        else:
            k = self.oracle.price_at(wts, exact=True, tolerance=2)
            if k is None or k <= 0:
                return False
        sig_t = time.time() - self.cfg.snipe_signal_lag_s
        s_lag, bar_sec = self.spot.close_at(sig_t)
        vol = self.spot.vol()
        basis = self.spot.basis()
        if s_lag is None or vol is None or basis is None:
            self.no_data += 1
            return False
        # sparse-tape staleness (audit F1): a bar much older than requested
        # would be priced as fresh, understating the true horizon
        if sig_t - bar_sec > self.cfg.spot_max_bar_age_s:
            self.no_data += 1
            return False
        tau = wts + T - bar_sec
        if tau <= 0:
            return False
        s_adj = s_lag * basis
        vol = max(vol, self.cfg.snipe_vol_floor)
        if n_twap:
            # _twap_fv books its own refusal reason: a thin grid is a data
            # problem, a sub-floor margin is simply no edge. Conflating them
            # would make every quiet tick look like a broken feed.
            got = self._twap_fv(wts + T, n_twap, k, s_adj, vol)
            if got is None:
                return False
            fv, u = got
        else:
            # input-resolution floor (audit F3): below N spot ticks of distance,
            # fv is quantization noise dressed as confidence (binds on SOL only)
            if abs(math.log(s_adj / k)) < self.cfg.snipe_min_ticks * self.cfg.spot_tick / s_adj:
                return False
            fv = norm_cdf(math.log(s_adj / k) / (vol * math.sqrt(tau)))
        self.evals += 1
        self.last_fv = fv
        side = "up" if fv >= self.cfg.snipe_fv_min else (
            "down" if fv <= 1 - self.cfg.snipe_fv_min else None)
        if side is None:
            return False
        token = mk.token_up if side == "up" else mk.token_down
        st = self.clob.state(token)
        if not self._ask_ok(st):
            self.near_misses += 1
            self._eval_snap(wts, side, fv, st)
            return False
        w["attempts"] += 1
        if w["attempts"] > 1:
            self.retries += 1
        depth_pre = sorted(st.asks.items())[:5]
        if self.cfg.snipe_take_recheck_s > 0 and self.cfg.mode != "live":
            # PAPER-ONLY live-fidelity gate: a real order needs ~network + 250ms
            # exchange hold to arrive; only fill if the ask is still there
            # afterwards (~82% of instantly-visible asks are gone by then —
            # audited). A failed recheck consumes an attempt, like a missed FAK.
            # In live mode the latency is REAL, so the sleep must not run:
            # the FAK goes out immediately and the exchange decides the race.
            await asyncio.sleep(self.cfg.snipe_take_recheck_s)
            st = self.clob.state(token)
            # full entry predicate, not a weaker subset: price floor and the
            # giant-ask refusal must hold on the post-latency book too
            if not self._ask_ok(st):
                self.recheck_fail += 1
                self._depth_event(wts, side, fv, depth_pre,
                                  self.clob.state(token), 0.0)
                return False
        # budget sized at the sweep LIMIT, not best_ask: the take may fill
        # deeper levels up to ask_max, and live sizes at the limit (audit M2)
        remaining = min(self.cfg.snipe_max_clip - w["shares"],
                        (self.cfg.snipe_window_max_cost - w["cost"])
                        / max(self.cfg.snipe_ask_max, 0.01))
        # informed-wall guard on the SWEEP (audit: skip_ask_above only vetted
        # the best level): cap size at the liquidity in front of the first
        # giant level, so neither paper nor live extends into the wall
        pre_wall = 0.0
        limit = self.cfg.snipe_ask_max
        for px in sorted(p for p in st.asks if p <= self.cfg.snipe_ask_max):
            if st.asks[px] > self.cfg.snipe_skip_ask_above:
                # cap the sweep PRICE at the wall, not just the size: retries
                # with the pre-wall level consumed would otherwise spill INTO
                # the wall (fill-economics audit S5 — the informed fills the
                # gate exists to refuse). Applies to paper and live alike.
                limit = min(limit, round(px - 0.001, 3))
                break
            pre_wall += st.asks[px]
        remaining = min(remaining, pre_wall)
        if w["attempts"] == 1:
            remaining = min(remaining, self.cfg.snipe_first_clip)
        if self.cfg.mode == "live":
            # exchange I/O off the event loop: a blocking POST in here would
            # blind every feed during the most latency-critical seconds
            order = await asyncio.to_thread(
                self.exec.take, wts, "snipe", token, limit, remaining)
        else:
            order = self.exec.take(wts, "snipe", token, limit, remaining)
        if order:
            self.signals += 1
            w["shares"] += order.filled
            w["cost"] += order.filled * order.price
            log.info("w%s SNIPE %s att=%d fv=%.4f avg=%.3f x%.1f (S=%.2f K=%.2f "
                     "basis=%.6f vol=%.2e tau=%.1f)", wts, side, w["attempts"], fv,
                     order.price, order.filled, s_adj, k, basis, vol, tau)
        self._depth_event(wts, side, fv, depth_pre, st,
                          order.filled if order else 0.0)
        return False

    def _ask_ok(self, st):
        """The single entry/recheck gate predicate (audit M4: the recheck must
        not be a weaker subset of the entry check). Tallies WHY it rejects so
        a fill drought is diagnosable from the STATUS line alone."""
        if not hasattr(self, "nm"):
            self.nm = {}
        if st is None:
            r = "nostate"
        elif not st.book_fresh(self.cfg.book_max_age_s):
            r = "stale_book"
        elif st.best_ask is None:
            r = "no_ask"
        elif st.best_ask > self.cfg.snipe_ask_max:
            r = "px_high"
        elif st.best_ask < self.cfg.snipe_price_floor:
            r = "px_floor"
        elif st.best_ask_size < self.cfg.snipe_min_ask_size:
            r = "too_small"
        elif st.best_ask_size > self.cfg.snipe_skip_ask_above:
            r = "wall"
        else:
            self._last_reason = None
            return True
        self.nm[r] = self.nm.get(r, 0) + 1
        self._last_reason = r
        return False

    def _eval_snap(self, wts, side, fv, st):
        """One row per window on the confident-but-no-trade path.

        The STATUS tallies say WHY we skipped but not what the book looked
        like, and after the 2026-08-07 rule change nearly every tick is
        confident — so the fill drought has to be diagnosed from the book
        itself: is the winning side empty, frozen, or just expensive, and are
        we picking both sides or stuck on one? Sampled once per window.
        """
        if getattr(self, "_snap_wts", None) == wts:
            return
        self._snap_wts = wts
        try:
            self.ledger.event("eval_snap", json.dumps({
                "w": wts, "s": side, "fv": round(fv, 6),
                "gap_bp": round(getattr(self, "_last_gap_bp", 0.0), 4),
                "why": getattr(self, "_last_reason", None),
                "ask": st.best_ask if st is not None else None,
                "sz": round(st.best_ask_size, 1) if st is not None else None,
                "bid": st.best_bid if st is not None else None,
                "age": round(time.time() - st.last_book_ts, 2) if st is not None else None,
            }, separators=(",", ":")))
        except Exception:  # noqa: BLE001 - telemetry must never break trading
            log.debug("eval_snap failed w%s", wts)

    def _twap_fv(self, C, n, k, s_adj, vol):
        """P(closing TWAP >= strike) for the venue's post-2026-08-07 rule.

        The closing average runs over [C-n, C). At decision time most of it is
        ALREADY DETERMINED — only the seconds after our last oracle sample are
        still random — so the uncertainty term is far smaller than the old
        spot-close model's `vol*sqrt(tau)`:

            TWAP_close ~ Normal( (known + u*S)/n ,  (S*vol/n) * sqrt(V) )
            V = u^2*a + u(u+1)(2u+1)/6      (a = seconds until the average starts)

        u = unknown seconds inside the average, a = 0 whenever the average has
        already begun (always true at snipe time: u<=5 vs n>=30). At u=5, n=30
        the sd is ~10x smaller than the old rule's — the same market move now
        buys far more certainty, which is exactly why this must clear the
        honesty gate on paper before it trades.
        Returns (fv, u) or None when coverage is too thin to average honestly.
        """
        L = self.oracle.last_sample_s
        known_sum, n_present, n_elapsed = self.oracle.twap_known(C, n, L + 1)
        if n_elapsed > 0 and n_present / n_elapsed < self.cfg.oracle_twap_min_coverage:
            self.no_data += 1
            return None                      # gappy grid: not the venue's number
        u = n - n_elapsed
        if u <= 0:
            self.no_data += 1
            return None                      # window already closed for us
        if n_present:                        # impute holes at the known mean
            known_sum *= n_elapsed / n_present
        a = max(0, (C - n) - (L + 1))        # average not started yet (not at snipe time)
        mu = (known_sum + u * s_adj) / n
        var = u * u * a + u * (u + 1) * (2 * u + 1) / 6.0
        sd = (s_adj * vol / n) * math.sqrt(var)
        if sd <= 0:
            self.no_data += 1
            return None
        gap = mu - k
        # resolution floor, damped by the projection's weight: only u/n of the
        # closing average carries the spot feed's tick noise
        if abs(gap) < (u / n) * self.cfg.snipe_min_ticks * self.cfg.spot_tick:
            self.thin_gap += 1
            return None
        # sub-basis-point margins are below our own reconstruction error (one
        # ETH window missed at 0.056bp in verification) — never call those
        if abs(gap) / k < self.cfg.snipe_min_gap_bps * 1e-4:
            self.thin_gap += 1
            return None
        self._last_gap_bp = gap / k * 1e4
        return norm_cdf(gap / sd), u

    def _depth_event(self, wts, side, fv, pre, st, filled):
        """Deeper-book research tap: the ask ladder at signal time and after
        the latency gate. Passive — the counterfactual for layers the strategy
        never takes; settlement joins outcomes in later, off-line."""
        try:
            post = sorted(st.asks.items())[:5] if st is not None else []
            kf = getattr(self, "kalshi", None)   # telemetry-only overlay; None-safe
            k = kf.state(wts + self.cfg.window_secs) if kf is not None else None
            self.ledger.event("depth", json.dumps(
                {"w": wts, "s": side, "fv": round(fv, 4),
                 "pre": [[p, round(z, 1)] for p, z in pre],
                 "post": [[p, round(z, 1)] for p, z in post],
                 "fill": round(filled, 1), "k": k}, separators=(",", ":")))
        except Exception:  # noqa: BLE001 - research logging must never break trading
            log.debug("depth event failed w%s", wts)
