"""Concatenate daily 1s klines into one global series with rolling features.

Output data/binance/btc_1s.parquet: ts (s, int64 index), close, plus:
  r_5s/r_15s/r_30s/r_60s/r_300s/r_900s/r_3600s  log returns ending at ts
  vol_60/vol_300/vol_900  std of 1s log-returns over trailing window (per-sqrt(s) units)
  tbr_60/tbr_300          taker-buy volume ratio over trailing window
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from strategy_lib.data import DATA


def main():
    src = os.path.join(DATA, "binance", "klines_1s")
    days = sorted(f for f in os.listdir(src) if f.endswith(".parquet"))
    dfs = []
    for f in days:
        df = pd.read_parquet(os.path.join(src, f),
                             columns=["open_time", "close", "volume", "taker_buy_base"])
        dfs.append(df)
    df = pd.concat(dfs, ignore_index=True)
    del dfs
    # open_time in ms (or us for newer dumps) -> seconds
    unit = 1000 if df.open_time.iloc[0] < 10**14 else 1_000_000
    df["ts"] = (df.open_time // unit).astype("int64")
    df = df.drop(columns=["open_time"]).sort_values("ts").drop_duplicates("ts")
    # reindex to full second grid, ffill close
    full = pd.DataFrame({"ts": np.arange(df.ts.iloc[0], df.ts.iloc[-1] + 1, dtype="int64")})
    df = full.merge(df, on="ts", how="left")
    df["close"] = df.close.ffill()
    df["volume"] = df.volume.fillna(0.0)
    df["taker_buy_base"] = df.taker_buy_base.fillna(0.0)

    c = df.close.to_numpy()
    logc = np.log(c)
    out = {"ts": df.ts.to_numpy(), "close": c.astype("float32")}
    for h in [5, 15, 30, 60, 300, 900, 3600]:
        r = np.empty_like(logc)
        r[:h] = np.nan
        r[h:] = logc[h:] - logc[:-h]
        out[f"r_{h}s"] = r.astype("float32")
    r1 = np.diff(logc, prepend=np.nan)
    s = pd.Series(r1)
    for h in [60, 300, 900]:
        out[f"vol_{h}"] = s.rolling(h, min_periods=h // 2).std().to_numpy().astype("float32")
    vol = df.volume.to_numpy()
    tb = df.taker_buy_base.to_numpy()
    vs = pd.Series(vol)
    tbs = pd.Series(tb)
    for h in [60, 300]:
        tot = vs.rolling(h).sum()
        out[f"tbr_{h}"] = (tbs.rolling(h).sum() / tot.replace(0, np.nan)).to_numpy().astype("float32")
    res = pd.DataFrame(out)
    res.to_parquet(os.path.join(DATA, "binance", "btc_1s.parquet"), index=False)
    print("wrote btc_1s.parquet", len(res), "seconds",
          pd.to_datetime(res.ts.iloc[0], unit="s"), "->", pd.to_datetime(res.ts.iloc[-1], unit="s"))


if __name__ == "__main__":
    main()
