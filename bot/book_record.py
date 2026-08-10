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
# Seconds BEFORE the next window opens. Stored as lead = WINDOW + n, so a 5m
# window's T-3 is lead 303 and the existing schema needs no change.
# THIS IS THE ONLY MOMENT NOTHING HAS EVER SAMPLED. The book sits flat and
# symmetric until the open (~0.50 both sides, hundreds of shares) and reprices
# to ~0.563 on the tilt side within two seconds. Every other recorder on this
# project starts at T+2, i.e. AFTER that move.
PREOPEN = sorted((int(x) for x in os.environ.get("PREOPEN", "20,10,5,3")
                  .split(",") if x.strip()), reverse=True)
OUT_DIR = os.environ.get("BOOK_DIR", "bot/data/bookcal")
SWEEP = float(os.environ.get("SWEEP", "0.05"))   # depth window above the touch
HDRS = {"User-Agent": "Mozilla/5.0"}


def db_open():
    os.makedirs(OUT_DIR, exist_ok=True)
    db = sqlite3.connect(os.path.join(OUT_DIR, f"{COIN}_{FAMILY}_book.db"))
    db.execute("""CREATE TABLE IF NOT EXISTS book(
        wts INTEGER, lead INTEGER, side TEXT, ask REAL, ask_sz REAL,
        bid REAL, bid_sz REAL, ts REAL, err INTEGER DEFAULT 0,
        ask_cum REAL,
        PRIMARY KEY(wts, lead, side))""")
    # MIGRATE. CREATE TABLE IF NOT EXISTS is a no-op on a database that
    # already exists, so adding `err` to the schema above did nothing to the
    # recorders already running — their inserts threw "8 columns but 9 values"
    # on every write and both 5m units sat dead for two hours before anyone
    # looked. Any column added here from now on needs a line below it.
    cols = {r[1] for r in db.execute("PRAGMA table_info(book)")}
    if "err" not in cols:
        db.execute("ALTER TABLE book ADD COLUMN err INTEGER DEFAULT 0")
        print("migrated: added err column to an existing book table", flush=True)
    if "ask_cum" not in cols:
        db.execute("ALTER TABLE book ADD COLUMN ask_cum REAL")
        print("migrated: added ask_cum (depth, not just the touch)", flush=True)
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
    """(ask, ask_sz, bid, bid_sz, ask_cum) or None if the FETCH failed.

    None is a measurement failure, never evidence that the book was empty —
    an empty book returns a valid response with no levels, which yields
    all-None instead.

    ask_cum is the size available at or below best_ask + SWEEP, i.e. what a
    marketable order could actually take. RECORDING ONLY THE TOUCH WAS AN
    ERROR THAT COST A CONCLUSION: one pre-open sample showed 14 shares at
    the best ask and I reported that liquidity collapses into the open. The
    full ladder at the same moment held 1,889 shares within five cents.
    The touch is not the tradeable size and never was.
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
    cum = (sum(sz for px, sz in asks if px <= a[0] + SWEEP + 1e-9)
           if asks else None)
    return a[0], a[1], d[0], d[1], cum


def main():
    db = db_open()
    print(f"book recorder: {COIN} {FAMILY}, leads {LEADS}s -> "
          f"{OUT_DIR}/{COIN}_{FAMILY}_book.db", flush=True)
    cur_wts, toks, done = None, None, set()
    nxt_toks, nxt_for = None, None
    while True:
        now = time.time()
        wts = int(now - now % WINDOW)
        C = wts + WINDOW
        if wts != cur_wts:
            cur_wts, done = wts, set()
            # reuse the tokens already fetched for the pre-open sampling
            toks = nxt_toks if nxt_for == wts else tokens(wts)
            nxt_toks, nxt_for = None, None
            if toks is None or None in toks:
                print(f"w{wts} no market", flush=True)
        # ---- pre-open: sample the NEXT window before it starts
        if PREOPEN and nxt_toks is None and C - now <= max(PREOPEN) + 20:
            nxt_toks, nxt_for = tokens(C), C
        if nxt_toks and None not in nxt_toks:
            for n_ in PREOPEN:
                key = WINDOW + n_
                if key in done:
                    continue
                if not (0 <= (C - n_) - now <= 2.0):
                    continue
                done.add(key)
                for side, tok in (("up", nxt_toks[0]), ("down", nxt_toks[1])):
                    t = top(tok)
                    err = 1 if t is None else 0
                    t = t or (None, None, None, None, None)
                    db.execute(
                        "INSERT OR REPLACE INTO book(wts,lead,side,ask,ask_sz,"
                        "bid,bid_sz,ts,err,ask_cum) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (C, key, side, t[0], t[1], t[2], t[3], time.time(),
                         err, t[4]))
                db.commit()
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
                    t = t or (None, None, None, None, None)
                    # named columns, not positional: a positional insert is
                    # what coupled this writer to the exact column count
                    db.execute(
                        "INSERT OR REPLACE INTO book(wts,lead,side,ask,ask_sz,"
                        "bid,bid_sz,ts,err,ask_cum) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (wts, L, side, t[0], t[1], t[2], t[3], time.time(),
                         err, t[4]))
                db.commit()
                if L == LEADS[0]:
                    n = db.execute("SELECT COUNT(*) FROM book").fetchone()[0]
                    print(f"w{wts} recording (rows={n})", flush=True)
        time.sleep(0.4)


if __name__ == "__main__":
    main()
