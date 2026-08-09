"""Record the order book at fixed lead times before each close.

  venv/bin/python -m bot.book_record            # btc 5m, runs until stopped
  COIN=eth FAMILY=15m venv/bin/python -m bot.book_record

bot/timing_scan.py showed the SIGNAL survives far earlier than we trade: a
>2bp projected-average gap called the winner 100% at 30s out and 98.4% at
60s, against a book that is empty by T-6s. That leaves one question, and it
is the whole question — at those earlier moments, is anyone OFFERING the
winning side, and at what price?

This samples both tokens' books at each lead time via the CLOB REST
endpoint and stores best bid/ask with size. Join it later against the 1s
grid and official outcomes to price the opportunity honestly.

REST, not the websocket, on purpose: at 30-120s leads a ~0.3s fetch is
irrelevant, and polling avoids reimplementing book reconstruction in a
measurement tool. Read-only — it never trades and touches no bot state.
"""
import json
import os
import sqlite3
import time
import urllib.request

COIN = os.environ.get("COIN", "btc").lower()
FAMILY = os.environ.get("FAMILY", "5m")
WINDOW = 900 if FAMILY == "15m" else 300
SLUG = f"{COIN}-updown-{FAMILY}-"
LEADS = sorted((int(x) for x in os.environ.get(
    "LEADS", "120,90,60,45,30,20,10,6,3").split(",")), reverse=True)
OUT_DIR = os.environ.get("BOOK_DIR", "bot/data/bookcal")
HDRS = {"User-Agent": "Mozilla/5.0"}


def db_open():
    os.makedirs(OUT_DIR, exist_ok=True)
    db = sqlite3.connect(os.path.join(OUT_DIR, f"{COIN}_{FAMILY}_book.db"))
    db.execute("""CREATE TABLE IF NOT EXISTS book(
        wts INTEGER, lead INTEGER, side TEXT, ask REAL, ask_sz REAL,
        bid REAL, bid_sz REAL, ts REAL, PRIMARY KEY(wts, lead, side))""")
    db.commit()
    return db


def tokens(wts):
    """(up_token, down_token) for a window, or None before it is minted."""
    try:
        a = json.load(urllib.request.urlopen(urllib.request.Request(
            f"https://gamma-api.polymarket.com/markets?slug={SLUG}{wts}",
            headers=HDRS), timeout=20))
        if not a:
            return None
        m = a[0]
        toks = m["clobTokenIds"]
        outs = m["outcomes"]
        if isinstance(toks, str):
            toks = json.loads(toks)
        if isinstance(outs, str):
            outs = json.loads(outs)
        by = {o.lower(): t for o, t in zip(outs, toks)}
        return by.get("up"), by.get("down")
    except Exception:  # noqa: BLE001
        return None


def top(token):
    """(best_ask, ask_size, best_bid, bid_size) or None if the FETCH failed.

    None is a measurement failure, never evidence that the book was empty —
    an empty book returns a valid response with no levels, which yields
    (None, None, None, None) instead.
    """
    for attempt in (0, 1):
        try:
            b = json.load(urllib.request.urlopen(urllib.request.Request(
                f"https://clob.polymarket.com/book?token_id={token}",
                headers=HDRS), timeout=15))
            break
        except Exception:  # noqa: BLE001
            if attempt:
                return None
            time.sleep(0.4)
    asks = [(float(x["price"]), float(x["size"])) for x in (b.get("asks") or [])]
    bids = [(float(x["price"]), float(x["size"])) for x in (b.get("bids") or [])]
    a = min(asks) if asks else (None, None)
    d = max(bids) if bids else (None, None)
    return a[0], a[1], d[0], d[1]


def main():
    db = db_open()
    print(f"book recorder: {COIN} {FAMILY}, leads {LEADS}s -> "
          f"{OUT_DIR}/{COIN}_{FAMILY}_book.db", flush=True)
    cur_wts, toks, done = None, None, set()
    while True:
        now = time.time()
        wts = int(now - now % WINDOW)
        C = wts + WINDOW
        if wts != cur_wts:
            cur_wts, done = wts, set()
            toks = tokens(wts)
            if toks is None or None in toks:
                print(f"w{wts} no market", flush=True)
        if toks and None not in toks:
            left = C - now
            for L in LEADS:
                # fire once per lead, as soon as we are inside it
                if L in done or left > L or left < L - 2:
                    continue
                done.add(L)
                for side, tok in (("up", toks[0]), ("down", toks[1])):
                    t = top(tok)
                    err = 1 if t is None else 0
                    t = t or (None, None, None, None)
                    db.execute(
                        "INSERT OR REPLACE INTO book VALUES(?,?,?,?,?,?,?,?,?)",
                        (wts, L, side, t[0], t[1], t[2], t[3], time.time(), err))
                db.commit()
                if L == LEADS[0]:
                    n = db.execute("SELECT COUNT(*) FROM book").fetchone()[0]
                    print(f"w{wts} recording (rows={n})", flush=True)
        time.sleep(0.4)


if __name__ == "__main__":
    main()
