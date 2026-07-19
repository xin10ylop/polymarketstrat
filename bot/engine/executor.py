"""Order executors. The strategies call one interface; only this layer differs
between paper and live.

PaperExecutor simulates fills against the LIVE tape and book under strict
price-time priority:
  - at placement, an order snapshots queue_ahead = all resting bid size at or
    above its price (the competitor "wall" plus anyone else already in line);
  - real prints at-or-through our price consume that queue FIRST; only the
    overflow fills us. This is what a real order experiences on the CLOB —
    the old queue_share=0.5 model ignored standing depth and overstated toll
    fills by orders of magnitude when a large incumbent is present.
  - a shared per-print pool prevents two of our own orders double-spending
    one print; takes execute against the live ladder top (freshness-gated)
    and decrement the simulated book.

LiveExecutor holds the py-clob-client wiring for the real-money phase; it
fails closed until deliberately enabled.
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
    side: str
    price: float
    size: float
    placed_ts: float
    status: str = "open"
    filled: float = 0.0
    fees: float = 0.0
    fills: list = field(default_factory=list)
    last_seq: int = 0                 # print sequence cursor (survives eviction)
    queue_ahead: float = 0.0          # resting size at-or-above our price at placement


class PaperExecutor:
    def __init__(self, cfg, clob, ledger):
        self.cfg = cfg
        self.clob = clob
        self.ledger = ledger
        self.open_orders = {}
        self._pool = {}               # (token, seq) -> remaining unallocated size

    # ---- interface ----
    def place_limit(self, wts, strategy, token, price, size):
        st = self.clob.state(token)
        q_ahead = 0.0
        if st is not None:
            q_ahead = sum(sz for px, sz in st.bids.items() if px >= price - 1e-9)
        o = Order(id=next(_ids), wts=wts, strategy=strategy, token=token, side="buy",
                  price=round(price, 3), size=size, placed_ts=time.time(),
                  last_seq=st.print_seq if st else 0,   # only future prints count
                  queue_ahead=q_ahead)
        self.open_orders[o.id] = o
        self.ledger.record_order(o, mode="paper-limit")
        log.info("[paper] LIMIT buy %s %.0f @ %.3f behind %.0f queued (%s w%s)",
                 token[:12], size, price, q_ahead, strategy, wts)
        return o

    def take(self, wts, strategy, token, price_limit, size):
        """Marketable-limit semantics: sweep every ask level <= price_limit,
        cheapest first, up to `size` — exactly what a real FAK at that limit
        does on the CLOB (the old version stopped at top-of-book)."""
        st = self.clob.state(token)
        if (st is None or not st.book_fresh(self.cfg.book_max_age_s)
                or st.best_ask is None or st.best_ask > price_limit):
            return None
        remaining = size
        total_sz = total_cost = total_fee = 0.0
        now = time.time()
        fills = []
        for px in sorted(p for p in list(st.asks) if p <= price_limit + 1e-9):
            if remaining <= 0:
                break
            avail = st.asks.get(px, 0.0)
            take_sz = min(avail, remaining)
            if take_sz <= 0:
                continue
            fee = self.cfg.taker_fee_mult * px * (1 - px) * take_sz
            fills.append((now, px, take_sz, fee, False))
            total_sz += take_sz
            total_cost += px * take_sz
            total_fee += fee
            remaining -= take_sz
            st.asks[px] = avail - take_sz                # consume simulated liquidity
            if st.asks[px] <= 0:
                st.asks.pop(px, None)
        if total_sz <= 0:
            return None
        avg_px = total_cost / total_sz
        o = Order(id=next(_ids), wts=wts, strategy=strategy, token=token, side="buy",
                  price=round(avg_px, 4), size=total_sz, placed_ts=now, status="done",
                  filled=total_sz, fees=total_fee)
        o.fills = fills
        self.ledger.record_order(o, mode="paper-take")
        for ts, px, sz, fee, mk in fills:
            self.ledger.record_fill(o, ts, px, sz, fee, maker=False, commit=False)
        self.ledger.commit()
        log.info("[paper] TAKE %s %.1f @ avg %.3f (%d levels) fee %.4f (%s w%s)",
                 token[:12], total_sz, avg_px, len(fills), total_fee, strategy, wts)
        return o

    def cancel(self, order_id):
        o = self.open_orders.pop(order_id, None)
        if o and o.status == "open":
            o.status = "cancelled" if o.filled == 0 else "done"
            self.ledger.close_order(o)

    # ---- fill engine ----
    def poll_fills(self):
        # placement order => price/time priority among our own orders
        orders = sorted(self.open_orders.values(), key=lambda o: o.placed_ts)
        touched = False
        for o in orders:
            st = self.clob.state(o.token)
            if st is None:
                continue
            for seq, ts, px, sz, _side in st.prints:
                if seq <= o.last_seq:
                    continue
                o.last_seq = seq
                if ts < o.placed_ts or o.filled >= o.size or px > o.price + 1e-9:
                    continue
                # FIFO: this print first pays down the queue that was resting
                # ahead of us at placement; only the overflow can fill us
                if o.queue_ahead > 0:
                    eaten = min(o.queue_ahead, sz)
                    o.queue_ahead -= eaten
                    sz -= eaten
                    if sz <= 0:
                        continue
                key = (o.token, seq)
                pool = self._pool.get(key, sz)
                take = min(pool, o.size - o.filled)
                if take <= 0:
                    continue
                self._pool[key] = pool - take
                fee = self.cfg.maker_fee_mult * o.price * (1 - o.price) * take
                o.filled += take
                o.fees += fee
                o.fills.append((ts, o.price, take, fee, True))
                self.ledger.record_fill(o, ts, o.price, take, fee, maker=True,
                                        commit=False)
                touched = True
                log.info("[paper] maker fill %.1f/%.0f @ %.3f (%s w%s)", o.filled,
                         o.size, o.price, o.strategy, o.wts)
            if o.filled >= o.size:
                o.status = "done"
                self.open_orders.pop(o.id, None)
                self.ledger.close_order(o)
        if touched:
            self.ledger.commit()
        if len(self._pool) > 20000:      # prune allocation pool
            self._pool = dict(list(self._pool.items())[-5000:])


# The real-money executor lives in bot/engine/live.py (LiveExecutor + Bankroll
# + balance_reconciler) behind three independent safety locks.
