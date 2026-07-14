"""Order executors. The strategies call one interface; only this layer differs
between paper and live.

PaperExecutor simulates fills against the LIVE tape and book:
  - maker limits fill from real prints that trade at-or-through our price after
    placement time; prints strictly through the price count fully, prints at the
    price count via `queue_share` (we join behind the existing queue). Partial
    fills are supported, exactly like the real CLOB.
  - takes execute against the live standing top-of-book, capped by its size.

LiveExecutor holds the py-clob-client wiring for the real-money phase. It
refuses to start without credentials and is intentionally NOT enabled by
default anywhere.
"""
import itertools
import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger("exec")
_ids = itertools.count(1)


@dataclass
class Order:
    id: int
    wts: int
    strategy: str
    token: str
    side: str            # 'buy' (all current strategies buy)
    price: float
    size: float
    placed_ts: float
    status: str = "open"          # open | cancelled | done
    filled: float = 0.0
    fees: float = 0.0
    fills: list = field(default_factory=list)   # (ts, px, sz, fee, is_maker)
    _tape_cursor: int = 0


class PaperExecutor:
    """Simulates fills from live market data. Interface: place_limit / take / cancel / poll."""

    def __init__(self, cfg, clob, ledger, queue_share=0.5):
        self.cfg = cfg
        self.clob = clob
        self.ledger = ledger
        self.queue_share = queue_share
        self.open_orders = {}

    # ---- interface ----
    def place_limit(self, wts, strategy, token, price, size):
        o = Order(id=next(_ids), wts=wts, strategy=strategy, token=token, side="buy",
                  price=round(price, 3), size=size, placed_ts=time.time())
        self.open_orders[o.id] = o
        self.ledger.record_order(o, mode="paper-limit")
        log.info("[paper] LIMIT buy %s %.0f @ %.3f (%s w%s)", token[:12], size, price,
                 strategy, wts)
        return o

    def take(self, wts, strategy, token, price_limit, size):
        st = self.clob.state(token)
        if not st or st.best_ask is None or st.best_ask > price_limit or st.best_ask_size <= 0:
            return None
        fill_sz = min(size, st.best_ask_size)
        px = st.best_ask
        fee = self.cfg.taker_fee_mult * px * (1 - px) * fill_sz
        o = Order(id=next(_ids), wts=wts, strategy=strategy, token=token, side="buy",
                  price=px, size=fill_sz, placed_ts=time.time(), status="done",
                  filled=fill_sz, fees=fee)
        o.fills.append((o.placed_ts, px, fill_sz, fee, False))
        self.ledger.record_order(o, mode="paper-take")
        self.ledger.record_fill(o, o.placed_ts, px, fill_sz, fee, maker=False)
        log.info("[paper] TAKE %s %.0f @ %.3f fee %.4f (%s w%s)", token[:12], fill_sz,
                 px, fee, strategy, wts)
        return o

    def cancel(self, order_id):
        o = self.open_orders.pop(order_id, None)
        if o and o.status == "open":
            o.status = "cancelled" if o.filled == 0 else "done"
            self.ledger.close_order(o)

    # ---- fill engine: call frequently (strategy ticks / main loop) ----
    def poll_fills(self):
        for o in list(self.open_orders.values()):
            st = self.clob.state(o.token)
            if st is None:
                continue
            tape = list(st.prints)
            new = tape[o._tape_cursor:]
            o._tape_cursor = len(tape)
            for ts, px, sz, side in new:
                if ts < o.placed_ts or o.filled >= o.size:
                    continue
                if px > o.price + 1e-9:
                    continue
                # through-price prints are ours by price priority; at-price prints
                # are shared with the pre-existing queue
                share = 1.0 if px < o.price - 1e-9 else self.queue_share
                take = min(sz * share, o.size - o.filled)
                if take <= 0:
                    continue
                fee = self.cfg.maker_fee_mult * o.price * (1 - o.price) * take
                o.filled += take
                o.fees += fee
                o.fills.append((ts, o.price, take, fee, True))
                self.ledger.record_fill(o, ts, o.price, take, fee, maker=True)
                log.info("[paper] maker fill %.1f/%.0f @ %.3f (%s w%s)", o.filled,
                         o.size, o.price, o.strategy, o.wts)
            if o.filled >= o.size:
                o.status = "done"
                self.open_orders.pop(o.id, None)
                self.ledger.close_order(o)


class LiveExecutor:
    """Real-money executor (py-clob-client). Deliberately fails closed."""

    def __init__(self, cfg, clob, ledger):
        if not (cfg.pm_private_key and cfg.pm_api_key):
            raise RuntimeError(
                "LiveExecutor requires PM_PRIVATE_KEY/PM_API_KEY/... env vars. "
                "Run the paper executor until the strategy is re-qualified live.")
        try:
            from py_clob_client.client import ClobClient  # noqa: WPS433
        except ImportError as e:
            raise RuntimeError("pip install py-clob-client to trade live") from e
        self.client = ClobClient(
            cfg.clob_url, key=cfg.pm_private_key, chain_id=137,
            creds={"api_key": cfg.pm_api_key, "api_secret": cfg.pm_api_secret,
                   "api_passphrase": cfg.pm_api_passphrase},
            funder=cfg.pm_funder or None)
        self.cfg, self.clob, self.ledger = cfg, clob, ledger
        raise RuntimeError("LiveExecutor wiring is present but intentionally not "
                           "enabled: qualify the paper bot first, then remove this "
                           "guard consciously.")
