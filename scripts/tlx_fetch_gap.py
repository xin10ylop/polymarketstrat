"""Fetch the vault's missing tick days (2026-05-13..2026-07-05, 2026-07-08..2026-07-13)
for the 5m/15m/4h families from Telonex, then consolidate into vault-style daily files.

Usage: python3 scripts/tlx_fetch_gap.py fetch   # download per-market files
       python3 scripts/tlx_fetch_gap.py consolidate
"""
import os
import sys
import glob
import queue
import threading
from datetime import date, timedelta

import pandas as pd
import requests

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA = os.path.join(ROOT, "data")
API = "https://api.telonex.io/v1/downloads/polymarket/{channel}/{date}"
NUM_WORKERS = 20

GAP_DAYS = ([("2026-05-13", "2026-07-05")], [("2026-07-08", "2026-07-13")])


def load_env(path):
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v)


def missing_days():
    days = []
    for a, b in GAP_DAYS[0] + GAP_DAYS[1]:
        d = date.fromisoformat(a)
        while d <= date.fromisoformat(b):
            days.append(d.isoformat())
            d += timedelta(days=1)
    return set(days)


def daterange(a, b):
    d = date.fromisoformat(a)
    end = date.fromisoformat(b)
    while d <= end:
        yield d.isoformat()
        d += timedelta(days=1)


def fetch():
    load_env(os.path.join(ROOT, ".env"))
    key = os.environ["TELONEX_API_KEY"]
    mk = pd.read_parquet(os.path.join(DATA, "tlx", "btc_updown_markets.parquet"))
    md = missing_days()
    w = pd.read_parquet(os.path.join(DATA, "windows_full.parquet"))
    wmap = dict(zip(w.slug, w.date))
    mk["wdate"] = mk.slug.map(wmap)
    sel = mk[mk.fam.isin(["5m", "15m", "4h"]) & mk.wdate.isin(md) & (mk.quotes_from != "")]
    print(f"{len(sel)} markets in gap days", flush=True)

    tasks = queue.Queue()
    n = 0
    # fetch only the window's own calendar day (validation needs +60s..settle windows,
    # all same-day); prioritize 5m, then 15m, then 4h
    sel = sel.copy()
    sel["fam_order"] = sel.fam.map({"5m": 0, "15m": 1, "4h": 2})
    sel = sel.sort_values(["fam_order", "wdate"])
    for _, r in sel.iterrows():
        d = r.wdate
        for channel, cfrom, cto in [("trades", r.trades_from, r.trades_to),
                                    ("quotes", r.quotes_from, r.quotes_to)]:
            if not cfrom or d < cfrom or d > cto:
                continue
            out = os.path.join(DATA, "tlx", "gap", r.fam, channel, d, f"{r.slug}.parquet")
            if os.path.exists(out):
                continue
            tasks.put((channel, d, r.slug, out))
            n += 1
    print(f"{n} files to fetch", flush=True)
    done = [0]
    errs = [0]
    lock = threading.Lock()

    def worker():
        s = requests.Session()
        s.headers["Authorization"] = f"Bearer {key}"
        while True:
            try:
                channel, d, slug, out = tasks.get_nowait()
            except queue.Empty:
                return
            ok = False
            for attempt in range(4):
                try:
                    resp = s.get(API.format(channel=channel, date=d),
                                 params={"slug": slug, "outcome": "Up"}, timeout=120,
                                 allow_redirects=True)
                    if resp.status_code == 200:
                        os.makedirs(os.path.dirname(out), exist_ok=True)
                        tmp = out + ".part"
                        with open(tmp, "wb") as fzz:
                            fzz.write(resp.content)
                        os.replace(tmp, out)
                        ok = True
                        break
                    elif resp.status_code == 404:
                        ok = True
                        break
                    else:
                        import time
                        time.sleep(2 ** attempt)
                except Exception:
                    import time
                    time.sleep(2 ** attempt)
            with lock:
                done[0] += 1
                if not ok:
                    errs[0] += 1
                if done[0] % 1000 == 0:
                    print(f"{done[0]}/{n} ({errs[0]} errors)", flush=True)

    threads = [threading.Thread(target=worker) for _ in range(NUM_WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"fetch complete: {done[0]}, errors {errs[0]}", flush=True)


def consolidate():
    w = pd.read_parquet(os.path.join(DATA, "windows_full.parquet"))
    for fam in ["5m", "15m", "4h"]:
        wmap = dict(zip(w[w.family == fam].slug, w[w.family == fam].wts))
        for kind in ["trades", "quotes"]:
            base = os.path.join(DATA, "tlx", "gap", fam, kind)
            if not os.path.isdir(base):
                continue
            # group files by the WINDOW's day (from slug epoch), matching vault semantics
            by_day = {}
            for f in glob.glob(os.path.join(base, "*", "*.parquet")):
                slug = os.path.basename(f)[:-8]
                wts = wmap.get(slug)
                if wts is None:
                    continue
                day = pd.Timestamp(wts, unit="s", tz="UTC").strftime("%Y-%m-%d")
                by_day.setdefault(day, []).append(f)
            for day, files in sorted(by_day.items()):
                out = os.path.join(DATA, "daily", fam, kind, f"{day}.parquet")
                if os.path.exists(out):
                    continue
                dfs = [pd.read_parquet(f) for f in files]
                df = pd.concat(dfs, ignore_index=True)
                df["wts"] = df.slug.map(wmap)
                df = df.dropna(subset=["wts"])
                df["wts"] = df.wts.astype("int64")
                if kind == "quotes":
                    for c in ["bid_price", "bid_size", "ask_price", "ask_size"]:
                        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float32")
                    df = df[["timestamp_us", "local_timestamp_us", "bid_price", "bid_size",
                             "ask_price", "ask_size", "wts"]]
                else:
                    df["price"] = pd.to_numeric(df.price, errors="coerce").astype("float32")
                    df["size"] = pd.to_numeric(df["size"], errors="coerce").astype("float32")
                    df = df[["timestamp_us", "local_timestamp_us", "price", "size", "side", "wts"]]
                df = df.sort_values("timestamp_us")
                df.to_parquet(out, index=False)
            print(f"{fam}/{kind}: {len(by_day)} days consolidated", flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "fetch":
        fetch()
    else:
        consolidate()
