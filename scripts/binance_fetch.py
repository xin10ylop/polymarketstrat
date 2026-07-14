"""Fetch Binance BTCUSDT 1s klines (daily zips) from data.binance.vision into data/binance/klines_1s/."""
import io
import os
import sys
import queue
import threading
import zipfile
from datetime import date, timedelta

import pandas as pd
import requests

ROOT = os.path.join(os.path.dirname(__file__), "..")
URL = "https://data.binance.vision/data/spot/daily/klines/BTCUSDT/1s/BTCUSDT-1s-{d}.zip"
COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "n_trades", "taker_buy_base", "taker_buy_quote", "ignore"]
NUM_WORKERS = 6


def main(a, b):
    outdir = os.path.join(ROOT, "data", "binance", "klines_1s")
    os.makedirs(outdir, exist_ok=True)
    tasks = queue.Queue()
    d = date.fromisoformat(a)
    end = date.fromisoformat(b)
    n = 0
    while d <= end:
        out = os.path.join(outdir, f"{d.isoformat()}.parquet")
        if not os.path.exists(out):
            tasks.put((d.isoformat(), out))
            n += 1
        d += timedelta(days=1)
    print(f"{n} days to fetch", flush=True)
    done = [0]
    lock = threading.Lock()

    def worker():
        s = requests.Session()
        while True:
            try:
                ds, out = tasks.get_nowait()
            except queue.Empty:
                return
            for attempt in range(4):
                try:
                    r = s.get(URL.format(d=ds), timeout=180)
                    if r.status_code == 404:
                        print(f"missing {ds}", flush=True)
                        break
                    r.raise_for_status()
                    zf = zipfile.ZipFile(io.BytesIO(r.content))
                    csv = zf.open(zf.namelist()[0])
                    df = pd.read_csv(csv, header=None, names=COLS)
                    if str(df.iloc[0, 0]).startswith("open_time"):
                        df = df.iloc[1:].reset_index(drop=True)
                    df = df.astype({"open_time": "int64", "close_time": "int64"})
                    for c in ["open", "high", "low", "close", "volume", "quote_volume",
                              "taker_buy_base", "taker_buy_quote"]:
                        df[c] = df[c].astype("float64")
                    df["n_trades"] = df["n_trades"].astype("int32")
                    df = df.drop(columns=["ignore"])
                    df.to_parquet(out, index=False)
                    break
                except Exception as e:
                    if attempt == 3:
                        print(f"FAILED {ds}: {e}", flush=True)
                    else:
                        import time
                        time.sleep(2 ** attempt)
            with lock:
                done[0] += 1
                if done[0] % 25 == 0:
                    print(f"{done[0]}/{n}", flush=True)

    threads = [threading.Thread(target=worker) for _ in range(NUM_WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print("binance fetch complete", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
