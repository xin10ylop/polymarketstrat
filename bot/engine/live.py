"""Real-money executor + bankroll management for Polymarket.

SAFETY MODEL — three independent locks, all must open before a real order:
  1. cfg.mode == "live" AND BANKROLL > 0 AND credentials present;
  2. LIVE_CONFIRM=I-UNDERSTAND-REAL-MONEY (a human typed this on purpose) —
     required only to POST real orders; shadow mode runs without it;
  3. LIVE_SHADOW=0 — shadow mode is the DEFAULT: everything runs INCLUDING
     order construction and signing (so credentials/allowances/tick sizes are
     exercised), except the final POST, which is logged instead.

Money rules enforced HERE, not in strategies:
  - per-trade cost <= bankroll * live_per_trade_frac (default 10%), computed
    at the LIMIT price — a FAK sweeps to the limit, so sizing off best_ask
    could overspend the cap by multiples (audit 2026-07-30 finding #3)
  - one in-flight position at a time; max trades/day cap (persisted: restart
    does not reset the day's count)
  - only strategies named in LIVE_STRATEGIES may trade (default: snipe)
  - every fill records the REAL matched size/price from the exchange response;
    an AMBIGUOUS response (matched but amounts unparseable) is recorded as a
    provisional fill at the worst case AND trips a sticky halt — never
    silently dropped (audit finding #2)
The spend-side reconciler (hourly) compares actual on-exchange balance against
the ledger's expectation and trips a REAL sticky halt via RiskManager on any
unexplained shortfall (audit finding #1).
"""
import asyncio
import itertools
import logging
import time

from bot.engine.executor import Order

log = logging.getLogger("live")
_ids = itertools.count(1_000_000)          # distinct id space from paper orders


class Bankroll:
    def __init__(self, cfg):
        self.cfg = cfg
        self.amount = cfg.bankroll
        if cfg.mode == "live" and self.amount <= 0:
            raise RuntimeError("live mode requires BANKROLL > 0 (dollars)")

    @property
    def per_trade_cap(self):
        return self.amount * self.cfg.live_per_trade_frac

    @property
    def daily_stop(self):
        return self.amount * self.cfg.live_daily_stop_frac


class LiveExecutor:
    """Same interface as PaperExecutor; the toll's maker path is deliberately
    NOT enabled live until the snipe phase qualifies (LIVE_STRATEGIES)."""

    def __init__(self, cfg, clob, ledger, bankroll, risk):
        if not cfg.live_shadow and cfg.live_confirm != "I-UNDERSTAND-REAL-MONEY":
            raise RuntimeError(
                "posting real orders needs LIVE_CONFIRM=I-UNDERSTAND-REAL-MONEY "
                "set by a human (shadow mode runs without it)")
        if not cfg.pm_private_key:
            raise RuntimeError("live mode needs PM_PRIVATE_KEY (never commit it; "
                               "put it in the EnvironmentFile with chmod 600)")
        # CLOB V2 (Apr 28 2026 migration): the archived v1 client signs an order
        # struct the exchange no longer accepts — only py-clob-client-v2 works
        import py_clob_client_v2.client as _clobmod
        from py_clob_client_v2.client import ClobClient  # noqa: WPS433
        # Bound the client's post-fill trade-hash polling: FAK matches return
        # tradeIDs without transactionsHashes (Jul 17 2026 change), which sends
        # the stock client into a poll loop of up to 30s per fill. We don't
        # need hashes at fill time; 2s captures the fast case and moves on.
        if hasattr(_clobmod, "RESOLVE_TRADES_TIMEOUT_SECONDS"):
            _clobmod.RESOLVE_TRADES_TIMEOUT_SECONDS = 2.0
        self.cfg, self.clob, self.ledger, self.bankroll = cfg, clob, ledger, bankroll
        self.risk = risk
        self.shadow = cfg.live_shadow
        self.client = ClobClient(
            cfg.clob_url, chain_id=137, key=cfg.pm_private_key,
            signature_type=cfg.pm_signature_type or None,
            funder=cfg.pm_funder or None)
        self.client.set_api_creds(self.client.create_or_derive_api_key())
        self.open_orders = {}
        self._in_flight = False
        self._warmed = set()
        self._day = self._utc_day()
        self._start_ts = time.time()
        # persist the trades/day cap across restarts (audit finding #14)
        day0 = time.time() - time.time() % 86400
        self._trades_today = self.ledger.db.execute(
            "SELECT COUNT(*) FROM fills WHERE ts >= ?", (day0,)).fetchone()[0]
        self.start_balance = self.fetch_balance()
        if self.start_balance < 0:
            raise RuntimeError("cannot read exchange balance at startup — fix "
                               "credentials/connectivity before running")
        if not self.shadow and self.start_balance < bankroll.amount:
            raise RuntimeError(
                f"exchange balance ${self.start_balance:.2f} < BANKROLL "
                f"${bankroll.amount:.2f}. Redeem winnings / deposit, or lower "
                "BANKROLL (ladder down) — the bot never trades money it can't see")
        self.ledger.event("live_start",
                          f"shadow={int(self.shadow)} bankroll={bankroll.amount} "
                          f"balance={self.start_balance} trades_today={self._trades_today}")
        log.warning("LIVE executor up: shadow=%s bankroll=$%.0f balance=$%.2f "
                    "per_trade_cap=$%.2f strategies=%s", self.shadow,
                    bankroll.amount, self.start_balance,
                    bankroll.per_trade_cap, cfg.live_strategies)

    # ------------------------------------------------------------------
    @staticmethod
    def _utc_day():
        return int(time.time() // 86400)

    def _day_roll(self):
        d = self._utc_day()
        if d != self._day:
            self._day, self._trades_today = d, 0

    def fetch_balance(self):
        """Collateral (pUSD, 1:1 USDC) balance on the exchange, in dollars."""
        try:
            from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
            r = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            return float(r.get("balance", 0)) / 1e6      # pUSD has 6 decimals
        except Exception as e:  # noqa: BLE001
            log.error("balance fetch failed: %s", e)
            return -1.0

    def prewarm_token(self, token):
        """Warm the client's per-token tick-size and neg-risk caches at market
        discovery time — otherwise the FIRST take of every window pays two
        uncached REST round-trips before signing, in a race where the median
        qualifying ask survives ~142ms (audit finding #11)."""
        if token in self._warmed:
            return
        try:
            self.client.get_tick_size(token)
            self.client.get_neg_risk(token)
            self._warmed.add(token)
        except Exception as e:  # noqa: BLE001
            log.warning("prewarm failed for %s: %s", token[:12], e)

    # ---- interface ----------------------------------------------------
    def take(self, wts, strategy, token, price_limit, size):
        self._day_roll()
        if strategy not in self.cfg.live_strategies:
            return None
        if self._in_flight:
            return None
        if self._trades_today >= self.cfg.live_max_trades_day:
            log.warning("live: daily trade cap reached")
            return None
        st = self.clob.state(token)
        if (st is None or not st.book_fresh(self.cfg.book_max_age_s)
                or st.best_ask is None or st.best_ask > price_limit
                or st.best_ask_size <= 0):
            return None
        # cap by cost AT THE LIMIT: a FAK sweeps every level <= price_limit,
        # so this is the only price that bounds worst-case spend
        cap_sz = self.bankroll.per_trade_cap / max(price_limit, 0.01)
        fill_sz = round(min(size, cap_sz), 2)
        if fill_sz < 5:                                  # exchange minimum
            return None
        # fill forensics (Tier 0's key measurement): the triggering book vs
        # what we actually pay tells us whether deep-band edge survives live
        levels = sorted((p, s) for p, s in st.asks.items() if p <= price_limit + 1e-9)
        self.ledger.event("live_forensics",
                          f"w{wts} trigger_ask={st.best_ask:.3f}x{st.best_ask_size:.0f} "
                          f"req={fill_sz} limit={price_limit} ladder={levels[:6]}")

        self._in_flight = True
        try:
            from py_clob_client_v2.clob_types import OrderArgs, OrderType
            from py_clob_client_v2.order_builder.constants import BUY
            # V2: no fee_rate_bps/nonce — fees are protocol-computed at match time
            args = OrderArgs(token_id=token, price=round(price_limit, 3),
                             size=fill_sz, side=BUY)
            signed = self.client.create_order(args)      # signs in shadow too

            if self.shadow:
                # shadow exercised discovery, sizing, tick/neg-risk resolution
                # and SIGNING — everything except the POST (audit finding #8)
                self.ledger.event("shadow_take",
                                  f"w{wts} {strategy} {token[:12]} {fill_sz}@<= {price_limit}")
                log.warning("[SHADOW] signed+would take %s %.0f @ <=%.3f (%s w%s)",
                            token[:12], fill_sz, price_limit, strategy, wts)
                return None

            # Durable intent BEFORE the POST (audit C5): if the process dies or
            # the connection drops AFTER the order matched on-chain, this row
            # is the only evidence a position may exist.
            self.ledger.event("live_pending",
                              f"w{wts} {strategy} {token[:12]} {fill_sz}@<={price_limit}")
            try:
                resp = self.client.post_order(signed, OrderType.FAK)
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "no orders found to match" in msg:
                    self.ledger.event("live_miss", f"w{wts} FAK empty (raced)")
                    return None                    # clean miss: rival got there first
                if any(k in msg for k in ("Too Many Requests", "post-only mode",
                                          "Trading is currently", "425")):
                    self.ledger.event("live_backoff", f"w{wts} {msg[:80]}")
                    time.sleep(2)     # worker thread: does not block the loop
                    return None
                # AMBIGUOUS TRANSPORT FAILURE (timeout/reset/5xx): the order may
                # have matched. Book the same worst-case provisional fill as an
                # unparseable response — never silently drop a possible position.
                self._trades_today += 1
                o = Order(id=next(_ids), wts=wts, strategy=strategy, token=token,
                          side="buy", price=price_limit, size=fill_sz,
                          placed_ts=time.time(), status="done", filled=fill_sz,
                          fees=self.cfg.taker_fee_mult * price_limit
                          * (1 - price_limit) * fill_sz)
                try:
                    self.ledger.record_order(o, mode="LIVE-take-UNCONFIRMED")
                    self.ledger.record_fill(o, o.placed_ts, price_limit, fill_sz,
                                            o.fees, maker=False)
                finally:
                    self.ledger.event("live_unconfirmed",
                                      f"w{wts} POST raised: {msg[:160]}")
                    self.risk.halt("all", "order POST failed ambiguously — "
                                   "reconcile against the exchange before restarting")
                log.error("LIVE POST AMBIGUOUS w%s (%s): booked worst-case "
                          "%.1f@%.3f, halted", wts, msg[:100], fill_sz, price_limit)
                return o
            matched, avg_px = self._parse_fill(resp, price_limit)
            if matched < 0:
                # CONFIRMED-BUT-AMBIGUOUS: the exchange says matched/delayed or
                # returned tradeIDs, but amounts didn't parse. Money moved.
                # Book the worst case, halt sticky, and make a human reconcile.
                self._trades_today += 1
                o = Order(id=next(_ids), wts=wts, strategy=strategy, token=token,
                          side="buy", price=price_limit, size=fill_sz,
                          placed_ts=time.time(), status="done", filled=fill_sz,
                          fees=self.cfg.taker_fee_mult * price_limit
                          * (1 - price_limit) * fill_sz)
                self.ledger.record_order(o, mode="LIVE-take-UNCONFIRMED")
                self.ledger.record_fill(o, o.placed_ts, price_limit, fill_sz,
                                        o.fees, maker=False)
                self.ledger.event("live_unconfirmed",
                                  f"w{wts} resp={str(resp)[:160]}")
                self.risk.halt("all", "unconfirmed live fill (amounts unparseable) "
                               "— reconcile against the exchange before restarting")
                log.error("LIVE UNCONFIRMED FILL w%s: booked worst-case %.1f@%.3f, "
                          "halted. Response: %s", wts, fill_sz, price_limit,
                          str(resp)[:200])
                return o
            if matched == 0:
                self.ledger.event("live_miss", f"w{wts} FAK no fill ({resp})")
                log.info("live: FAK missed (%s)", str(resp)[:120])
                return None
            self._trades_today += 1
            fee_est = self.cfg.taker_fee_mult * avg_px * (1 - avg_px) * matched
            o = Order(id=next(_ids), wts=wts, strategy=strategy, token=token,
                      side="buy", price=avg_px, size=matched,
                      placed_ts=time.time(), status="done", filled=matched,
                      fees=fee_est)
            self.ledger.record_order(o, mode="LIVE-take")
            self.ledger.record_fill(o, o.placed_ts, avg_px, matched, fee_est,
                                    maker=False)
            cost = matched * avg_px
            if cost > self.bankroll.per_trade_cap * 1.05:
                # tripwire: should be unreachable with limit-price sizing;
                # if it fires, the cap model is wrong — stop everything
                self.risk.halt("all", f"per-trade cap exceeded: spent {cost:.2f} "
                               f"> cap {self.bankroll.per_trade_cap:.2f}")
            log.warning("LIVE FILL %s %.1f @ %.3f (trigger ask %.3f) est-fee %.4f "
                        "(%s w%s)", token[:12], matched, avg_px, st.best_ask,
                        fee_est, strategy, wts)
            return o
        except Exception as e:  # noqa: BLE001
            # an UNEXPECTED order error (insufficient balance, allowance, tick
            # size, ban...) is not a race loss — it will recur. Fail loud and
            # stop until a human looks (audit finding #10).
            self.ledger.event("live_error", f"w{wts} take: {e}")
            self.risk.halt("all", f"unexpected order error: {str(e)[:120]}")
            log.exception("live take failed (halted): %s", e)
            return None
        finally:
            self._in_flight = False

    @staticmethod
    def _parse_fill(resp, fallback_px):
        """Extract matched size and average price from a POST /order response.

        V2 schema: {success, orderID, status: live|matched|delayed,
        makingAmount, takingAmount, tradeIDs, ...} — amounts are fixed-point
        strings with 6 decimals; BUY: taking=outcome shares, making=pUSD spent.
        transactionsHashes is no longer returned for FAK matches (Jul 17 2026
        changelog) — never key on it.

        Returns (shares, avg_px); shares == -1.0 means CONFIRMED-BUT-AMBIGUOUS
        (matched/delayed/tradeIDs present but amounts unparseable) — the caller
        MUST treat that as a real position, never as a miss."""
        if not isinstance(resp, dict):
            return 0.0, fallback_px
        try:
            shares = float(resp.get("takingAmount") or 0) / 1e6
            usdc = float(resp.get("makingAmount") or 0) / 1e6
            if shares > 0:
                return shares, (usdc / shares if usdc > 0 else fallback_px)
        except (TypeError, ValueError):
            pass
        status = str(resp.get("status", "")).lower()
        if status in ("matched", "delayed") or resp.get("tradeIDs"):
            return -1.0, fallback_px                     # confirmed, size unknown
        return 0.0, fallback_px

    # maker path: not enabled in phase 1 (LIVE_STRATEGIES gates it anyway)
    def place_limit(self, wts, strategy, token, price, size):
        if strategy not in self.cfg.live_strategies:
            return _NullOrder()
        raise RuntimeError("live maker path not qualified yet (phase 1 = snipe only)")

    def cancel(self, order_id):
        pass

    def poll_fills(self):
        pass


class _NullOrder:
    id = -1
    wts = 0
    strategy = ""
    token = ""
    status = "cancelled"
    filled = 0.0
    size = 0.0
    price = 0.0
    fees = 0.0
    fills = ()


async def prewarm_loop(clob, executor):
    """Warm tick-size/neg-risk caches for every discovered market's tokens,
    off the event loop, so the order path never pays that latency."""
    while True:
        try:
            for mk in list(clob.markets.values()):
                for token in (mk.token_up, mk.token_down):
                    if token not in executor._warmed:
                        await asyncio.to_thread(executor.prewarm_token, token)
        except Exception as e:  # noqa: BLE001
            log.warning("prewarm loop: %s", e)
        await asyncio.sleep(5)


async def balance_reconciler(cfg, ledger, executor, risk):
    """Hourly spend-side check: actual balance must never be lower than
    (start - everything the ledger says we spent SINCE THIS PROCESS STARTED)
    by more than tolerance. Redemptions only ADD funds, so a shortfall means a
    hidden cost or a bug — trips a REAL sticky halt via RiskManager.
    Also warns when buying power runs low (unredeemed winnings are not
    collateral — redeem daily)."""
    TOL = 0.10
    while True:
        await asyncio.sleep(3600)
        actual = await asyncio.to_thread(executor.fetch_balance)
        if actual < 0:
            ledger.event("reconciler_skip", "balance unreadable")
            continue
        spent = ledger.db.execute(
            "SELECT COALESCE(SUM(price*size + fee), 0) FROM fills "
            "WHERE ts >= ?", (executor._start_ts,)).fetchone()[0]
        floor = executor.start_balance - spent - TOL
        if actual < floor:
            risk.halt("all", f"balance reconciler: actual {actual:.2f} < "
                      f"floor {floor:.2f} (spent {spent:.2f} since start)")
            log.error("RECONCILER HALT: balance %.2f below floor %.2f", actual, floor)
        else:
            log.info("reconciler ok: balance %.2f, ledger spend %.2f", actual, spent)
            if actual < 2 * executor.bankroll.per_trade_cap:
                ledger.event("low_buying_power", f"balance {actual:.2f}")
                log.warning("LOW BUYING POWER $%.2f — redeem winnings "
                            "(unredeemed positions are not collateral)", actual)
