"""Incrementally consolidate completed gap days into daily files and DELETE raw per-market files.

Safe frontier rule: for a family, only consolidate window-days strictly earlier than the max
date directory present (the fetch works in date order), unless the fetch is complete
(pass --all to consolidate everything present).
"""
import os
import shutil
import sys
import glob

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA = os.path.join(ROOT, "data")


def main(consolidate_all=False, fams=("5m", "15m", "4h")):
    w = pd.read_parquet(os.path.join(DATA, "windows_full.parquet"))
    for fam in fams:
        wmap = dict(zip(w[w.family == fam].slug, w[w.family == fam].wts))
        base_q = os.path.join(DATA, "tlx", "gap", fam, "quotes")
        base_t = os.path.join(DATA, "tlx", "gap", fam, "trades")
        if not os.path.isdir(base_q):
            continue
        days = sorted(os.listdir(base_q))
        if not days:
            continue
        frontier = days[-1] if consolidate_all else days[-1]
        done_days = days if consolidate_all else [d for d in days if d < frontier]
        for kind, base in [("quotes", base_q), ("trades", base_t)]:
            for day in done_days:
                src_dir = os.path.join(base, day)
                if not os.path.isdir(src_dir):
                    continue
                out = os.path.join(DATA, "daily", fam, kind, f"{day}.parquet")
                files = glob.glob(os.path.join(src_dir, "*.parquet"))
                if not files:
                    shutil.rmtree(src_dir)
                    continue
                if not os.path.exists(out):
                    dfs = []
                    for f in files:
                        d1 = pd.read_parquet(f)
                        d1["wts"] = d1.slug.map(wmap)
                        d1 = d1.dropna(subset=["wts"])
                        if d1.empty:
                            continue
                        d1["wts"] = d1.wts.astype("int64")
                        if kind == "quotes":
                            for c in ["bid_price", "bid_size", "ask_price", "ask_size"]:
                                d1[c] = pd.to_numeric(d1[c], errors="coerce").astype("float32")
                            d1 = d1[["timestamp_us", "local_timestamp_us", "bid_price", "bid_size",
                                     "ask_price", "ask_size", "wts"]]
                        else:
                            d1["price"] = pd.to_numeric(d1.price, errors="coerce").astype("float32")
                            d1["size"] = pd.to_numeric(d1["size"], errors="coerce").astype("float32")
                            d1 = d1[["timestamp_us", "local_timestamp_us", "price", "size", "side", "wts"]]
                        dfs.append(d1)
                    if not dfs:
                        shutil.rmtree(src_dir)
                        continue
                    df = pd.concat(dfs, ignore_index=True)
                    del dfs
                    df = df.sort_values("timestamp_us", kind="stable")
                    tmp = out + ".tmp"
                    df.to_parquet(tmp, index=False)
                    os.replace(tmp, out)
                shutil.rmtree(src_dir)
            print(f"{fam}/{kind}: consolidated+cleaned {len(done_days)} days", flush=True)


if __name__ == "__main__":
    fams = tuple(a for a in sys.argv[1:] if not a.startswith("--")) or ("5m", "15m", "4h")
    main(consolidate_all="--all" in sys.argv, fams=fams)
