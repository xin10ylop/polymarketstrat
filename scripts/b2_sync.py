"""Sync the whole Backblaze vault into data/ (skips files already present with same size)."""
import os
import sys
import queue
import threading

from b2_list import client, load_env

NUM_WORKERS = 8


def main():
    load_env(os.path.join(os.path.dirname(__file__), "..", ".env"))
    bucket = os.environ["DS_BUCKET"]
    root = os.path.join(os.path.dirname(__file__), "..", "data")
    manifest = os.path.join(root, "_manifest.txt")
    tasks = queue.Queue()
    n_total = 0
    with open(manifest) as f:
        for line in f:
            size, key = line.rstrip("\n").split("\t", 1)
            if key.endswith("/") or "/.done_" in key:
                continue
            local = os.path.join(root, key.replace("data/processed/", "", 1))
            if os.path.exists(local) and os.path.getsize(local) == int(size):
                continue
            tasks.put((key, local))
            n_total += 1
    print(f"{n_total} files to download", flush=True)

    done = [0]
    lock = threading.Lock()

    def worker():
        s3 = client()
        while True:
            try:
                key, local = tasks.get_nowait()
            except queue.Empty:
                return
            os.makedirs(os.path.dirname(local), exist_ok=True)
            tmp = local + ".part"
            for attempt in range(4):
                try:
                    s3.download_file(bucket, key, tmp)
                    os.replace(tmp, local)
                    break
                except Exception as e:
                    if attempt == 3:
                        print(f"FAILED {key}: {e}", flush=True)
            with lock:
                done[0] += 1
                if done[0] % 100 == 0:
                    print(f"{done[0]}/{n_total} done", flush=True)

    threads = [threading.Thread(target=worker) for _ in range(NUM_WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"sync complete: {done[0]} files", flush=True)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(__file__))
    main()
