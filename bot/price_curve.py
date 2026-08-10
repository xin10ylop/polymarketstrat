"""Where is the money, by PRICE? The decomposition that found the old edge.

  venv/bin/python -m bot.price_curve
  COIN=eth FAMILY=15m venv/bin/python -m bot.price_curve

WHY THIS EXISTS. The loss audit's single most important number was not about
accuracy at all, it was about price. Splitting the 616 pre-change fills by
what we paid:

    px < 0.50   +$1,155   (46% of profit)
    0.50-0.80   +  $856   (34%)
    px >= 0.95  -$61.50 on 15,814 shares, z = -0.06   (nothing, ever)

Every post-change study aimed at px >= 0.95 — the one region that never paid.
This aims at the whole curve.

TWO QUESTIONS, ONE TABLE.

1. Per lead: how determined is the outcome (our projected-TWAP call's hit
   rate on EVERY window), versus how confident the book is (what it charges
   for that same side)? The gap between those two columns is the edge, and
   it needs no model to read.

2. Per price bucket: when the book WAS cheap on the side we picked, did that
   side win? Cheap-and-right is the only shape that ever made money here.

Both selections — lead and ask — are observable before the outcome, so the
hit rate inside a bucket is an honest estimate of that bucket's accuracy.
That is the difference between this and the two results I had to retract,
which priced a filtered population with an unfiltered win rate.

Read-only. Never trades, touches no bot state.
"""
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

from bot.twap_verify import COIN, FAMILY, NSEC, WINDOW, load_grid, official

# 298/285/270 are T+2/T+15/T+30 on a 5m window — the OPEN. They were added
# to the recorders on 08-09 and the default here never included them, so the
# first run after that change silently reported on the old leads only.
LEADS = [int(x) for x in os.environ.get(
    "LEADS", "298,285,270,240,180,120,90,60,45,30,20,10,6,3").split(",")]
GAP_BPS = float(os.environ.get("GAP_BPS", "0"))     # 0 = the whole population
CLIP = float(os.environ.get("CLIP", "250"))
BOOK_DIR = os.environ.get("BOOK_DIR", "bot/data/bookcal")
MIN_COVER = 0.9
BUCKETS = [(0.0, 0.30), (0.30, 0.50), (0.50, 0.70), (0.70, 0.85),
           (0.85, 0.95), (0.95, 0.99), (0.99, 1.01)]


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


def load_book():
    path = os.path.join(BOOK_DIR, f"{COIN}_{FAMILY}_book.db")
    if not os.path.exists(path):
        raise SystemExit(f"no book recording at {path} — run bot.book_record")
    db = sqlite3.connect(path)
    has_err = "err" in {r[1] for r in db.execute("PRAGMA table_info(book)")}
    q = ("SELECT wts, lead, side, ask, ask_sz, err FROM book" if has_err
         else "SELECT wts, lead, side, ask, ask_sz, 0 FROM book")
    return {(w, L, s): (a, z, e) for w, L, s, a, z, e in db.execute(q)}


def main():
    g = load_grid()
    book = load_book()
    lo, hi = min(g), max(g)
    bw = sorted({w for (w, _, _) in book})
    wtss = [w for w in bw if w - NSEC >= lo and w + WINDOW <= hi
            and w % WINDOW == 0]
    with ThreadPoolExecutor(16) as ex:
        won = {w: r for w, r in ex.map(official, wtss) if r}
    if not won:
        print("nothing joinable yet — book, grid and settlement must overlap")
        return
    span_h = (max(won) - min(won) + WINDOW) / 3600.0
    print(f"{COIN} {FAMILY}: {len(won)} windows with grid + book + outcome, "
          f"{span_h:.1f}h span"
          + (f", signal gate |gap| > {GAP_BPS}bp" if GAP_BPS else
             ", whole population (no signal gate)") + "\n")

    def mean(a, b):
        v = [g[s] for s in range(a, b) if s in g]
        return (sum(v) / len(v), len(v) / (b - a)) if v else (None, 0.0)

    def spot(t):
        for kk in (t, t - 1, t - 2):
            if kk in g:
                return g[kk]
        return None

    # ------------------------------------------------ per-lead: truth vs book
    rows_by_lead = {}
    print("HOW DETERMINED vs HOW PRICED")
    print(f"{'lead':>5} {'u/n':>6} {'wins':>6} {'sig hit%':>9} {'95% CI':>13} "
          f"{'quoted':>7} {'avg ask':>8} {'need%':>7} {'EV c/sh':>8}")
    for L in LEADS:
        rows = []
        for w, winner in sorted(won.items()):
            C = w + WINDOW
            k, ck = mean(w - NSEC, w)
            if k is None or ck < MIN_COVER:
                continue
            el = max(0, min(NSEC, NSEC - L))
            if el:
                km, cov = mean(C - NSEC, C - NSEC + el)
                if km is None or cov < MIN_COVER:
                    continue
                known = km * el
            else:
                known = 0.0
            s = spot(C - L)
            if s is None:
                continue
            est = (known + (NSEC - el) * s) / NSEC
            gap = (est - k) / k * 1e4
            if GAP_BPS and abs(gap) < GAP_BPS:
                continue
            pick = "up" if gap >= 0 else "down"
            ask, sz, err = book.get((w, L, pick), (None, None, 1))
            rows.append((pick == winner, ask, sz, err, abs(gap), w))
        rows_by_lead[L] = rows
        if not rows:
            print(f"{L:>5} {'':>6} {0:>6}")
            continue
        n = len(rows)
        k_ = sum(r[0] for r in rows)
        clo, chi = wilson(k_, n)
        u = min(NSEC, L)
        known_side = [r for r in rows if not r[3]]
        pr = [r for r in known_side if r[1] is not None]
        head = (f"{L:>5} {u/NSEC:>6.2f} {n:>6} {100*k_/n:>8.1f}% "
                f"[{100*clo:>4.0f},{100*chi:>4.0f}]%")
        if not pr or not known_side:
            print(head + f" {0:>6.0f}%")
            continue
        avg = sum(r[1] for r in pr) / len(pr)
        # accuracy from the FULL population at this lead; price from what was
        # actually quoted. Mixing the two is the point: the question is
        # whether the book charges for uncertainty that no longer exists.
        ev = k_ / n - avg - fee(avg)
        print(head + f" {100*len(pr)/len(known_side):>6.0f}% {avg:>8.3f} "
                     f"{100*(avg+fee(avg)):>6.1f}% {100*ev:>7.2f}c")

    print("\n'u/n' = the share of the settling average still unwritten at that")
    print("lead. It is the mechanical reason the outcome is knowable early:")
    print("a late move changes the settled mean by only u/n of itself.")
    print("'sig hit%' is measured on EVERY window, not on the ones that filled.")
    print("READ THAT EV COLUMN AS AN UPPER BOUND. It pairs an unconditional")
    print("hit rate with the price of the quoted subset, and quoting is not")
    print("random — that exact pairing is what made the maker result wrong.")
    print("The by-price table below conditions both on the same rows.")

    # -------------------------------------------------- per-price decomposition
    print("\n\nBY PRICE — the split that located the old edge")
    print(f"{'bucket':>12} {'lead':>5} {'n':>5} {'hit%':>6} {'95% CI':>13} "
          f"{'avg ask':>8} {'EV c/sh':>8} {'EV lo':>8} {'$/day':>9}")
    grand = []
    for a, b in BUCKETS:
        for L in LEADS:
            sel = [r for r in rows_by_lead.get(L, [])
                   if r[1] is not None and not r[3] and a <= r[1] < b]
            if not sel:
                continue
            n = len(sel)
            k_ = sum(r[0] for r in sel)
            clo, _ = wilson(k_, n)
            avg = sum(r[1] for r in sel) / n
            ev = k_ / n - avg - fee(avg)
            ev_lo = clo - avg - fee(avg)
            sh = sum(min(r[2] or 0, CLIP) for r in sel) / n
            per_day = ev * sh * n * 24.0 / span_h if span_h else 0.0
            print(f"{f'{a:.2f}-{b:.2f}':>12} {L:>5} {n:>5} {100*k_/n:>5.0f}% "
                  f"[{100*clo:>4.0f},{100*wilson(k_, n)[1]:>4.0f}]% {avg:>8.3f} "
                  f"{100*ev:>7.2f}c {100*ev_lo:>7.2f}c {per_day:>9.2f}")
            grand.append((ev_lo, per_day, a, b, L, n))
    if not grand:
        print("  (no priced observations yet)")
        return

    # ------------------------------------------------------------- the verdict
    live = [x for x in grand if x[0] > 0]
    print("\nEV lo = EV at the 95% LOWER bound of the hit rate. That is the")
    print("column to read. Four of the last five promising cells on this")
    print("project were in-sample luck and died out of sample; a cell whose")
    print("lower bound is negative has not been shown to be anything.")
    if live:
        live.sort(key=lambda x: -x[1])
        print(f"\n{len(live)} cell(s) positive at the lower bound:")
        for ev_lo, per_day, a, b, L, n in live[:6]:
            print(f"  ask {a:.2f}-{b:.2f} at lead {L}s: n={n}, "
                  f"{100*ev_lo:+.2f}c/sh floor, ~${per_day:.2f}/day")
        print("\nNext: re-run after another day. A cell that survives a second,")
        print("disjoint sample is worth wiring; one that does not, is not.")
    else:
        print("\nNo cell is positive at its lower bound. Cheap-and-right does")
        print("not currently exist in this family at these leads — the book is")
        print("charging for the accuracy we have.")


if __name__ == "__main__":
    main()
