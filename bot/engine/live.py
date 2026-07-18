"""Real-money executor + bankroll management for Polymarket.

SAFETY MODEL — three independent locks, all must open before a real order:
  1. cfg.mode == "live" AND BANKROLL > 0 AND credentials present;
  2. LIVE_CONFIRM=I-UNDERSTAND-REAL-MONEY (a human typed this on purpose);
  3. LIVE_SHADOW=0 — shadow mode is the DEFAULT: everything runs (signing,
     sizing, ledger) except the final order POST, which is logged instead.

Money rules enforced HERE, not in strategies:
  - per-trade cost <= bankroll * live_per_trade_frac (default 10%)
  - one in-flight position at a time; max trades/day cap
  - only strategies named in LIVE_STRATEGIES may trade (default: snipe)
  - every fill records the REAL matched size/price from the exchange response
The spend-side reconciler (hourly) compares actual on-exchange balance against
the ledger's expectation and halts on any unexplained shortfall.
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

    def __init__(self, cfg, clob, ledger, bankroll):
        if cfg.live_confirm != "I-UNDERSTAND-REAL-MONEY":
            raise RuntimeError(
                "live mode needs LIVE_CONFIRM=I-UNDERSTAND-REAL-MONEY set by a human")
        if not cfg.pm_private_key:
            raise RuntimeError("live mode needs PM_PRIVATE_KEY (never commit it; "
                               "put it in the EnvironmentFile with chmod 600)")
        from py_clob_client.client import ClobClient  # noqa: WPS433
        self.cfg, self.clob, self.ledger, self.bankroll = cfg, clob, ledger, bankroll
        self.shadow = cfg.live_shadow
        self.client = ClobClient(
            cfg.clob_url, chain_id=137, key=cfg.pm_private_key,
            signature_type=cfg.pm_signature_type or None,
            funder=cfg.pm_funder or None)
        self.client.set_api_creds(self.client.create_or_derive_api_creds())
        self.open_orders = {}
        self._in_flight = False
        self._trades_today = 0
        self._day = self._utc_day()
        self.start_balance = self.fetch_balance()
        self.ledger.event("live_start",
                          f"shadow={int(self.shadow)} bankroll={bankroll.amount} "
                          f"balance={self.start_balance}")
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
        """Collateral (USDC) balance on the exchange, in dollars."""
        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
            r = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            return float(r.get("balance", 0)) / 1e6      # USDC has 6 decimals
        except Exception as e:  # noqa: BLE001
            log.error("balance fetch failed: %s", e)
            return -1.0

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
        px = st.best_ask
        cap_sz = self.bankroll.per_trade_cap / max(px, 0.01)
        fill_sz = round(min(size, st.best_ask_size, cap_sz), 2)
        if fill_sz < 5:                                  # exchange minimum
            return None

        if self.shadow:
            self.ledger.event("shadow_take",
                              f"w{wts} {strategy} {token[:12]} {fill_sz}@<= {price_limit}")
            log.warning("[SHADOW] would take %s %.0f @ <=%.3f (%s w%s)",
                        token[:12], fill_sz, price_limit, strategy, wts)
            return None

        self._in_flight = True
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY
            args = OrderArgs(token_id=token, price=round(price_limit, 3),
                             size=fill_sz, side=BUY, fee_rate_bps=1000)
            signed = self.client.create_order(args)
            resp = self.client.post_order(signed, OrderType.FAK)
            matched, avg_px = self._parse_fill(resp, price_limit)
            if matched <= 0:
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
            log.warning("LIVE FILL %s %.1f @ %.3f est-fee %.4f (%s w%s)",
                        token[:12], matched, avg_px, fee_est, strategy, wts)
            return o
        except Exception as e:  # noqa: BLE001
            self.ledger.event("live_error", f"w{wts} take: {e}")
            log.exception("live take failed: %s", e)
            return None
        finally:
            self._in_flight = False

    @staticmethod
    def _parse_fill(resp, fallback_px):
        """Extract matched size and average price from a post_order response."""
        if not isinstance(resp, dict):
            return 0.0, fallback_px
        if not resp.get("success", False):
            return 0.0, fallback_px
        taking = resp.get("takingAmount") or resp.get("taking_amount")
        making = resp.get("makingAmount") or resp.get("making_amount")
        try:
            shares = float(taking) if taking else 0.0    # BUY: taking = outcome tokens
            usdc = float(making) if making else 0.0      # making = collateral spent
            if shares > 0:
                return shares, (usdc / shares if usdc > 0 else fallback_px)
        except (TypeError, ValueError):
            pass
        status = str(resp.get("status", "")).lower()
        if status in ("matched", "success"):
            return -1.0, fallback_px                     # matched, size unknown
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
    status = "cancelled"
    filled = 0.0
    size = 0.0
    price = 0.0
    fills = ()


async def balance_reconciler(cfg, ledger, executor):
    """Hourly spend-side check: actual balance must never be lower than
    (start - everything the ledger says we spent) by more than tolerance.
    Redemptions only ADD funds, so a shortfall means a hidden cost or a bug —
    halt-worthy either way. Raises awareness, then a sticky halt via ledger."""
    TOL = 0.10
    while True:
        await asyncio.sleep(3600)
        actual = executor.fetch_balance()
        if actual < 0:
            continue
        spent = ledger.db.execute(
            "SELECT COALESCE(SUM(price*size + fee), 0) FROM fills "
            "WHERE ts >= ?", (0,)).fetchone()[0]
        floor = executor.start_balance - spent - TOL
        if actual < floor:
            ledger.event("HALT", f"balance reconciler: actual {actual:.2f} < "
                         f"floor {floor:.2f} (spent {spent:.2f})")
            log.error("RECONCILER HALT: balance %.2f below floor %.2f", actual, floor)
        else:
            log.info("reconciler ok: balance %.2f, ledger spend %.2f", actual, spent)
