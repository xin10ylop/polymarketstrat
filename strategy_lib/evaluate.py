"""Uniform evaluation of a strategy's order stream: PnL stats + bankroll path.

A strategy is a function make_orders(master_df) -> orders DataFrame (see backtest.Order
fields). Evaluation runs the tape backtester on train and test separately.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from strategy_lib.backtest import TapeBacktester

TEST_START = "2026-05-16"


def evaluate(family, orders, queue_mult=2.0, label="strategy", stake=5.0, bankroll0=100.0):
    """orders must include a 'date' column (window date) for splitting."""
    bt = TapeBacktester(family, queue_mult=queue_mult)
    out = {}
    for period, sub in [("train", orders[orders.date < TEST_START]),
                        ("test", orders[orders.date >= TEST_START])]:
        if not len(sub):
            out[period] = None
            continue
        res = bt.run(sub.drop(columns=["date"]))
        res = res.merge(sub[["wts", "date"]].drop_duplicates("wts"), on="wts", how="left")
        f = res[res.filled].copy()
        days = sub.date.nunique()
        stats = {
            "label": label, "period": period,
            "orders": len(res), "fills": len(f),
            "fill_rate": len(f) / max(len(res), 1),
            "ev_cents": f.pnl.mean() * 100 if len(f) else np.nan,
            "win_rate": (f.pnl > 0).mean() if len(f) else np.nan,
            "trades_per_day": len(f) / max(days, 1),
            "days": days,
        }
        if len(f):
            # bankroll path: stake $ per trade, shares = stake/entry price ~ pnl_frac = pnl/entry
            f = f.sort_values(["date", "wts"])
            entry_px = sub.set_index("wts").limit_px.to_dict()
            f["entry_px"] = f.wts.map(entry_px)
            f["ret"] = f.pnl / f.entry_px  # return on stake
            bk = bankroll0
            path = []
            for r in f.ret:
                st = min(stake, bk)  # can't stake more than bankroll
                bk += st * r
                path.append(bk)
            path = np.array(path)
            peak = np.maximum.accumulate(path)
            stats["final_bankroll"] = bk
            stats["max_dd_pct"] = ((path - peak) / peak).min() * 100
            # monthly consistency
            f["month"] = f.date.str[:7]
            monthly = f.groupby("month").pnl.agg(["mean", "sum", "count"])
            stats["monthly"] = monthly
            stats["pnl_per_share_std"] = f.pnl.std()
            stats["tstat"] = f.pnl.mean() / (f.pnl.std() / np.sqrt(len(f))) if len(f) > 1 else np.nan
        out[period] = stats
    return out


def print_eval(out):
    for period in ["train", "test"]:
        s = out.get(period)
        if s is None:
            print(f"{period}: no orders")
            continue
        print(f"[{s['label']}] {period}: fills {s['fills']}/{s['orders']} ({s['fill_rate']:.0%}), "
              f"EV {s['ev_cents']:.2f}c/share, win {s['win_rate']:.1%}, "
              f"{s['trades_per_day']:.1f} tr/day over {s['days']}d, t={s.get('tstat', float('nan')):.1f}")
        if "final_bankroll" in s:
            print(f"   bankroll $100 -> ${s['final_bankroll']:.0f}, maxDD {s['max_dd_pct']:.1f}%")
        if "monthly" in s:
            print(s["monthly"].round(4).to_string())
