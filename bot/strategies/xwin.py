"""XWIN: shared-close 5m x 15m structural scanner (BTC, paper-only).

Every quarter-hour close C is shared by the 5m window [C-300, C) and the 15m
window [C-900, C). With strikes o5 (chainlink at C-300) and o15 (at C-900):
  o15 < o5:  Up15 dominates Up5  -> buy Up15 + Down5: pays 1 always, 2 when
             the close lands in (o15, o5].
  o5 < o15:  mirror              -> buy Up5 + Down15.
Whenever the combined taker cost of the two legs is below ~1, the package is
positive-EV with a hard floor (verified on 9,729 historical settlements, zero
violations — runbook 2026-08-04).

Tiers (from the audited backtest):
  AUTO  (paper-trades): tau in [tau_min, tau_max], cost in [min_cost, max_cost],
        both L1 sizes >= min_size, books fresh. Honest expectation $8-20/day.
  DEEP  (log-only): cost < min_cost or tau < tau_min sightings — the $30/day
        dislocation tail whose live fillability is unproven (51% of entries see
        one leg reprice >1c within 1s). Recorded, never traded, until the
        leg-fail telemetry earns it.

Leg-risk realism (the whole point of paper-first): entries fire as TWO takes.
A surviving-alone leg is retried briefly at a capped chase price; if still
unhedged it RIDES NAKED to settlement and a 'xwin_leg_fail' event records the
liquidation bid available at that moment — so the ledger carries the honest
worst case while the events let us price a liquidation policy offline.
"""
import asyncio
import json
import logging
import time

log = logging.getLogger("xwin")


class XwinStrategy:
    def __init__(self, cfg, clob5, clob15, oracle, exec5, exec15, ledger, risk):
        self.cfg = cfg
        self.clob5, self.clob15 = clob5, clob15
        self.oracle = oracle
        self.exec5, self.exec15 = exec5, exec15
        self.ledger, self.risk = ledger, risk
        self._done = set()          # closes C already entered/exhausted
        self._deep_logged = {}      # C -> best cost already logged (throttle)
        self.entries = 0
        self.leg_fails = 0

    async def run(self):
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("xwin tick failed: %s", e)
                await asyncio.sleep(2.0)

    async def _tick(self):
        now = time.time()
        C = int(now - now % 900 + 900)
        tau = C - now
        if tau > self.cfg.xwin_tau_max:
            await asyncio.sleep(min(tau - self.cfg.xwin_tau_max, 5.0))
            return
        if C in self._done or self.risk.halted("xwin"):
            await asyncio.sleep(0.5)
            return
        await self._evaluate(C, tau)
        await asyncio.sleep(0.25)

    # ------------------------------------------------------------------
    def _strikes(self, C):
        o5 = self.oracle.price_at(C - 300, exact=True, tolerance=2)
        o15 = self.oracle.price_at(C - 900, exact=True, tolerance=2)
        return o5, o15

    def _legs(self, C):
        """Returns (legA, legB) as (market, token, executor, clob) or None.
        legA is the 'Up on the longer-strike-dominated market' side."""
        mk5 = self.clob5.market_for(C - 300)
        mk15 = self.clob15.market_for(C - 900)
        if mk5 is None or mk15 is None:
            return None
        o5, o15 = self._strikes(C)
        if o5 is None or o15 is None or o5 == o15:
            return None
        gap_bps = (o5 - o15) / o15 * 1e4
        if o15 < o5:      # Up15 + Down5
            legA = (mk15, mk15.token_up, self.exec15, self.clob15)
            legB = (mk5, mk5.token_down, self.exec5, self.clob5)
        else:             # Up5 + Down15
            legA = (mk5, mk5.token_up, self.exec5, self.clob5)
            legB = (mk15, mk15.token_down, self.exec15, self.clob15)
        return legA, legB, gap_bps

    @staticmethod
    def _l1(st):
        """(best_ask, size_at_best) or (None, 0)."""
        if st is None or not st.asks:
            return None, 0.0
        px = min(st.asks)
        return px, st.asks[px]

    def _quote(self, legA, legB):
        stA = legA[3].state(legA[1])
        stB = legB[3].state(legB[1])
        age = self.cfg.xwin_book_max_age_s
        if (stA is None or stB is None
                or not stA.book_fresh(age) or not stB.book_fresh(age)):
            return None
        aA, sA = self._l1(stA)
        aB, sB = self._l1(stB)
        if aA is None or aB is None:
            return None
        fee = self.cfg.taker_fee_mult * (aA * (1 - aA) + aB * (1 - aB))
        return {"aA": aA, "sA": sA, "aB": aB, "sB": sB,
                "cost": aA + aB, "fee": fee}

    # ------------------------------------------------------------------
    async def _evaluate(self, C, tau):
        legs = self._legs(C)
        if legs is None:
            return
        legA, legB, gap_bps = legs
        q = self._quote(legA, legB)
        if q is None:
            return

        # DEEP tier telemetry (log-only): below-band cost or inside tau_min
        deep = (q["cost"] + q["fee"] < 1.0 and
                (q["cost"] < self.cfg.xwin_min_cost or tau < self.cfg.xwin_tau_min))
        if deep and q["cost"] < self._deep_logged.get(C, 9.9) - 0.005:
            self._deep_logged[C] = q["cost"]
            self.ledger.event("xwin_deep", json.dumps(
                {"C": C, "tau": round(tau, 1), "gap": round(gap_bps, 2),
                 "cost": round(q["cost"], 3), "szA": round(q["sA"], 1),
                 "szB": round(q["sB"], 1)}, separators=(",", ":")))

        # AUTO tier gate
        if not (tau >= self.cfg.xwin_tau_min
                and self.cfg.xwin_min_cost <= q["cost"] <= self.cfg.xwin_max_cost
                and min(q["sA"], q["sB"]) >= self.cfg.xwin_min_size):
            return

        # survival gate (paper-only pessimism, mirrors the snipe's 0.5s rule:
        # a flicker that cannot survive half a second is not a live fill)
        await asyncio.sleep(self.cfg.xwin_survival_s)
        q2 = self._quote(legA, legB)
        if (q2 is None
                or q2["cost"] > self.cfg.xwin_max_cost
                or min(q2["sA"], q2["sB"]) < self.cfg.xwin_min_size):
            self.ledger.event("xwin_gone", json.dumps(
                {"C": C, "tau": round(tau, 1),
                 "cost0": round(q["cost"], 3)}, separators=(",", ":")))
            return
        qty = min(q2["sA"], q2["sB"], self.cfg.xwin_clip)

        # fire both legs back-to-back (live would be two concurrent FAKs)
        mkA, tokA, exA, _ = legA
        mkB, tokB, exB, _ = legB
        oA = exA.take(mkA.wts, "xwin", tokA, q2["aA"], qty)
        oB = exB.take(mkB.wts, "xwin", tokB, q2["aB"], qty)
        if oA is None and oB is None:
            return                      # nothing committed; may re-qualify

        # leg repair: retry the missing/short side at a capped chase price
        fillA = oA.filled if oA else 0.0
        fillB = oB.filled if oB else 0.0
        deadline = time.time() + self.cfg.xwin_leg_retry_s
        while fillA + 0.01 < fillB and time.time() < deadline:
            o = exA.take(mkA.wts, "xwin", tokA,
                         q2["aA"] + self.cfg.xwin_max_chase, fillB - fillA)
            if o:
                fillA += o.filled
            else:
                await asyncio.sleep(0.25)
        while fillB + 0.01 < fillA and time.time() < deadline:
            o = exB.take(mkB.wts, "xwin", tokB,
                         q2["aB"] + self.cfg.xwin_max_chase, fillA - fillB)
            if o:
                fillB += o.filled
            else:
                await asyncio.sleep(0.25)

        naked = abs(fillA - fillB)
        if naked > 5.0:
            # unhedged remainder rides to settlement (honest worst case);
            # record the liquidation bid available right now for offline policy
            self.leg_fails += 1
            held = legA if fillA > fillB else legB
            held_st = held[3].state(held[1])
            liq_bid = max(held_st.bids) if held_st and held_st.bids else None
            self.ledger.event("xwin_leg_fail", json.dumps(
                {"C": C, "naked": round(naked, 1),
                 "held": "A" if fillA > fillB else "B",
                 "liq_bid": liq_bid, "cost": round(q2["cost"], 3)},
                separators=(",", ":")))
            log.warning("xwin LEG FAIL C=%s naked %.1f sh (liq bid %s)",
                        C, naked, liq_bid)

        self.entries += 1
        self._done.add(C)
        self.ledger.event("xwin_enter", json.dumps(
            {"C": C, "tau": round(tau, 1), "gap": round(gap_bps, 2),
             "cost": round(q2["cost"], 3), "fee": round(q2["fee"], 4),
             "qA": round(fillA, 1), "qB": round(fillB, 1)},
            separators=(",", ":")))
        log.info("xwin ENTER C=%s tau=%.0fs cost=%.3f gap=%.1fbps qty %.0f/%.0f",
                 C, tau, q2["cost"], gap_bps, fillA, fillB)
