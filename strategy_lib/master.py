"""Master per-window dataset and model-value grid for a family."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from strategy_lib.data import DATA
from strategy_lib.model import fair_value


def build_master(family, refresh=False):
    cache = os.path.join(DATA, "features", f"{family}_master.parquet")
    if os.path.exists(cache) and not refresh:
        return pd.read_parquet(cache)
    w = pd.read_parquet(os.path.join(DATA, "windows_full.parquet"))
    w = w[w.family == family].copy().sort_values("wts")
    snap = pd.read_parquet(os.path.join(DATA, "features", f"{family}_snap.parquet"))
    tr = pd.read_parquet(os.path.join(DATA, "features", f"{family}_trades.parquet"))
    m = w.merge(snap, on="wts", how="inner").merge(tr, on="wts", how="left")
    bpath = os.path.join(DATA, "features", f"{family}_book.parquet")
    if os.path.exists(bpath):
        m = m.merge(pd.read_parquet(bpath), on="wts", how="left")

    b = pd.read_parquet(os.path.join(DATA, "binance", "btc_1s.parquet"))
    b = b.rename(columns={"close": "S_open"})
    m = m.merge(b, left_on="wts", right_on="ts", how="left").drop(columns=["ts"])

    # price to beat: chainlink open where known, else Binance at open
    m["K"] = m.open_chainlink.fillna(m.S_open)
    m["has_cl"] = m.open_chainlink.notna()

    # prev-window features (same family, consecutive windows only)
    m = m.sort_values("wts").reset_index(drop=True)
    dur = m.duration.iloc[0]
    prev = m[["wts", "result"]].copy()
    prev["wts"] = prev.wts + dur
    prev = prev.rename(columns={"result": "prev_result"})
    m = m.merge(prev, on="wts", how="left")
    # streak: number of consecutive identical previous results
    res = m.result.to_numpy()
    wts = m.wts.to_numpy()
    # signed streak of consecutive identical outcomes ending at window i-1
    streak_prev = np.full(len(m), np.nan)
    run = 0
    for i in range(1, len(m)):
        if wts[i] - wts[i - 1] == dur:
            if i >= 2 and wts[i - 1] - wts[i - 2] == dur and res[i - 1] == res[i - 2]:
                run += 1
            else:
                run = 1
            streak_prev[i] = run * (1 if res[i - 1] == 0 else -1)  # signed: + = Up streak
        else:
            run = 0
    m["prev_streak"] = streak_prev  # +k: k consecutive Ups just before this window

    hod = pd.to_datetime(m.wts, unit="s", utc=True)
    m["hour_utc"] = hod.dt.hour.astype("int8")
    m["dow"] = hod.dt.dayofweek.astype("int8")
    m.to_parquet(cache, index=False)
    return m


def build_grid_model(family, refresh=False):
    """Grid points enriched with S_t, sigma, tau, fair value, result."""
    cache = os.path.join(DATA, "features", f"{family}_grid_model.parquet")
    if os.path.exists(cache) and not refresh:
        return pd.read_parquet(cache)
    g = pd.read_parquet(os.path.join(DATA, "features", f"{family}_grid.parquet"))
    w = pd.read_parquet(os.path.join(DATA, "windows_full.parquet"))
    w = w[w.family == family][["wts", "result", "duration", "open_chainlink", "date"]]
    g = g.merge(w, on="wts", how="inner")
    g["ts_s"] = (g.ts_us // 1_000_000).astype("int64")
    b = pd.read_parquet(os.path.join(DATA, "binance", "btc_1s.parquet"),
                        columns=["ts", "close", "vol_300", "vol_900"])
    g = g.merge(b, left_on="ts_s", right_on="ts", how="left").drop(columns=["ts"])
    bo = pd.read_parquet(os.path.join(DATA, "binance", "btc_1s.parquet"), columns=["ts", "close"])
    bo = bo.rename(columns={"close": "S_open", "ts": "wts"})
    g = g.merge(bo, on="wts", how="left")
    g["K"] = g.open_chainlink.fillna(g.S_open)
    g["tau"] = g.duration - g.off
    live = g.tau > 0
    g["fv"] = np.nan
    g.loc[live, "fv"] = fair_value(g.loc[live, "close"], g.loc[live, "K"],
                                   g.loc[live, "vol_300"], g.loc[live, "tau"])
    g["mid"] = (g.bid_price + g.ask_price) / 2
    g.to_parquet(cache, index=False)
    return g
