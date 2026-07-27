"""Fetch the newest 5m up/down days for the edge-freshness audit.

    python3 scripts/freshness_fetch.py 2026-07-14 2026-07-25 [coin]

1. Gamma sweep: enumerate <coin>-updown-5m-<wts> slugs for the date range, pull
   conditionId/outcomePrices -> mini windows table with official results.
2. Telonex downloads: quotes+trades per resolved market (404 = not archived yet).
Writes: data/fresh/windows_fresh[_<coin>].parquet, data/fresh/raw[_<coin>]/...
(btc keeps the original un-suffixed paths).
"""
import json
import os
import queue
import sys
import threading
import urllib.request
from datetime import date, timedelta

import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(DATA, "fresh")
COIN = (sys.argv[3] if len(sys.argv) > 3 else "btc").lower()
SUF = "" if COIN == "btc" else f"_{COIN}"
GAMMA = "https://gamma-api.polymarket.com/markets?slug={slug}&closed=true"  # closed=true: gamma hides old markets from plain slug queries
TLX = "https://api.telonex.io/v1/downloads/polymarket/{channel}/{d}"


def load_env():
    with open(os.path.join(ROOT, ".env")) as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k, v)


def gamma_sweep(d0, d1):
    days = []
    d = date.fromisoformat(d0)
    while d <= date.fromisoformat(d1):
        days.append(d.isoformat())
        d += timedelta(days=1)
    rows, lock, tasks = [], threading.Lock(), queue.Queue()
    for day in days:
        base = int(pd.Timestamp(day, tz="UTC").timestamp())
        for k in range(288):
            tasks.put(base + k * 300)

    def worker():
        while True:
            try:
                wts = tasks.get_nowait()
            except queue.Empty:
                return
            slug = f"{COIN}-updown-5m-{wts}"
            try:
                req = urllib.request.Request(GAMMA.format(slug=slug),
                                             headers={"User-Agent": "fresh/1.0"})
                arr = json.load(urllib.request.urlopen(req, timeout=15))
                if not arr:
                    continue
                m = arr[0]
                op = m.get("outcomePrices")
                prices = json.loads(op) if isinstance(op, str) else op
                if not prices:
                    continue
                p0 = float(prices[0])
                if p0 not in (0.0, 1.0):
                    continue                       # unresolved
                outcomes = m.get("outcomes")
                outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
                up_idx = outcomes.index("Up")
                result = 0 if float(prices[up_idx]) == 1.0 else 1
                with lock:
                    rows.append(dict(slug=slug, wts=wts, family="5m", duration=300,
                                     result=result,
                                     date=pd.Timestamp(wts, unit="s", tz="UTC").strftime("%Y-%m-%d")))
            except Exception:  # noqa: BLE001 - skip transient failures
                pass

    ths = [threading.Thread(target=worker) for _ in range(16)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    if not rows:
        print(f"gamma sweep: 0 resolved windows for {COIN} {d0}..{d1}", flush=True)
        return pd.DataFrame()
    df = pd.DataFrame(rows).sort_values("wts")
    os.makedirs(OUT, exist_ok=True)
    df.to_parquet(os.path.join(OUT, f"windows_fresh{SUF}.parquet"), index=False)
    print(f"gamma sweep: {len(df)} resolved windows {d0}..{d1}", flush=True)
    return df


def tlx_fetch(df):
    load_env()
    key = os.environ["TELONEX_API_KEY"]
    tasks, n = queue.Queue(), 0
    channels = tuple(os.environ.get("FRESH_CHANNELS", "quotes,trades").split(","))
    for r in df.itertuples():
        for channel in channels:
            out = os.path.join(OUT, f"raw{SUF}", channel, r.date, f"{r.slug}.parquet")
            if not os.path.exists(out):
                tasks.put((channel, r.date, r.slug, out))
                n += 1
    print(f"telonex: {n} files to try", flush=True)
    done, miss = [0], [0]
    lock = threading.Lock()

    def worker():
        import requests
        s = requests.Session()
        s.headers["Authorization"] = f"Bearer {key}"
        while True:
            try:
                channel, d, slug, out = tasks.get_nowait()
            except queue.Empty:
                return
            try:
                resp = s.get(TLX.format(channel=channel, d=d),
                             params={"slug": slug, "outcome": "Up"}, timeout=90,
                             allow_redirects=True)
                if resp.status_code == 200:
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    tmp = out + ".part"
                    with open(tmp, "wb") as f:
                        f.write(resp.content)
                    os.replace(tmp, out)
                else:
                    with lock:
                        miss[0] += 1
            except Exception:  # noqa: BLE001
                with lock:
                    miss[0] += 1
            with lock:
                done[0] += 1
                if done[0] % 1000 == 0:
                    print(f"{done[0]}/{n} ({miss[0]} missing)", flush=True)

    ths = [threading.Thread(target=worker) for _ in range(20)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    print(f"telonex done: {done[0]} tried, {miss[0]} missing/404", flush=True)


if __name__ == "__main__":
    d0, d1 = sys.argv[1], sys.argv[2]
    df = gamma_sweep(d0, d1)
    if len(df):
        tlx_fetch(df)
