"""Consolidate per-market Telonex 1h files into vault-style daily parquets.

data/tlx/1h/{trades,quotes}/{date}/{slug}.parquet  ->  data/daily/1h/{trades,quotes}/{date}.parquet
Prices cast to float32, slugs mapped to wts, vault column layout.
"""
import os
import sys
import glob
from multiprocessing import Pool

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from strategy_lib.data import DATA

W = pd.read_parquet(os.path.join(DATA, "windows_full.parquet"))
WMAP = dict(zip(W[W.family == "1h"].slug, W[W.family == "1h"].wts))


def one(args):
    kind, day = args
    files = glob.glob(os.path.join(DATA, "tlx", "1h", kind, day, "*.parquet"))
    if not files:
        return 0
    out_path = os.path.join(DATA, "daily", "1h", kind, f"{day}.parquet")
    if os.path.exists(out_path):
        return 0
    dfs = [pd.read_parquet(f) for f in files]
    df = pd.concat(dfs, ignore_index=True)
    df["wts"] = df.slug.map(WMAP)
    if kind == "quotes":
        for c in ["bid_price", "bid_size", "ask_price", "ask_size"]:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("float32")
        df = df[["timestamp_us", "local_timestamp_us", "bid_price", "bid_size",
                 "ask_price", "ask_size", "wts", "slug"]]
    else:
        df["price"] = pd.to_numeric(df.price, errors="coerce").astype("float32")
        df["size"] = pd.to_numeric(df["size"], errors="coerce").astype("float32")
        df = df[["timestamp_us", "local_timestamp_us", "price", "size", "side", "wts", "slug"]]
    df = df.sort_values("timestamp_us")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    df.to_parquet(out_path, index=False)
    return len(df)


def main():
    tasks = []
    for kind in ["trades", "quotes"]:
        days = sorted(os.listdir(os.path.join(DATA, "tlx", "1h", kind)))
        tasks += [(kind, d) for d in days]
    print(f"{len(tasks)} day-files to consolidate")
    with Pool(3) as p:
        for i, n in enumerate(p.imap_unordered(one, tasks)):
            if (i + 1) % 50 == 0:
                print(f"{i+1}/{len(tasks)}", flush=True)
    print("done")


if __name__ == "__main__":
    main()
