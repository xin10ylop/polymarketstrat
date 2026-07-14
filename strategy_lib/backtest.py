"""Maker-fill backtester against the real trade tape.

Positions are always expressed in Up-token terms internally:
  token='up':   buy Up at price p   -> resting bid p, hit by aggressor SELLS at price <= p
  token='down': buy Down at price q -> resting Up-ask at 1-q, hit by aggressor BUYS at price >= 1-q

Exits:
  target: resting maker sell of the held token at `target` (complement logic as above)
  stop_taker: if mid moves against beyond stop, cross the spread (taker, pays fee)
  settle: hold to resolution -> 1{token won}

Fill rule (conservative): an entry/exit resting order is filled when cumulative
qualifying aggressor volume since placement >= queue_mult * order_shares.
Qualifying = trades strictly through our price, or at our price.
"""
import os
import sys
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from strategy_lib.data import DATA, FAMILY_DURATION, CURRENT_TAKER_RATE, taker_fee


@dataclass
class Order:
    wts: int
    token: str            # 'up' or 'down'
    limit_px: float       # in token's own price terms
    t_place: float        # rel seconds vs window open
    t_cancel: float       # cancel entry if unfilled by then
    exit_target: Optional[float] = None   # token-terms target for maker exit
    exit_deadline: Optional[float] = None # rel s; after this, if no target fill -> settle or taker-out
    exit_mode: str = "settle"             # what happens at deadline: 'settle' | 'taker'
    shares: float = 10.0


def _prep_day(trades_df):
    """Group one day's tape by wts into numpy arrays."""
    out = {}
    df = trades_df.sort_values("timestamp_us")
    rel = df.timestamp_us.to_numpy() / 1e6 - df.wts.to_numpy()
    price = df.price.to_numpy().astype("float64")
    size = df["size"].to_numpy().astype("float64")
    is_buy = (df.side == "buy").to_numpy()
    wts = df.wts.to_numpy()
    order = np.argsort(wts, kind="stable")
    wts_s = wts[order]
    bounds = np.searchsorted(wts_s, np.unique(wts_s))
    uniq = np.unique(wts_s)
    idx = np.searchsorted(wts_s, uniq)
    idx = np.append(idx, len(wts_s))
    for i, w in enumerate(uniq):
        sl = order[idx[i]:idx[i + 1]]
        sl = sl[np.argsort(rel[sl], kind="stable")]
        out[int(w)] = (rel[sl], price[sl], size[sl], is_buy[sl])
    return out


def _maker_fill(rel, price, size, is_buy, side_up_bid, px, t_from, t_until, shares, queue_mult):
    """Return fill time or None.

    side_up_bid=True: we rest a bid on Up at px -> qualifying: aggressor sells at price <= px.
    side_up_bid=False: we rest an ask on Up at px -> qualifying: aggressor buys at price >= px.
    """
    m = (rel > t_from) & (rel <= t_until)
    if side_up_bid:
        m &= (~is_buy) & (price <= px + 1e-9)
    else:
        m &= is_buy & (price >= px - 1e-9)
    if not m.any():
        return None
    csum = np.cumsum(size[m])
    need = queue_mult * shares
    k = np.searchsorted(csum, need)
    if k >= csum.size:
        return None
    return rel[m][k]


class TapeBacktester:
    def __init__(self, family, queue_mult=1.0, taker_rate=CURRENT_TAKER_RATE):
        self.family = family
        self.T = FAMILY_DURATION[family]
        self.queue_mult = queue_mult
        self.taker_rate = taker_rate
        self.windows = pd.read_parquet(os.path.join(DATA, "windows_full.parquet"))
        self.windows = self.windows[self.windows.family == family]
        self.result = dict(zip(self.windows.wts, self.windows.result))

    def _day_tape(self, day):
        if self.family == "1h":
            from scripts.build_features import load_1h_day  # noqa
            df = load_1h_day("trades", day)
        else:
            p = os.path.join(DATA, "daily", self.family, "trades", f"{day}.parquet")
            if not os.path.exists(p):
                return {}
            df = pd.read_parquet(p)
        if df is None or not len(df):
            return {}
        return _prep_day(df)

    def run(self, orders: pd.DataFrame, grid: Optional[pd.DataFrame] = None):
        """orders: columns of Order. Returns per-order results with pnl in $ per share."""
        orders = orders.copy()
        orders["day"] = pd.to_datetime(orders.wts, unit="s", utc=True).dt.strftime("%Y-%m-%d")
        results = []
        for day, sub in orders.groupby("day"):
            tape = self._day_tape(day)
            for o in sub.itertuples():
                res = self._run_one(o, tape.get(int(o.wts)))
                results.append(res)
        return pd.DataFrame(results)

    def _run_one(self, o, tape):
        out = {"wts": o.wts, "token": o.token, "filled": False, "pnl": 0.0,
               "entry_t": np.nan, "exit_t": np.nan, "exit_kind": ""}
        if tape is None:
            return out
        rel, price, size, is_buy = tape
        up_won = self.result.get(int(o.wts))
        if up_won is None:
            return out
        token_wins = (up_won == 0) if o.token == "up" else (up_won == 1)

        if o.token == "up":
            entry_up_bid, entry_px_up = True, o.limit_px
        else:
            entry_up_bid, entry_px_up = False, 1.0 - o.limit_px

        t_fill = _maker_fill(rel, price, size, is_buy, entry_up_bid, entry_px_up,
                             o.t_place, o.t_cancel, o.shares, self.queue_mult)
        if t_fill is None:
            return out
        out["filled"] = True
        out["entry_t"] = t_fill
        cost = o.limit_px

        # exit: maker target first
        if o.exit_target is not None:
            deadline = o.exit_deadline if o.exit_deadline is not None else self.T + 45
            if o.token == "up":
                exit_up_bid, exit_px_up = False, o.exit_target
            else:
                exit_up_bid, exit_px_up = True, 1.0 - o.exit_target
            t_exit = _maker_fill(rel, price, size, is_buy, exit_up_bid, exit_px_up,
                                 t_fill, deadline, o.shares, self.queue_mult)
            if t_exit is not None:
                out["exit_t"] = t_exit
                out["exit_kind"] = "target"
                out["pnl"] = o.exit_target - cost
                return out
            if o.exit_mode == "taker":
                # cross at last trade price before deadline as proxy for the touch
                m = rel <= deadline
                px = price[m][-1] if m.any() else (1.0 if token_wins else 0.0)
                token_px = px if o.token == "up" else 1.0 - px
                fee = taker_fee(token_px, self.taker_rate)
                out["exit_t"] = deadline
                out["exit_kind"] = "taker"
                out["pnl"] = token_px - cost - fee
                return out
        # settle
        out["exit_kind"] = "settle"
        out["pnl"] = (1.0 if token_wins else 0.0) - cost
        return out
