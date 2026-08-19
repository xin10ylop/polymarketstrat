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

from bot.twap_verify import (COIN, DB_DIR, FAMILY, NSEC, WINDOW, load_grid,
                             nsec_at)

LEAD = int(os.environ.get("LEAD", "3"))
GATE = float(os.environ.get("GATE", "0.5"))
COVER = float(os.environ.get("COVER", "0.75"))
HOURS = float(os.environ.get("HOURS", "0"))          # 0 = the whole grid span
EXITS = [float(x) for x in os.environ.get(
    "EXITS", "0.02,0.05,0.07,0.10,0.15,0.20,0.25,0.30,0.35,0.40").split(",")]
BY = [float(x) for x in os.environ.get("BY", "5,15,30,60,300").split(",")]
# how far before the open to read the entry price off the tape. The bot takes
# the ask at T-3; the tape's nearest equivalent is what takers paid just
# before that, and the sensitivity of the result to this window is reported
# rather than assumed.
ENTRY_WIN = int(os.environ.get("ENTRY_WIN", "60"))
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


def autocorr(v):
    """Lag-1 autocorrelation. Consecutive windows are NOT independent — the
    tilt is spot minus a trailing mean, so a sustained move puts the same sign
    on several windows in a row, and bot/tilt_cluster.py measured a lag-1 of
    +0.126 on the win indicator. An interval computed as if they were
    independent is too tight."""
    n = len(v)
    if n < 4:
        return 0.0
    m = sum(v) / n
    den = sum((x - m) ** 2 for x in v)
    if den <= 0:
        return 0.0
    return sum((v[i] - m) * (v[i + 1] - m) for i in range(n - 1)) / den


def band(pnl):
    """(mean, lower, upper, n_eff) on a per-window P&L series, in cents.

    A t-style interval on the P&L itself rather than a Wilson interval on a
    win rate: the strategies being compared do not all HAVE a win rate (a
    filled limit pays a fixed amount regardless of the outcome), and putting
    every candidate on the same footing is the only way the comparison means
    anything. n_eff discounts the sample for autocorrelation.
    """
    n = len(pnl)
    if n < 4:
        return (0.0, -1e9, 1e9, 0)
    m = sum(pnl) / n
    var = sum((x - m) ** 2 for x in pnl) / (n - 1)
    r = autocorr(pnl)
    n_eff = max(4.0, n * (1 - r) / (1 + r)) if r > -0.99 else n
    # NEVER claim more precision than the raw sample. Negative autocorrelation
    # makes the formula return n_eff > n, which is real but is not something
    # to bank on from one 40-hour window; capping keeps every band at or wider
    # than the independent one.
    n_eff = min(n_eff, float(n))
    se = (var / n_eff) ** 0.5
    return (m, m - 1.96 * se, m + 1.96 * se, n_eff)


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
    """(tilt_bp, pick) from only what existed at T-LEAD, carry-forward.

    Era-aware since RULE2: a 5m window before 2026-08-14 has a 30s strike,
    one after has a 60s strike — tools sweeping the full tape span both."""
    n = nsec_at(w)
    carry, total, present = None, 0.0, 0
    for back in range(1, 121):
        if (w - n - back) in g:
            carry = g[w - n - back]
            break
    for s in range(w - n, w - LEAD):
        v = g.get(s)
        if v is not None:
            carry, present = v, present + 1
        if carry is None:
            return None
        total += carry
    if present < (n - LEAD) * COVER:
        return None
    spot = next((g[s] for s in range(w - LEAD, w - LEAD - 4, -1) if s in g),
                None)
    if spot is None:
        return None
    k = (total + LEAD * spot) / n
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
    drop = {"no pre-open prints": 0, "entry out of range": 0, "no tape": 0}
    for w in sorted(picks):
        if w not in tape:
            drop["no tape"] += 1
            continue
        winner, prints = tape[w]
        tilt, pick = picks[w]
        # our side's prints: normalised to UP, so flip for a down pick
        pr = [(t, s, p if pick == "up" else 1.0 - p, z)
              for (t, s, p, z) in prints]
        if pick == "down":
            pr = [(t, "SELL" if s == "BUY" else "BUY", p, z)
                  for (t, s, p, z) in pr]
        # ENTRY: what a taker paid on our side just before the open
        pre = [p for (t, s, p, z) in pr if -ENTRY_WIN <= t < 0 and s == "BUY"]
        near = [p for (t, s, p, z) in pr if -15 <= t < 0 and s == "BUY"]
        if not pre:
            drop["no pre-open prints"] += 1
            continue
        entry = st.median(pre)
        if not (0.02 < entry < 0.98):
            drop["entry out of range"] += 1
            continue
        # EXIT: a resting sell fills when someone BUYS our side at >= target
        buys = [(t, p, z) for (t, s, p, z) in pr if t >= 0 and s == "BUY"]
        rows.append(dict(w=w, tilt=tilt, pick=pick, entry=entry, buys=buys,
                         won=(winner == pick),
                         near=(st.median(near) if near else None)))
    if len(rows) < 20:
        raise SystemExit(f"only {len(rows)} windows joined tape+grid — "
                         "not enough to say anything")

    rows.sort(key=lambda r: r["w"])
    span_h = (rows[-1]["w"] - rows[0]["w"]) / 3600.0
    ent = st.median(r["entry"] for r in rows)
    held = sum(r["won"] for r in rows)

    # ------------------------------------------------------- 0. diagnostics
    print("WHAT GOT DROPPED (a silent drop is a selection bias)")
    for k, v in drop.items():
        if v:
            print(f"   {k:>22}: {v:>4} "
                  f"({100*v/(len(rows)+sum(drop.values())):.0f}%)")
    if not any(drop.values()):
        print("   nothing")
    print(f"   {'joined':>22}: {len(rows):>4}")

    # entry sensitivity: does reading the tape closer to the open move it?
    both = [r for r in rows if r["near"] is not None]
    if both:
        d = st.median(abs(r["near"] - r["entry"]) for r in both)
        print(f"\nENTRY PRICE SENSITIVITY (median |entry| difference when read "
              f"from the last 15s instead of {ENTRY_WIN}s): {100*d:.2f}c "
              f"on {len(both)}/{len(rows)} windows")
        if d > 0.01:
            print("   >1c — the entry estimate is NOT stable and every EV "
                  "below inherits that uncertainty")

    # -------------------------------------------------- 1. invariant checks
    # A winner settles at 1.00, so before the close it MUST trade above any
    # target below that. Therefore at the full-window horizon the unfilled
    # windows have to be ~100% losers, and fill% has to be >= win%. If either
    # fails, the tape join is broken and nothing below can be trusted.
    print("\nCONSISTENCY CHECKS (mechanical truths — a failure means a bug)")
    fill300 = sum(1 for r in rows if any(
        p >= r["entry"] + 0.15 - 1e-9 for t, p, _ in r["buys"]))
    left300 = [r for r in rows if not any(
        p >= r["entry"] + 0.15 - 1e-9 for t, p, _ in r["buys"])]
    lw = sum(r["won"] for r in left300)
    ok1 = fill300 >= held
    ok2 = (lw == 0) if left300 else True
    print(f"   fill% at +15c over the whole window >= win% : "
          f"{fill300} >= {held}  [{'PASS' if ok1 else 'FAIL'}]")
    print(f"   unfilled-at-close windows are all losers    : "
          f"{lw}/{len(left300)} won  [{'PASS' if ok2 else 'FAIL'}]")
    if not (ok1 and ok2):
        print("   ^ STOP. The tape/outcome join is inconsistent.")

    print(f"\n{len(rows)} windows with tape + grid + outcome over {span_h:.1f}h")
    print(f"median pre-open entry {ent:.4f}  (fee {100*fee(ent):.2f}c, "
          f"break-even {100*(ent+fee(ent)):.2f}%)")
    print(f"settles our way {100*held/len(rows):.1f}%\n")

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

    # ---------------------------------------------- per-window P&L, in cents
    # EVERY strategy is scored the same way: what one share made in that one
    # window, using THAT window's entry price rather than the sample median.
    def pnl_hold(r):
        return 100.0 * ((1.0 if r["won"] else 0.0) - r["entry"] - fee(r["entry"]))

    def pnl_limit(r, x, h):
        """Rest a sell at entry+x. If it fills by h, that is the trade. If it
        never fills, the position rides to settlement. h=inf is the user's
        'leave the limit resting until resolution'."""
        if touch(r, x, h) is not None:
            return 100.0 * (x - fee(r["entry"]))   # maker exit: no exit fee
        return pnl_hold(r)

    def show(label, pnl, extra=""):
        m, lo, hi, ne = band(pnl)
        # m is in CENTS per share. Dollars per day = cents/100 * clip *
        # windows-per-day. The first version omitted the /100 and printed
        # $300k/day, which is the kind of number that should stop a reader
        # cold rather than be read past.
        per_day = (m / 100.0) * 250 * len(pnl) * 24.0 / span_h if span_h else 0.0
        print(f"{label:>22} {m:>+8.2f}c [{lo:>+7.2f},{hi:>+7.2f}]c "
              f"{ne:>6.0f} {per_day:>9.0f} {extra}")
        return (m, lo, per_day)

    hold_pnl = [pnl_hold(r) for r in rows]
    print("\nEVERY STRATEGY, SAME WINDOWS, PER-WINDOW P&L PER SHARE")
    print(f"{'strategy':>22} {'mean':>9} {'95% band':>18} {'n_eff':>6} "
          f"{'$/day':>9}")
    hold = show("HOLD to settlement", hold_pnl)

    print("\n  -- rest the limit and LEAVE IT until resolution (your spec) --")
    grid = []
    for x in EXITS:
        p = [pnl_limit(r, x, 1e9) for r in rows]
        f_ = sum(1 for r in rows if touch(r, x, 1e9) is not None)
        m, lo, pd = show(f"LIMIT +{100*x:.0f}c, to close", p,
                         f"fills {100*f_/len(rows):.0f}%")
        grid.append((m, lo, x, 1e9, p))

    print("\n  -- give up at T+H and hold the rest --")
    for x in EXITS:
        for h in BY[:-1]:
            p = [pnl_limit(r, x, h) for r in rows]
            grid.append((sum(p) / len(p), band(p)[1], x, h, p))
    timed = sorted((g for g in grid if g[3] < 1e8), key=lambda z: -z[0])[:6]
    for m, lo, x, h, p in timed:
        f_ = sum(1 for r in rows if touch(r, x, h) is not None)
        show(f"LIMIT +{100*x:.0f}c by T+{h:.0f}s", p,
             f"fills {100*f_/len(rows):.0f}%")

    # ------------------------------------------- out-of-sample on the clock
    # 50 cells are searched above and the best of 50 always looks good. The
    # only honest guard is to choose on one half of the clock and score on the
    # other. Four promising cells on this project died exactly here.
    half = len(rows) // 2
    a_rows, b_rows = rows[:half], rows[half:]
    if len(a_rows) < 10 or len(b_rows) < 10:
        print("\n(too few windows to split out of sample)")
        return
    print(f"\nOUT OF SAMPLE — pick on the first {len(a_rows)} windows, "
          f"score on the last {len(b_rows)}")
    cand = []
    for x in EXITS:
        for h in list(BY) + [1e9]:
            pa = [pnl_limit(r, x, h) for r in a_rows]
            cand.append((sum(pa) / len(pa), x, h))
    cand.sort(key=lambda z: -z[0])
    print(f"{'strategy':>22} {'in-sample':>11} {'OUT':>11} {'verdict':>10}")
    ha = sum(pnl_hold(r) for r in a_rows) / len(a_rows)
    hb = sum(pnl_hold(r) for r in b_rows) / len(b_rows)
    print(f"{'HOLD to settlement':>22} {ha:>+10.2f}c {hb:>+10.2f}c "
          f"{'':>10}")
    for m, x, h in cand[:4]:
        pb = [pnl_limit(r, x, h) for r in b_rows]
        mb = sum(pb) / len(pb)
        lbl = (f"LIMIT +{100*x:.0f}c, to close" if h > 1e8
               else f"LIMIT +{100*x:.0f}c by T+{h:.0f}s")
        print(f"{lbl:>22} {m:>+10.2f}c {mb:>+10.2f}c "
              f"{('HELD' if mb > hb else 'lost to HOLD'):>10}")

    best = [(m, lo, x, h, 0) for (m, lo, x, h, _) in grid]

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
    lbl = (f"+{100*b[2]:.0f}c to close" if b[3] > 1e8
           else f"+{100*b[2]:.0f}c by T+{b[3]:.0f}s")
    print(f"\nBEST LIMIT BY LOWER BOUND: {lbl} -> {b[0]:+.2f}c/share, "
          f"bound {b[1]:+.2f}c")
    print(f"HOLD on the same windows:   {hold[0]:+.2f}c/share, "
          f"bound {hold[1]:+.2f}c")
    print("\nHOW TO READ THIS, AND WHAT IT STILL CANNOT TELL YOU.")
    print(f"  - {len(EXITS)*(len(BY)+1)} cells are searched. The best of that")
    print("    many always looks good in sample, which is why the OUT column")
    print("    above matters more than the mean column.")
    print("  - n_eff is below the row count because consecutive windows are")
    print("    correlated (lag-1 +0.126 measured separately). The bands are")
    print("    widened for it; they are NOT widened for the cell search.")
    print("  - a print at our price is not a guaranteed fill: queue position")
    print("    is unmodelled, so every LIMIT row is the OPTIMISTIC bound and")
    print("    the live bid tracker is the pessimistic one. HOLD has no such")
    print("    assumption, so the comparison is tilted IN THE LIMIT'S FAVOUR.")


if __name__ == "__main__":
    main()
