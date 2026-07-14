"""Combined portfolio simulation on the test period from results/trades_*.csv.

$100 starting bankroll, $5 stake per trade (never more than current bankroll),
profits reinvested. Trades ordered chronologically by fill time.
"""
import glob
import os

import numpy as np
import pandas as pd

# OOS survivors only: S1 failed walk-forward, S2 15m was a basis artifact
SURVIVORS = ["S2_snipe_5m", "S3_toll_5m", "S3_toll_15m"]
FALLBACK_COST = {"S2_snipe_5m": 0.70, "S3_toll_5m": 0.992, "S3_toll_15m": 0.992}


def main():
    frames = []
    for strat in SURVIVORS:
        f = f"results/trades_{strat}_test.csv"
        df = pd.read_csv(f)
        df["strategy"] = strat
        default_t = {"S2_snipe_5m": 295.0, "S3_toll_5m": 302.0, "S3_toll_15m": 902.0}[strat]
        if "entry_t" not in df:
            df["entry_t"] = default_t
        df["t_fill"] = df.wts + df.entry_t.fillna(default_t)
        df["cost"] = FALLBACK_COST.get(strat, 0.8)
        frames.append(df[["strategy", "t_fill", "pnl", "cost", "date"]])
    allt = pd.concat(frames).sort_values("t_fill").reset_index(drop=True)
    print(f"{len(allt)} test-period trades across {allt.strategy.nunique()} strategies, "
          f"{allt.date.nunique()} days")
    print(allt.groupby("strategy").agg(n=("pnl", "size"), ev_c=("pnl", lambda s: s.mean() * 100)).round(2))

    bk = 100.0
    path = []
    for r in allt.itertuples():
        stake = min(5.0, bk)
        shares = stake / r.cost
        bk += shares * r.pnl
        path.append(bk)
    path = np.array(path)
    peak = np.maximum.accumulate(path)
    dd = ((path - peak) / peak).min()
    days = allt.date.nunique()
    print(f"\n$100 -> ${bk:.0f} over {days} test days "
          f"({(bk/100)**(1/max(days,1))-1:.2%}/day compounded), maxDD {dd:.1%}")
    daily = allt.assign(dollar=lambda d: 5.0 / d.cost * d.pnl).groupby("date").dollar.sum()
    print(f"daily $ pnl at flat $5 stakes: mean {daily.mean():.2f}, median {daily.median():.2f}, "
          f"worst {daily.min():.2f}, best {daily.max():.2f}, positive days {(daily>0).mean():.0%}")
    # per-strategy standalone paths
    for strat, sub in allt.groupby("strategy"):
        bk2 = 100.0
        for r in sub.itertuples():
            stake = min(5.0, bk2)
            bk2 += stake / r.cost * r.pnl
        print(f"  {strat} standalone: $100 -> ${bk2:.0f} ({len(sub)} trades)")


if __name__ == "__main__":
    main()
