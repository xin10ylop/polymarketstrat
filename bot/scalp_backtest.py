"""Backtest the scalp on the real tape: entry at T-3, resting sell at +Xc.

  venv/bin/python -m bot.scalp_backtest              # btc, whole grid span
  COIN=eth GATE=1.0 venv/bin/python -m bot.scalp_backtest
  HOURS=12 venv/bin/python -m bot.scalp_backtest     # quick pass

WHY THIS EXISTS. The live touch tracker is honest but slow — it produces one
row per position taken, so a day of running buys perhaps seventy rows. The
question it answers (does a resting sell at +Xc fill, and when) can be asked
of history instead, because Polymarket's data-api serves the full trade tape
per market for at least thirty days, dense enough to matter: 1,006 prints on
one 5-minute window, 209 of them inside the first thirty seconds.

WHAT BOUNDS IT, and it is not the tape. The tilt needs Chainlink's 1s grid,
which only exists as far back as bot.twap_record has been running (~50h), and
NOTHING before 2026-08-07 is usable at any price: until that date the strike
was spot at the open, so the window opened exactly at-the-money and there was
no opening tilt to detect. The strategy is younger than the rule change.

TRADES, NOT THE BOOK — and the difference cuts both ways. A resting sell fills
when someone BUYS our side at or above our price, and a print at that price is
direct evidence somebody did. That is stronger than the bid-snapshot test
bot/exit_curve.py used. It is also optimistic in one specific way: it ignores
queue position, so a thin print at our level might have filled the resting
orders ahead of us and not ours. The volume column is reported so that
assumption can be judged rather than hidden.

COMPLEMENTARY ORDERS ARE THE SAME ORDER. Polymarket's CLOB merges the two
sides — buying Up at p and selling Down at 1-p match against each other, and
ask(up) == 1 - bid(down) to the tick on a live book. So every print is
normalised onto our side before anything is measured; reading only the rows
whose `outcome` matches would miss roughly half the tape.

Caches the tape in sqlite, so the first run is slow and every rerun is free.

Read-only with respect to the bots. Never trades.
"""
import json
import os
import sqlite3
import statistics as st
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from bot.twap_verify import COIN, DB_DIR, FAMILY, NSEC, WINDOW, load_grid

LEAD = int(os.environ.get("LEAD", "3"))
GATE = float(os.environ.get("GATE", "0.5"))
COVER = float(os.environ.get("COVER", "0.75"))
HOURS = float(os.environ.get("HOURS", "0"))          # 0 = the whole grid span
EXITS = [float(x) for x in os.environ.get(
    "EXITS", "0.01,0.02,0.03,0.04,0.05,0.07,0.10,0.15").split(",")]
BY = [float(x) for x in os.environ.get("BY", "5,15,30,60,300").split(",")]
CACHE = os.environ.get("TAPE_DIR", "bot/data/tape")
WORKERS = int(os.environ.get("WORKERS", "6"))
HDRS = {"User-Agent": "Mozilla/5.0"}
GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"


def fee(p):
    return 0.07 * p * (1 - p)


def wilson(k, n):
    if not n:
        return (0.0, 1.0)
    z, p = 1.96, k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, c - h), min(1.0, c + h))


def _get(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=HDRS), timeout=25) as r:
                return json.load(r)
        except Exception:  # noqa: BLE001
            if i == tries - 1:
                return None
            time.sleep(1.0 * (2 ** i))
    return None


def cache_db():
    os.makedirs(CACHE, exist_ok=True)
    db = sqlite3.connect(os.path.join(CACHE, f"{COIN}_{FAMILY}_tape.db"),
                         timeout=30)
    db.execute("""CREATE TABLE IF NOT EXISTS tape(
        wts INTEGER PRIMARY KEY, winner TEXT, prints TEXT, fetched REAL)""")
    db.commit()
    return db


def fetch_window(wts):
    """(wts, winner, [(t_rel, side_on_up, price_on_up, size)]) or None.

    Every print is normalised onto the UP side: buying Down at p is selling Up
    at 1-p, and the CLOB matches those against each other, so a tape read only
    on matching `outcome` rows sees about half of what traded.
    """
    slug = f"{COIN}-updown-{FAMILY}-{wts}"
    m = _get(f"{GAMMA}/markets?slug={slug}&closed=true")
    if not m:
        return None
    m = m[0]
    try:
        outs = m["outcomes"]
        pr = m["outcomePrices"]
        outs = json.loads(outs) if isinstance(outs, str) else outs
        pr = json.loads(pr) if isinstance(pr, str) else pr
    except Exception:  # noqa: BLE001
        return None
    hit = [o.lower() for o, p in zip(outs, pr) if float(p) == 1.0]
    if len(hit) != 1:
        return None
    winner, cond = hit[0], m.get("conditionId")
    if not cond:
        return None
    prints, off = [], 0
    while off <= 4000:
        tr = _get(f"{DATA}/trades?market={cond}&limit=500&offset={off}")
        if not isinstance(tr, list) or not tr:
            break
        for x in tr:
            try:
                o = str(x["outcome"]).lower()
                s = str(x["side"]).upper()
                p = float(x["price"])
                sz = float(x.get("size") or 0)
                t = int(x["timestamp"]) - wts
            except Exception:  # noqa: BLE001
                continue
            if o == "up":
                prints.append((t, s, p, sz))
            elif o == "down":
                prints.append((t, "SELL" if s == "BUY" else "BUY", 1.0 - p, sz))
        if len(tr) < 500:
            break
        off += 500
    prints.sort()
    return wts, winner, prints


def load_tape(wtss):
    db = cache_db()
    have = {w for (w,) in db.execute("SELECT wts FROM tape")}
    todo = [w for w in wtss if w not in have]
    if todo:
        print(f"fetching {len(todo)} windows from the tape "
              f"({len(have)} already cached) — first run only", flush=True)
        done = 0
        with ThreadPoolExecutor(WORKERS) as ex:
            for r in ex.map(fetch_window, todo):
                done += 1
                if r:
                    db.execute("INSERT OR REPLACE INTO tape VALUES(?,?,?,?)",
                               (r[0], r[1], json.dumps(r[2],
                                                       separators=(",", ":")),
                                time.time()))
                if done % 50 == 0:
                    db.commit()
                    print(f"  {done}/{len(todo)}", flush=True)
        db.commit()
    out = {}
    for w, win, pr, _ in db.execute("SELECT * FROM tape"):
        try:
            out[w] = (win, json.loads(pr))
        except ValueError:
            continue
    return out


def tilt_at(g, w):
    """(tilt_bp, pick) from only what existed at T-LEAD, carry-forward."""
    carry, total, present = None, 0.0, 0
    for back in range(1, 121):
        if (w - NSEC - back) in g:
            carry = g[w - NSEC - back]
            break
    for s in range(w - NSEC, w - LEAD):
        v = g.get(s)
        if v is not None:
            carry, present = v, present + 1
        if carry is None:
            return None
        total += carry
    if present < (NSEC - LEAD) * COVER:
        return None
    spot = next((g[s] for s in range(w - LEAD, w - LEAD - 4, -1) if s in g),
                None)
    if spot is None:
        return None
    k = (total + LEAD * spot) / NSEC
    if k <= 0:
        return None
    t = (spot - k) / k * 1e4
    return (t, "up" if t >= 0 else "down")


def main():
    g = load_grid()
    lo, hi = min(g), max(g)
    if HOURS:
        lo = max(lo, hi - int(HOURS * 3600))
    # nothing before the rule change can be tested: the strike WAS spot at the
    # open, so the window opened at-the-money and there was no tilt at all
    RULE = 1786060800          # 2026-08-07 00:00 UTC
    lo = max(lo, RULE)
    first = ((int(lo) + NSEC) // WINDOW + 1) * WINDOW
    wtss = [w for w in range(first, int(hi) - WINDOW, WINDOW)
            if w - NSEC >= lo]
    picks = {}
    for w in wtss:
        t = tilt_at(g, w)
        if t and abs(t[0]) >= GATE:
            picks[w] = t
    if not picks:
        raise SystemExit("no windows pass the tilt gate in the grid span")
    print(f"{COIN} {FAMILY}: {len(picks)} windows pass |tilt| >= {GATE}bp "
          f"out of {len(wtss)} in the grid span "
          f"({(max(picks)-min(picks))/3600:.1f}h, post-rule-change only)\n")

    tape = load_tape(sorted(picks))
    rows = []
    for w, (winner, prints) in tape.items():
        if w not in picks:
            continue
        tilt, pick = picks[w]
        # our side's prints: normalised to UP, so flip for a down pick
        pr = [(t, s, p if pick == "up" else 1.0 - p, z)
              for (t, s, p, z) in prints]
        if pick == "down":
            pr = [(t, "SELL" if s == "BUY" else "BUY", p, z)
                  for (t, s, p, z) in pr]
        # ENTRY: what a taker paid on our side just before the open
        pre = [p for (t, s, p, z) in pr if -60 <= t < 0 and s == "BUY"]
        if not pre:
            continue
        entry = st.median(pre)
        if not (0.02 < entry < 0.98):
            continue
        # EXIT: a resting sell fills when someone BUYS our side at >= target
        buys = [(t, p, z) for (t, s, p, z) in pr if t >= 0 and s == "BUY"]
        rows.append(dict(w=w, tilt=tilt, pick=pick, entry=entry, buys=buys,
                         won=(winner == pick)))
    if len(rows) < 20:
        raise SystemExit(f"only {len(rows)} windows joined tape+grid — "
                         "not enough to say anything")

    span_h = (max(r["w"] for r in rows) - min(r["w"] for r in rows)) / 3600.0
    ent = st.median(r["entry"] for r in rows)
    held = sum(r["won"] for r in rows)
    print(f"{len(rows)} windows with tape + grid + outcome over {span_h:.1f}h")
    print(f"median pre-open entry {ent:.4f}  (fee {100*fee(ent):.2f}c)")
    print(f"settles our way {100*held/len(rows):.1f}%  -> HOLD is "
          f"{100*(held/len(rows) - ent - fee(ent)):+.2f}c/share\n")

    # EVERY NUMBER BELOW IS BOUND TO A HORIZON, and the first version of this
    # tool was not. It collected prints from t>=0 with no upper bound, so its
    # "peak" and "ever touched" columns spanned the whole five minutes — which
    # measures the contract CONVERGING TO SETTLEMENT, not the opening jump. A
    # binary that resolves up drifts to 0.98 by the close, so the median peak
    # read +45c above a 0.51 entry and the blend was a hold with a take-profit
    # wearing the scalp's name.
    def peak_by(r, h):
        return max((p for t, p, _ in r["buys"] if t <= h), default=None)

    def touch(r, x, h):
        return next((t for t, p, _ in r["buys"]
                     if t <= h and p >= r["entry"] + x - 1e-9), None)

    print("HOW OFTEN THE JUMP GOES THE WRONG WAY, by horizon")
    print("  (our side never printed above what we paid within...)")
    for h in BY:
        bad = sum(1 for r in rows
                  if (peak_by(r, h) or 0) <= r["entry"] + 1e-9)
        print(f"    T+{h:>4.0f}s : {bad:>4}/{len(rows)} "
              f"({100*bad/len(rows):>5.1f}%)"
              + ("   <- the scalp's real failure rate" if h == 15 else ""))

    print("\nTHE SCALP: rest at +Xc, give up at T+H and hold to settlement")
    print(f"{'exit':>5} {'by':>6} {'fill%':>7} {'med s':>7} {'left win%':>10} "
          f"{'blend':>9} {'lo':>8} {'$/day':>9}")
    best = []
    for x in EXITS:
        for h in BY:
            hits = [touch(r, x, h) for r in rows]
            fs = {i for i, t in enumerate(hits) if t is not None}
            left = [i for i in range(len(rows)) if i not in fs]
            lw = sum(rows[i]["won"] for i in left)
            lev = ((lw / len(left)) - ent - fee(ent)) if left else 0.0
            # a filled window pays x minus the ENTRY fee only: the resting
            # sell is a maker order and maker fees are zero
            blend = (len(fs) * (x - fee(ent)) + len(left) * lev) / len(rows)
            flo, _ = wilson(len(fs), len(rows))
            llo, _ = wilson(lw, len(left)) if left else (0.0, 1.0)
            blo = flo * (x - fee(ent)) + (1 - flo) * min(
                (llo - ent - fee(ent)) if left else 0.0, lev)
            per_day = blend * 250 * len(rows) * 24.0 / span_h if span_h else 0.0
            got = [t for t in hits if t is not None]
            print(f"{100*x:>4.0f}c {f'T+{h:.0f}':>6} "
                  f"{100*len(fs)/len(rows):>6.0f}% "
                  f"{(st.median(got) if got else float('nan')):>7.1f} "
                  f"{(100*lw/len(left) if left else float('nan')):>9.0f}% "
                  f"{100*blend:>+8.2f}c {100*blo:>+7.2f}c {per_day:>9.0f}")
            best.append((blend, blo, x, h, len(fs)))

    print("\nDOES THE JUMP SCALE WITH THE TILT?  (peak within T+15s)")
    print(f"{'|tilt| bp':>12} {'n':>5} {'med peak':>10} {'med 5c s':>10} "
          f"{'wrong way':>10} {'settles':>9}")
    for a, b in [(GATE, 1), (1, 2), (2, 4), (4, 8), (8, 1e9)]:
        if b <= a:
            continue
        sel = [r for r in rows if a <= abs(r["tilt"]) < b]
        if len(sel) < 5:
            continue
        pk = [100 * (peak_by(r, 15) - r["entry"]) for r in sel
              if peak_by(r, 15) is not None]
        t5 = [t for r in sel for t in [touch(r, 0.05, 15)] if t is not None]
        wrong = sum(1 for r in sel if (peak_by(r, 15) or 0) <= r["entry"] + 1e-9)
        lbl = f"{a:.1f}-{b:.0f}" if b < 1e9 else f"{a:.0f}+"
        print(f"{lbl:>12} {len(sel):>5} "
              f"{(st.median(pk) if pk else float('nan')):>+9.2f}c "
              f"{(st.median(t5) if t5 else float('nan')):>10.1f} "
              f"{100*wrong/len(sel):>9.0f}% "
              f"{100*sum(r['won'] for r in sel)/len(sel):>8.0f}%")

    best.sort(key=lambda z: -z[1])          # rank by the LOWER bound
    b = best[0]
    print(f"\nBEST BY LOWER BOUND: +{100*b[2]:.0f}c by T+{b[3]:.0f}s -> "
          f"{100*b[0]:+.2f}c/share, bound {100*b[1]:+.2f}c, "
          f"filling {b[4]}/{len(rows)}")
    print(f"HOLD TO SETTLEMENT on the same windows: "
          f"{100*(held/len(rows) - ent - fee(ent)):+.2f}c/share")
    print("\nRANKED BY THE LOWER BOUND, NOT THE BLEND, because this prints")
    print(f"{len(EXITS)*len(BY)} cells and the best of {len(EXITS)*len(BY)} "
          "always looks good. A cell whose bound is")
    print("negative has not been shown to be anything.")
    print("A print at our price is not a guaranteed fill — queue position is")
    print("not modelled, so this is the OPTIMISTIC half of the execution")
    print("question and the live bid tracker is the pessimistic half.")


if __name__ == "__main__":
    main()
