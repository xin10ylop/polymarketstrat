"""Fetch Telonex data for the Polymarket hourly BTC up/down family (trades + quotes, Up token).

Reads the market catalog (btc_updown_markets.parquet built from the Telonex markets dataset),
downloads per-market per-day parquet files into data/tlx/1h/{channel}/, then this can be
concatenated into daily files later.
"""
import os
import sys
import queue
import threading
from datetime import date, timedelta

import pandas as pd
import requests

SCRATCH = "/tmp/claude-0/-home-user-polymarketstrat/5805d42b-5a3a-57ae-9378-55ff20b77134/scratchpad"
ROOT = os.path.join(os.path.dirname(__file__), "..")
API = "https://api.telonex.io/v1/downloads/polymarket/{channel}/{date}"
NUM_WORKERS = 10


def load_env(path):
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v)


def daterange(a, b):
    d = date.fromisoformat(a)
    end = date.fromisoformat(b)
    while d <= end:
        yield d.isoformat()
        d += timedelta(days=1)


def main():
    load_env(os.path.join(ROOT, ".env"))
    key = os.environ["TELONEX_API_KEY"]
    mk = pd.read_parquet(os.path.join(SCRATCH, "btc_updown_markets.parquet"))
    hr = mk[(mk.fam == "1h_et") & (mk.quotes_from != "")].copy()
    print(f"{len(hr)} hourly markets with quotes", flush=True)

    tasks = queue.Queue()
    n = 0
    for _, r in hr.iterrows():
        for channel, cfrom, cto in [("trades", r.trades_from, r.trades_to),
                                    ("quotes", r.quotes_from, r.quotes_to)]:
            if not cfrom:
                continue
            for d in daterange(cfrom, cto):
                out = os.path.join(ROOT, "data", "tlx", "1h", channel, d, f"{r.slug}.parquet")
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
            url = API.format(channel=channel, date=d)
            ok = False
            for attempt in range(4):
                try:
                    resp = s.get(url, params={"slug": slug, "outcome": "Up"}, timeout=120,
                                 allow_redirects=True)
                    if resp.status_code == 200:
                        os.makedirs(os.path.dirname(out), exist_ok=True)
                        tmp = out + ".part"
                        with open(tmp, "wb") as f:
                            f.write(resp.content)
                        os.replace(tmp, out)
                        ok = True
                        break
                    elif resp.status_code == 404:
                        ok = True  # no data that day; skip silently
                        break
                    elif resp.status_code == 429:
                        import time
                        time.sleep(5 * (attempt + 1))
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
                if done[0] % 500 == 0:
                    print(f"{done[0]}/{n} done, {errs[0]} errors", flush=True)

    threads = [threading.Thread(target=worker) for _ in range(NUM_WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"complete: {done[0]} processed, {errs[0]} errors", flush=True)


if __name__ == "__main__":
    main()
