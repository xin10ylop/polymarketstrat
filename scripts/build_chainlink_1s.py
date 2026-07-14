"""Concatenate vault crypto_prices dailies into one Chainlink 1s series (Apr 2 - Jul 7 2026)."""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from strategy_lib.data import DATA


def main():
    src = os.path.join(DATA, "daily", "crypto_prices")
    days = sorted(f for f in os.listdir(src) if f.endswith(".parquet"))
    dfs = [pd.read_parquet(os.path.join(src, f)) for f in days]
    df = pd.concat(dfs, ignore_index=True).sort_values("timestamp_us")
    df["ts"] = (df.timestamp_us // 1_000_000).astype("int64")
    df = df.drop_duplicates("ts", keep="last")
    out = df[["ts", "price", "server_timestamp_us", "local_timestamp_us"]].rename(
        columns={"price": "cl_price"})
    out.to_parquet(os.path.join(DATA, "binance", "chainlink_1s.parquet"), index=False)
    print("wrote chainlink_1s.parquet", len(out),
          pd.to_datetime(out.ts.iloc[0], unit="s"), "->", pd.to_datetime(out.ts.iloc[-1], unit="s"))


if __name__ == "__main__":
    main()
