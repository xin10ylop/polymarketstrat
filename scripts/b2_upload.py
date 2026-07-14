"""Upload session-gathered datasets to the Backblaze vault under data/processed/.

Uploads (skipping keys that already exist with same size):
  daily/1h/{trades,quotes}/*.parquet   (consolidated hourly family)
  binance/klines_1s/*.parquet, binance/btc_1s.parquet, binance/chainlink_1s.parquet
  tlx/btc_updown_markets.parquet
  windows_full.parquet
  features/*.parquet
"""
import os
import sys
import queue
import threading

sys.path.insert(0, os.path.dirname(__file__))
from b2_list import client, load_env

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA = os.path.join(ROOT, "data")
NUM_WORKERS = 8


def gather():
    rels = []
    for sub in ["daily/1h/trades", "daily/1h/quotes", "binance/klines_1s", "features"]:
        d = os.path.join(DATA, sub)
        if os.path.isdir(d):
            rels += [os.path.join(sub, f) for f in sorted(os.listdir(d)) if f.endswith(".parquet")]
    for f in ["binance/btc_1s.parquet", "binance/chainlink_1s.parquet",
              "tlx/btc_updown_markets.parquet", "windows_full.parquet"]:
        if os.path.exists(os.path.join(DATA, f)):
            rels.append(f)
    return rels


def main():
    load_env(os.path.join(ROOT, ".env"))
    bucket = os.environ["DS_BUCKET"]
    s3 = client()
    existing = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="data/processed/"):
        for o in page.get("Contents", []):
            existing[o["Key"]] = o["Size"]
    rels = gather()
    tasks = queue.Queue()
    n = 0
    for rel in rels:
        local = os.path.join(DATA, rel)
        key = f"data/processed/{rel}"
        if existing.get(key) == os.path.getsize(local):
            continue
        tasks.put((local, key))
        n += 1
    print(f"{n} files to upload ({sum(os.path.getsize(os.path.join(DATA, r)) for r in rels)/1e9:.2f} GB total set)", flush=True)

    done = [0]
    lock = threading.Lock()

    def worker():
        s3w = client()
        while True:
            try:
                local, key = tasks.get_nowait()
            except queue.Empty:
                return
            for attempt in range(4):
                try:
                    s3w.upload_file(local, bucket, key)
                    break
                except Exception as e:
                    if attempt == 3:
                        print(f"FAILED {key}: {e}", flush=True)
            with lock:
                done[0] += 1
                if done[0] % 50 == 0:
                    print(f"{done[0]}/{n}", flush=True)

    threads = [threading.Thread(target=worker) for _ in range(NUM_WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"upload complete: {done[0]}", flush=True)


if __name__ == "__main__":
    main()
