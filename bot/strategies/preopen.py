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
  - two seconds after the open the tilt side is quoted higher; the book
    reprices AFTER the open, using information that existed before it.
  - CURRENT NUMBERS LIVE IN THE INSTRUMENTS, NOT HERE (audit 2026-08-13:
    this docstring once said "flat book ~0.50 a side, 60.4% settle,
    +8.12c/share, floor +5.68c" — early small-sample figures at an entry
    nobody pays; the archive now reads ~57% at ask ~0.521 and real fills
    pay ~0.525). bot/launch_ev.py is the number a launch decision reads;
    bot/lean_test.py and bot/slippage.py are the instruments behind it. A
    docstring number ages the moment it is written; these tools do not.

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

# How many recent evaluations the achieved-lead summary spans. A rolling
# window, not all of history: the question STATUS answers is "is the loop
# firing on time NOW", and an all-time median would take days to move after a
# regression. Every lead is also written to the ledger, so nothing is lost.
LEAD_KEEP = 500


class PreopenStrategy:
    def __init__(self, cfg, clob, oracle, spot, executor, ledger, risk):
        self.cfg = cfg
        self.clob = clob
        self.oracle = oracle
        self.spot = spot
        self.exec = executor
        self.ledger = ledger
        self.risk = risk
        self.done = set()          # windows accounted for: entered OR declined
        self.last_nxt = None       # the window the loop last had in its sights
        self.marks = {}            # wts -> entry info, for post-open telemetry
        self.evals = self.entries = self.skips = 0
        self.why = {}
        self.leads = []            # actual seconds-to-open, last LEAD_KEEP evals

    # ------------------------------------------------------------------
    def _skip(self, reason, wts=None, detail=""):
        """Every skipped window says so. A silent strategy is indistinguishable
        from a broken one, which is how two recorders sat dead this week."""
        self.why[reason] = self.why.get(reason, 0) + 1
        self.skips += 1
        log.info("w%s preopen skip: %s%s", wts, reason,
                 f" ({detail})" if detail else "")
        return False

    def _when(self, now, nxt):
        """(action, lead) for this instant: "wait" | "fire" | "too_late".

        Extracted from the run loop so the tests exercise THIS function rather
        than a copy of its logic — a test that mirrors an implementation
        cannot catch the implementation changing.
        """
        if now < nxt - self.cfg.preopen_lead_s:
            return ("wait", None)
        floor = self.cfg.preopen_min_lead_s
        if self.cfg.mode == "live":
            # THE 0.5s FLOOR IS A PAPER FLOOR (runbook 2026-08-11): it assumes
            # an instant fill. A live FAK adds POST latency plus the venue's
            # marketable hold, so an order fired at T-0.5 can land AFTER the
            # open, sweeping the repriced ladder up to the ceiling. 1.0s is
            # the conservative live floor until shadow-phase journals measure
            # the real round trip; it may only ever be TIGHTENED from that
            # measurement, never loosened by hand.
            floor = max(floor, 1.0)
        if now > nxt - floor:
            return ("too_late", None)
        return ("fire", nxt - now)

    def lead_stats(self):
        """One STATUS field answering "is the loop still firing on time?".

        `worst` is the SMALLEST achieved lead, i.e. the latest the loop ever
        woke — that is the number that walks toward the floor when the box
        gets busy, and the one that would have shown the dropped-window bug
        the day it started instead of nineteen hours later.
        """
        if not self.leads:
            return "n/a"
        ls = sorted(self.leads)
        return (f"med={ls[len(ls) // 2]:.2f} worst={ls[0]:.2f} "
                f"tgt={self.cfg.preopen_lead_s:g} n={len(ls)}")

    def _roll(self, now, nxt, T):
        """Per-window bookkeeping. Returns the LIST of windows that rolled
        past unaccounted-for (empty when none).

        THE BACKSTOP. `done` means "accounted for" — entered, refused, or
        declined, every one of them counted and logged. So a window that
        rolls past WITHOUT entering it was never reached at all, and that is
        the one failure the counters cannot otherwise show.

        Two audit fixes (2026-08-13): a stall spanning k windows used to
        report only the last-targeted one, undercounting the very metric this
        exists to close; and a BACKWARD clock step used to mark the still-
        future `last_nxt` as missed, poisoning `done` so the window was then
        skipped silently when its time genuinely came.
        """
        if self.last_nxt is None or nxt == self.last_nxt:
            self.last_nxt = nxt
            return []
        if nxt < self.last_nxt:
            # the clock stepped backward: re-target quietly. Inventing a
            # "missed" for a window still in the future would pre-poison
            # `done` and silently skip it at its real approach.
            self.last_nxt = nxt
            return []
        missed = [w for w in range(self.last_nxt, nxt, T)
                  if w not in self.done]
        for w in missed:
            self.done.add(w)
        # pruned HERE, once per window, rather than on the entry path: a bot
        # halted all day never reaches the entry path, and would grow `done`
        # without bound on exactly the days it is already unhappy
        if len(self.done) > 500:
            self.done = {w for w in self.done if w > now - 4 * T}
        self.last_nxt = nxt
        return missed

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
        t0 = int(open_s - lead)
        s = self.oracle.price_at(t0, exact=False, tolerance=3)
        if not s or s <= 0:
            return None
        # HOW STALE IS THE PRICE WE ARE PRICING OFF? price_at falls back up to
        # three seconds, and a bot that has only received through T-6 computes
        # the tilt from a print three seconds older than the archive holds.
        # Sign agreement is 98.9% at T-3 and 82.6% at T-10, so this is the
        # number that explains a live/archive divergence — recorded rather
        # than inferred, because eth's live tilt differs from the archive's by
        # a median 1.02bp against a 1.0bp gate while btc's differs by 0.08bp.
        age = next((b for b in range(0, 4) if (t0 - b) in self.oracle.samples),
                   None)
        k, n_present, n_elapsed = self.oracle.twap_carry(
            open_s, n, t0, tail=s)
        if k is None or k <= 0 or n_elapsed <= 0:
            return None
        # n_present is 0 for a feed blackout — 6.5% of btc windows and 10.1%
        # of eth have no print at all in this range — and that must always
        # refuse, however good the carry looks.
        if n_present < n_elapsed * self.cfg.preopen_min_coverage:
            return None
        return (s - k) / k * 1e4, s, k, n_present, n_elapsed, age

    # ------------------------------------------------------------------
    async def run(self):
        T = self.cfg.window_secs
        while True:
            now = time.time()
            nxt = int(now - now % T) + T          # the window about to open
            # ---- post-open tracking for a window we entered (telemetry only)
            for wts, info in list(self.marks.items()):
                try:
                    self._track(wts, info, now)
                    if now >= wts + self.cfg.preopen_track_s:
                        self._flush(wts, info)
                except Exception as e:  # noqa: BLE001
                    # telemetry must never be able to stop the trading loop
                    self.marks.pop(wts, None)
                    log.warning("w%s preopen track error: %s", wts, e)
            if not self.cfg.preopen_enabled:
                await asyncio.sleep(0.2)
                continue
            for w in self._roll(now, nxt, T):
                self._skip("missed", w,
                           "the loop never reached this window's firing range")
            if nxt in self.done:
                await asyncio.sleep(0.2)
                continue
            # A HALT IS A DECISION, SO IT GETS COUNTED LIKE ONE. This was a
            # bare `continue`: no counter, no log. A bot halted on the daily
            # loss breaker therefore printed evals=0 skip=0 why={} — character
            # for character what a BROKEN bot prints. Distinguishing those two
            # cost an hour it should have cost a glance.
            if self.risk.halted("preopen"):
                self.done.add(nxt)
                self._skip("halted", nxt, "risk halt in force")
                await asyncio.sleep(1.0)
                continue
            # The previous version fired ONLY inside a 0.6s slot at T-lead and
            # silently dropped the window otherwise. Over 19h that lost 22% of
            # btc 5m evaluations and 35% of eth's with no log line of any kind,
            # while both 15m bots — a third as many reconcile cycles landing on
            # top of the band — hit 101% and 104%.
            action, actual = self._when(now, nxt)
            if action == "wait":
                await asyncio.sleep(0.05)
                continue
            if action == "too_late":
                self.done.add(nxt)
                self._skip("too_late", nxt, f"woke {nxt - now:+.2f}s from open")
                continue
            self.done.add(nxt)
            # EVALUATE AT THE MOMENT WE ACTUALLY EVALUATE. Passing the nominal
            # lead would price a T-3 view while standing at T-1.5, discarding
            # information already in hand. It also makes the achieved lead a
            # recorded quantity, so the spread across live fills answers the
            # lead question with real entries instead of book snapshots.
            try:
                await self._enter(nxt, actual)
            except Exception as e:  # noqa: BLE001
                log.warning("w%s preopen error: %s", nxt, e)

    # ------------------------------------------------------------------
    async def _enter(self, wts, lead=None):
        """lead is the ACTUAL seconds-to-open at this instant, not the
        configured target — a loop that woke late holds more of the strike
        and should use it."""
        if lead is None:
            lead = self.cfg.preopen_lead_s
        self.evals += 1
        self.leads.append(round(lead, 2))
        if len(self.leads) > LEAD_KEEP:
            del self.leads[:-LEAD_KEEP]
        # THE CLOCK IS THE WHOLE TIMING MODEL (audit 2026-08-13). The snipe
        # refuses on an unfit oracle; preopen fired without checking either.
        # A local clock slow by d seconds fires at true T-3+d while computing
        # and RECORDING a healthy lead of 3.0 — for d > 2.5 the order lands
        # after the open against the repriced book, and nothing in the
        # telemetry would say so.
        if self.oracle.degraded or not self.oracle.clock_ok():
            return self._skip("oracle_unfit", wts,
                              f"degraded={self.oracle.degraded} "
                              f"clock_ok={self.oracle.clock_ok()}")
        # A RESTART FORGETS `done`, THE LEDGER DOES NOT. A crash + fast
        # restart landing inside the same window's firing band would enter it
        # a second time — duplicate paper fills now, a doubled real position
        # later. The ledger is the persistent memory `done` is not.
        if self.ledger.has_fill(wts, "preopen"):
            return self._skip("already_entered", wts,
                              "a previous process already filled this window")
        t = self._tilt(wts, lead)
        if t is None:
            return self._skip("no_grid", wts)
        tilt, s, k, n_present, n_elapsed, age = t
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
        if self.cfg.mode == "live":
            if "preopen" not in self.cfg.live_strategies:
                # live take() would return None here and it would be logged as
                # no_fill — a config omission disguised as a quiet market
                return self._skip("not_live_enabled", wts,
                                  "LIVE_STRATEGIES does not name preopen")
            # exchange I/O off the event loop: a blocking POST in here (plus
            # its retry sleep) would blind every feed during the most
            # latency-critical seconds — same rule as the snipe's live path
            order = await asyncio.to_thread(
                self.exec.take, wts, "preopen", token,
                self.cfg.preopen_max_px, self.cfg.preopen_clip)
        else:
            # PAPER HONESTY (audit 2026-08-13): a real FAK decided now
            # reaches the book ~latency later, and the measured cost of that
            # gap is +0.49-0.84c/share even when depth is ample. Paper used
            # to fill at the decision instant off a possibly 3s-old snapshot
            # — the exact optimism the snipe's paper-only recheck exists to
            # remove. Sleep the modeled latency, then sweep the THEN-current
            # book; take() itself re-checks freshness and the ceiling,
            # exactly as a late-arriving FAK's limit would.
            if self.cfg.preopen_take_recheck_s > 0:
                await asyncio.sleep(self.cfg.preopen_take_recheck_s)
            order = self.exec.take(wts, "preopen", token,
                                   self.cfg.preopen_max_px,
                                   self.cfg.preopen_clip)
        if not order or not order.filled:
            return self._skip("no_fill", wts, f"{side} ask {st.best_ask:.3f}")
        self.entries += 1
        self.marks[wts] = dict(side=side, token=token, px=order.price,
                               sz=order.filled, tilt=tilt,
                               peak=None, peak_t=None, low=None, low_t=None,
                               first=None, hit={}, n=0)
        log.info("w%s PREOPEN %s x%.0f @ %.3f (tilt %+.2fbp, spot %.2f "
                 "strike %.2f, grid %d/%d, spot age %ss, lead %.2fs)", wts,
                 side, order.filled, order.price, tilt, s, k, n_present,
                 n_elapsed, age if age is not None else "?", lead)
        self.ledger.event("preopen_entry", json.dumps(
            {"w": wts, "side": side, "tilt": round(tilt, 3),
             "px": round(order.price, 4), "sz": round(order.filled, 1),
             "lead": round(lead, 2),
             # what the bot could SEE when it decided — the only thing that
             # can differ from the archive once the maths is proven identical
             "cov": n_present, "el": n_elapsed, "age": age},
            separators=(",", ":")))
        return True

    def _track(self, wts, info, now):
        """Watch the bid on our side continuously from the open.

        THIS IS THE WHOLE POINT. A resting limit sell fills the instant the
        price touches it, even for a moment. Recording the book at T+2, T+15
        and T+30 asks instead "was the bid above target at these three
        instants", which is a strictly harder test and misses every touch in
        between — so the fill rates measured that way (5c in 60-78% of
        windows within 30s) are lower bounds by an unknown margin.

        The CLOB websocket is already subscribed to this token, so sampling
        it on the loop's own cadence costs nothing. What gets recorded is the
        running peak bid, when it happened, and the first second each exit
        level was reached: that is exactly the set of orders that would have
        filled, and it prices every exit level at once instead of committing
        to 5c in advance.
        """
        if now < wts:
            return
        st = self.clob.state(info["token"])
        bid = getattr(st, "best_bid", None) if st is not None else None
        if bid is None:
            return
        t = round(now - wts, 2)
        info["n"] = info.get("n", 0) + 1
        # SEEDED FROM THE FIRST OBSERVED BID, NOT FROM THE ENTRY PRICE. The
        # first version seeded the peak at what we paid and only ratcheted
        # up, so a window whose jump went the WRONG way recorded peak==entry
        # and was indistinguishable from one that drifted to exactly
        # break-even. That hid the scalp's only real failure mode — the book
        # repricing against the side the tilt picked — behind a floor.
        if info["peak"] is None:
            info["first"] = bid
            info["peak"] = info["low"] = bid
            info["peak_t"] = info["low_t"] = t
        if bid > info["peak"]:
            info["peak"], info["peak_t"] = bid, t
        if bid < info["low"]:
            info["low"], info["low_t"] = bid, t
        for c in self.cfg.preopen_track_levels:
            k = str(int(round(c * 100)))
            if k not in info["hit"] and bid >= info["px"] + c - 1e-9:
                info["hit"][k] = round(now - wts, 2)

    def _flush(self, wts, info):
        """One row per position: the full touch profile of the post-open move."""
        self.marks.pop(wts, None)
        st = self.clob.state(info["token"])
        bid = getattr(st, "best_bid", None) if st is not None else None
        ask = getattr(st, "best_ask", None) if st is not None else None
        self.ledger.event("preopen_mark", json.dumps(
            {"w": wts, "side": info["side"], "entry": round(info["px"], 4),
             "sz": round(info["sz"], 1), "tilt": round(info["tilt"], 3),
             "t": self.cfg.preopen_track_s,
             "bid": None if bid is None else round(bid, 4),
             "ask": None if ask is None else round(ask, 4),
             # peak bid reached, and WHEN — an exit level is only real if the
             # touch happens early enough to be worth resting for
             "peak": None if info["peak"] is None else round(info["peak"], 4),
             "peak_t": info["peak_t"],
             # the other side of the same question: how far the book repriced
             # AGAINST us, which is the scalp's whole risk and what the
             # leftover position is worth if no level is ever touched
             "low": None if info["low"] is None else round(info["low"], 4),
             "low_t": info["low_t"],
             "first": None if info["first"] is None else round(info["first"], 4),
             # {cents_above_entry: seconds_after_open_first_touched}
             "hit": info["hit"],
             "samples": info.get("n", 0)},
            separators=(",", ":")))
        if info["peak"] is None:
            log.info("w%s preopen mark: no book samples after the open", wts)
            return
        log.info("w%s preopen mark: peak %+.2fc at t+%.1fs, low %+.2fc at "
                 "t+%.1fs, touched %s", wts,
                 100 * (info["peak"] - info["px"]), info["peak_t"],
                 100 * (info["low"] - info["px"]), info["low_t"],
                 ",".join(f"{k}c@{v:.0f}s" for k, v in sorted(
                     info["hit"].items(), key=lambda z: int(z[0]))) or "nothing")
