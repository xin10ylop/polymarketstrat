"""Buy the tilt side BEFORE the window opens, while the book is still flat.

THE MECHANISM. Since 2026-08-07 the strike is the 30s mean ending at T, not
spot at T. A trailing mean lags, so the window opens off-strike — and that
tilt is knowable in advance, because at T-3 we already hold 27 of the
strike's 30 seconds and need only extrapolate the last three from the most
recent print.

MEASURED, and this is the whole case:
  - sign of the tilt at T-3 vs the true tilt at T+0: 98.9% btc / 97.4% eth
    agreement on windows over 1bp (6,047 windows per coin). At T-10 that
    falls to 83%/80%, so THREE SECONDS is the lead, not ten.
  - the pre-open book is flat and symmetric, ~0.50 a side, and DEEP:
    1,889 shares within 5c on one side and 3,310 on the other at T-3, with
    depth essentially unchanged from T-30 to T-1. Makers do not pull.
  - two seconds after the open the tilt side is quoted 0.563, and by T+15
    it has gone as far as 0.70. The book reprices AFTER the open, using
    information that already existed before it.
  - so: 60.4% of these windows settle the tilt side, bought at ~0.505.
    That is +8.12c/share held to settlement, 95% lower bound +5.68c.

WHY BUY-AND-HOLD RATHER THAN THE +5c EXIT. The proposal rests a limit sell
at entry+5c and the snap does clear it. But holding is worth MORE (+8.1c
against +3.25c after the entry fee), and simulating a resting SELL would
need fill machinery the paper executor does not have — machinery whose
first version would be untested on the exact path that decides the result.
So this holds, and records the post-open marks as telemetry, which lets the
+5c variant be priced later from data instead of from a fill model.

RISKS THIS DOES NOT HIDE. The pre-open book may LEAN toward the tilt side
in windows we have not yet sampled, which would raise the entry price and
shrink the edge one-for-one; preopen_max_px refuses anything above a
ceiling so a leaning book costs a skipped trade rather than a bad fill. And
the settlement rate above comes from a large sample of the SIGNAL but only
a small sample of pre-open PRICES. The ledger this writes is the instrument
that settles both.
"""
import asyncio
import json
import logging
import time

log = logging.getLogger("preopen")


class PreopenStrategy:
    def __init__(self, cfg, clob, oracle, spot, executor, ledger, risk):
        self.cfg = cfg
        self.clob = clob
        self.oracle = oracle
        self.spot = spot
        self.exec = executor
        self.ledger = ledger
        self.risk = risk
        self.done = set()          # windows already acted on
        self.marks = {}            # wts -> entry info, for post-open telemetry
        self.evals = self.entries = self.skips = 0
        self.why = {}

    # ------------------------------------------------------------------
    def _skip(self, reason, wts=None, detail=""):
        """Every skipped window says so. A silent strategy is indistinguishable
        from a broken one, which is how two recorders sat dead this week."""
        self.why[reason] = self.why.get(reason, 0) + 1
        self.skips += 1
        log.info("w%s preopen skip: %s%s", wts, reason,
                 f" ({detail})" if detail else "")
        return False

    def _tilt(self, open_s, lead):
        """(tilt_bp, spot, strike) using ONLY what exists at open_s - lead.

        The strike is the mean over [T-N, T). At T-lead we have the first
        N-lead seconds of it; holes inside those seconds carry forward from
        the last print, and the not-yet-elapsed tail is imputed from the
        latest print. Both reach backward only, so there is no lookahead.

        Carrying rather than rescaling the present seconds' mean is worth 4x
        on btc and 5.5x on eth in strike accuracy — see Oracle.twap_carry for
        the measurement. It matters most exactly where it is least visible:
        on a marginal window near the 0.5bp gate, where rescaling's worst
        bucket drifts 0.377bp and can flip the side by itself.
        """
        n = self.cfg.oracle_twap_s
        if not n:
            return None
        s = self.oracle.price_at(int(open_s - lead), exact=False, tolerance=3)
        if not s or s <= 0:
            return None
        k, n_present, n_elapsed = self.oracle.twap_carry(
            open_s, n, int(open_s - lead), tail=s)
        if k is None or k <= 0 or n_elapsed <= 0:
            return None
        # n_present is 0 for a feed blackout — 6.5% of btc windows and 10.1%
        # of eth have no print at all in this range — and that must always
        # refuse, however good the carry looks.
        if n_present < n_elapsed * self.cfg.preopen_min_coverage:
            return None
        return (s - k) / k * 1e4, s, k

    # ------------------------------------------------------------------
    async def run(self):
        T = self.cfg.window_secs
        lead = self.cfg.preopen_lead_s
        while True:
            now = time.time()
            nxt = int(now - now % T) + T          # the window about to open
            # ---- post-open mark for a window we entered (telemetry only)
            for wts, info in list(self.marks.items()):
                if now >= wts + self.cfg.preopen_mark_s:
                    self._mark(wts, info)
            if nxt in self.done or not self.cfg.preopen_enabled:
                await asyncio.sleep(0.2)
                continue
            if self.risk.halted("preopen"):
                await asyncio.sleep(1.0)
                continue
            # fire once, inside a tight band around T-lead
            if not (0.0 <= (nxt - lead) - now <= 0.6):
                await asyncio.sleep(0.05)
                continue
            self.done.add(nxt)
            try:
                self._enter(nxt)
            except Exception as e:  # noqa: BLE001
                log.warning("w%s preopen error: %s", nxt, e)
            # keep `done` from growing forever
            if len(self.done) > 500:
                self.done = {w for w in self.done if w > now - 4 * T}

    # ------------------------------------------------------------------
    def _enter(self, wts):
        self.evals += 1
        t = self._tilt(wts, self.cfg.preopen_lead_s)
        if t is None:
            return self._skip("no_grid", wts)
        tilt, s, k = t
        if abs(tilt) < self.cfg.preopen_tilt_min_bp:
            return self._skip("flat_tilt", wts, f"{tilt:+.2f}bp < "
                              f"{self.cfg.preopen_tilt_min_bp}bp")
        mk = self.clob.market_for(wts)
        if mk is None:
            return self._skip("no_market", wts)
        side = "up" if tilt >= 0 else "down"
        token = mk.token_up if side == "up" else mk.token_down
        st = self.clob.state(token)
        if st is None or not st.book_fresh(self.cfg.book_max_age_s):
            return self._skip("stale_book", wts, f"tilt {tilt:+.2f}bp {side}")
        if st.best_ask is None:
            return self._skip("no_ask", wts, f"tilt {tilt:+.2f}bp {side}")
        # A LEANING BOOK IS THE ONE THING THAT KILLS THIS. If the makers have
        # already priced the tilt, the entry is no longer ~0.50 and the edge
        # shrinks one-for-one. Refuse rather than pay it.
        if st.best_ask > self.cfg.preopen_max_px:
            return self._skip("book_leans", wts,
                              f"{side} ask {st.best_ask:.3f} > "
                              f"{self.cfg.preopen_max_px:.2f}, tilt {tilt:+.2f}bp")
        order = self.exec.take(wts, "preopen", token, self.cfg.preopen_max_px,
                               self.cfg.preopen_clip)
        if not order or not order.filled:
            return self._skip("no_fill", wts, f"{side} ask {st.best_ask:.3f}")
        self.entries += 1
        self.marks[wts] = dict(side=side, token=token, px=order.price,
                               sz=order.filled, tilt=tilt)
        log.info("w%s PREOPEN %s x%.0f @ %.3f (tilt %+.2fbp, spot %.2f "
                 "strike %.2f)", wts, side, order.filled, order.price, tilt, s, k)
        self.ledger.event("preopen_entry", json.dumps(
            {"w": wts, "side": side, "tilt": round(tilt, 3),
             "px": round(order.price, 4), "sz": round(order.filled, 1),
             "lead": self.cfg.preopen_lead_s}, separators=(",", ":")))
        return True

    def _mark(self, wts, info):
        """Where our side trades after the open — prices the +5c exit later."""
        self.marks.pop(wts, None)
        st = self.clob.state(info["token"])
        bid = getattr(st, "best_bid", None) if st is not None else None
        ask = getattr(st, "best_ask", None) if st is not None else None
        exit_px = info["px"] + self.cfg.preopen_exit_c
        self.ledger.event("preopen_mark", json.dumps(
            {"w": wts, "side": info["side"], "entry": round(info["px"], 4),
             "sz": round(info["sz"], 1), "tilt": round(info["tilt"], 3),
             "t": self.cfg.preopen_mark_s,
             "bid": None if bid is None else round(bid, 4),
             "ask": None if ask is None else round(ask, 4),
             "exit_target": round(exit_px, 4),
             # would a resting +5c sell have been reachable at this instant?
             "exit_hit": None if bid is None else bool(bid >= exit_px)},
            separators=(",", ":")))
