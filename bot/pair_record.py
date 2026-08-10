"""Sample Polymarket and Kalshi books at the SAME instant. Read-only.

  venv/bin/python -m bot.pair_record

THE SETUP. Kalshi's KXBTC15M and Polymarket's btc-updown-15m ask the same
question — is the 60-second average at the close at least the 60-second
average at the open — on the same :00/:15/:30/:45 clock. Kalshi settles on
CF Benchmarks BRTI, Polymarket on Chainlink. Two venues, one event, two
resolvers. That is the shape of the only mechanism that ever demonstrably
paid on this project, and the rule change did not reach it.

MEASURED ALREADY, from history, 291 post-cutover windows:
  - the two feeds AGREE on the outcome 96.9% of the time, and the 9
    disagreements split 6/3, symmetric within noise. So a paired position
    (buy UP on one venue, the opposite on the other) pays exactly 1.00 in
    97% of windows and the mismatches cancel rather than bleed.
  - Kalshi has real depth: 800-1900 contracts quoted, 100k+ contracts
    traded in a single minute.
  - profit per pair, priced off 1-minute candles: +1.54c optimistic
    (t=1.90), +0.73c once you use the worst quote inside each minute
    (t=0.55). Neither is significant, and the conservative pass throws
    away 76% of the "opportunities" as timing artifacts.

WHY A RECORDER AND NOT AN ANSWER. Polymarket publishes 1-minute price
history and Kalshi 1-minute candles. Inside one of those minutes Kalshi's
ask moved 0.52 -> 0.31. Pairing a quote from one venue with a quote from
the other up to 60 seconds away cannot tell a real cross-venue gap from
two prices sampled at different moments. That ambiguity is the entire
result. It can only be resolved by sampling both books at once, which no
public history provides and which cannot be back-filled.

So this records, and nothing else. It never trades, holds no keys, touches
no ledger, and takes the lowest CPU share on the box.
"""
import json
import os
import sqlite3
import time
import urllib.request

WINDOW = 900
OUT_DIR = os.environ.get("PAIR_DIR", "bot/data/paircal")
EVERY = float(os.environ.get("EVERY", "20"))     # seconds between samples
HDRS = {"User-Agent": "Mozilla/5.0"}
KB = "https://api.elections.kalshi.com/trade-api/v2"
PM_SLUG = os.environ.get("PM_SLUG", "btc-updown-15m-")
K_SERIES = os.environ.get("K_SERIES", "KXBTC15M")


def get(url, tries=2, timeout=12):
    for attempt in range(tries):
        try:
            return json.load(urllib.request.urlopen(
                urllib.request.Request(url, headers=HDRS), timeout=timeout))
        except Exception:  # noqa: BLE001
            if attempt == tries - 1:
                return None
            time.sleep(0.3)


def db_open():
    os.makedirs(OUT_DIR, exist_ok=True)
    db = sqlite3.connect(os.path.join(OUT_DIR, "pair_15m.db"))
    db.execute("""CREATE TABLE IF NOT EXISTS pair(
        wts INTEGER, ts REAL, left_s INTEGER,
        pm_up_ask REAL, pm_up_sz REAL, pm_dn_ask REAL, pm_dn_sz REAL,
        k_yes_ask REAL, k_yes_sz REAL, k_no_ask REAL, k_no_sz REAL,
        pm_err INTEGER, k_err INTEGER, dt REAL,
        PRIMARY KEY(wts, ts))""")
    db.commit()
    return db


def pm_tokens(wts):
    a = get(f"https://gamma-api.polymarket.com/markets?slug={PM_SLUG}{wts}")
    if not a:
        return None
    m = a[0]
    toks, outs = m["clobTokenIds"], m["outcomes"]
    if isinstance(toks, str):
        toks = json.loads(toks)
    if isinstance(outs, str):
        outs = json.loads(outs)
    by = {o.lower(): t for o, t in zip(outs, toks)}
    return by.get("up"), by.get("down")


def k_ticker(close_ts):
    """The Kalshi market closing at this instant, or None.

    No status filter. Kalshi markets sit as `initialized` and only flip to
    `open` partway into their window — filtering on status=open here meant
    the lookup returned nothing at the window boundary, and since the
    result was cached for the whole window the recorder logged one sample
    in nine hours.
    """
    r = get(f"{KB}/markets?series_ticker={K_SERIES}&limit=200")
    for m in ((r or {}).get("markets") or []):
        ct = m.get("close_time") or ""
        try:
            t = time.mktime(time.strptime(ct[:19], "%Y-%m-%dT%H:%M:%S")) \
                - time.timezone
        except Exception:  # noqa: BLE001
            continue
        if abs(t - close_ts) < 30:
            return m["ticker"]
    return None


def pm_top(token):
    """(ask, size) or None on a FETCH failure — never on an empty book."""
    b = get(f"https://clob.polymarket.com/book?token_id={token}")
    if b is None:
        return None
    asks = [(float(x["price"]), float(x["size"])) for x in (b.get("asks") or [])]
    return min(asks) if asks else (None, None)


def k_top(ticker):
    """(yes_ask, yes_sz, no_ask, no_sz) or None on a fetch failure.

    Kalshi quotes both sides as BIDS, so the ask for one side is the
    complement of the best bid on the other.
    """
    d = get(f"{KB}/markets/{ticker}/orderbook?depth=1")
    ob = (d or {}).get("orderbook_fp") or (d or {}).get("orderbook")
    if not ob:
        return None
    def best(side):
        lv = ob.get(side) or []
        if not lv:
            return (None, None)
        px, sz = max(lv, key=lambda r: float(r[0]))
        return (float(px), float(sz))
    yb, ysz = best("yes_dollars") if "yes_dollars" in ob else best("yes")
    nb, nsz = best("no_dollars") if "no_dollars" in ob else best("no")
    yes_ask = None if nb is None else round(1.0 - nb, 4)
    no_ask = None if yb is None else round(1.0 - yb, 4)
    return yes_ask, nsz, no_ask, ysz


def main():
    db = db_open()
    print(f"pair recorder: {PM_SLUG}* vs {K_SERIES}, every {EVERY:.0f}s -> "
          f"{OUT_DIR}/pair_15m.db", flush=True)
    cur, toks, tick, nxt, retry = None, None, None, 0.0, 0.0
    while True:
        now = time.time()
        wts = int(now - now % WINDOW)
        if wts != cur:
            cur, nxt, toks, tick, retry = wts, 0.0, None, None, 0.0
        # keep retrying a failed lookup INSIDE the window instead of writing
        # the window off — either venue can be briefly unresolvable at the
        # boundary, and caching that failure costs the whole window
        if (toks is None or not all(toks) or tick is None) and now >= retry:
            retry = now + 20
            if toks is None or not all(toks):
                toks = pm_tokens(wts)
            if tick is None:
                tick = k_ticker(wts + WINDOW)
            if toks and all(toks) and tick:
                n = db.execute("SELECT COUNT(*) FROM pair").fetchone()[0]
                print(f"w{wts} both venues resolved, kalshi={tick} "
                      f"(rows={n})", flush=True)
            elif now - wts > 120:
                print(f"w{wts} still unresolved: "
                      f"pm={'ok' if toks and all(toks) else 'MISSING'} "
                      f"kalshi={tick or 'MISSING'}", flush=True)
        if now >= nxt and toks and all(toks) and tick:
            nxt = now + EVERY
            t0 = time.time()
            up = pm_top(toks[0])
            dn = pm_top(toks[1])
            k = k_top(tick)
            dt = time.time() - t0            # how simultaneous the sample was
            pm_err = 1 if (up is None or dn is None) else 0
            k_err = 1 if k is None else 0
            up = up or (None, None)
            dn = dn or (None, None)
            k = k or (None, None, None, None)
            db.execute("INSERT OR REPLACE INTO pair VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (wts, t0, int(wts + WINDOW - t0), up[0], up[1], dn[0], dn[1],
                        k[0], k[1], k[2], k[3], pm_err, k_err, dt))
            db.commit()
        time.sleep(0.5)


if __name__ == "__main__":
    main()
