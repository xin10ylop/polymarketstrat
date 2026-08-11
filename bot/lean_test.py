"""Is a LEANING pre-open book a cost, or is it a signal?

  venv/bin/python -m bot.lean_test
  COIN=eth venv/bin/python -m bot.lean_test
  GATE=1.0 LEAD=3 venv/bin/python -m bot.lean_test

THE QUESTION, AND WHY IT MATTERS MORE THAN ANY GATE. preopen.py refuses a
window when the ask on our side is above PREOPEN_MAX_PX, on this stated
theory:

    "A LEANING BOOK IS THE ONE THING THAT KILLS THIS. If the makers have
     already priced the tilt, the entry is no longer ~0.50 and the edge
     shrinks one-for-one. Refuse rather than pay it."

The live ledger says the opposite. Sweeping the ceiling over 130 btc 5m fills
and 63 btc 15m fills, the CHEAPEST entries lose money on both:

    btc 5m   ask <= 0.50: 24 fills, 45.8%, -4.66c/share, -$276
             ask <= 0.56: 130 fills, 56.2%, +1.97c/share, +$471
    btc 15m  ask <= 0.50: 34 fills, 47.1%, -2.68c/share,  -$61
             ask <= 0.56:  63 fills, 61.9%, +9.55c/share, +$575

If that is real the mechanism is plain: a HIGH ask on our side means the book
already agrees with the tilt, and the book is informed. A LOW ask means we
are buying the side the market thinks will lose. The rule would then be
inverted — pay up when the book agrees, refuse when it does not — and the
current ceiling is discarding the good half.

WHY THIS TOOL AND NOT THE LEDGER. 130 fills over two days cannot settle it,
eth ran the other way, and every 95% lower bound in that sweep was negative.
This joins the book ARCHIVE (every pre-open snapshot the recorders took,
whether or not the bot traded) to the official outcomes in the tape cache —
the same question at many times the sample, and free of the selection the
live ceiling imposes, because the archive contains the windows the bot
REFUSED as well as the ones it took.

TWO MEASURES, because they are not the same thing:
  ASK      the level on our side. This is what PREOPEN_MAX_PX gates, so it
           is the actionable one.
  LEAN     our ask minus the other side's ask. This is the mechanism — it
           says the book DISAGREES between the two sides, independent of
           where both happen to sit.

GUARDED. Split in time, first half to look, second half to confirm. This
project has retracted a vol-scaled gate twice and an entry-ceiling
hypothesis once today; a bucketed win rate is exactly the shape that
produces them.

Read-only.
"""
import os
import sqlite3

from bot.pnl_daily import breakeven, wilson
from bot.scalp_backtest import CACHE, tilt_at
from bot.twap_verify import COIN, DB_DIR, FAMILY, NSEC, WINDOW

BOOK_DIR = os.environ.get("BOOK_DIR", "bot/data/bookcal")
LEAD = int(os.environ.get("LEAD", "3"))
GATE = float(os.environ.get("GATE", "1.0"))
ASK_EDGES = (0.0, 0.48, 0.50, 0.51, 0.52, 0.53, 0.54, 0.56, 1.01)
LEAN_EDGES = (-1.0, -0.04, -0.02, -0.005, 0.005, 0.02, 0.04, 1.0)


def bucket(v, edges):
    for i in range(len(edges) - 1):
        if edges[i] <= v < edges[i + 1]:
            return i
    return None


def stats(rows):
    """(n, win%, avg ask, break-even, edge_c, lower bound_c)."""
    n = len(rows)
    if not n:
        return None
    wins = sum(1 for _, _, w in rows if w)
    px = sum(a for a, _, _ in rows) / n
    be = breakeven(px)
    lo, _ = wilson(wins, n)
    return n, wins / n, px, be, (wins / n - be) * 100, (lo - be) * 100


def show(title, rows, edges, key, fmt):
    print(f"\n{title}")
    print(f"{'bucket':>14} {'n':>5} {'win%':>6} {'avg ask':>8} {'b/e%':>6} "
          f"{'edge¢':>7} {'95% lo':>8}")
    for i in range(len(edges) - 1):
        sub = [r for r in rows if bucket(key(r), edges) == i]
        s = stats(sub)
        if not s or s[0] < 5:
            continue
        n, wr, px, be, edge, lo = s
        print(f"{fmt(edges[i], edges[i+1]):>14} {n:>5} {100*wr:>5.1f}% "
              f"{px:>8.4f} {100*be:>5.2f}% {edge:>+7.2f} {lo:>+8.2f}")


def main():
    gp = os.path.join(DB_DIR, f"{COIN}_1s.db")
    bp = os.path.join(BOOK_DIR, f"{COIN}_{FAMILY}_book.db")
    tp = os.path.join(CACHE, f"{COIN}_{FAMILY}_tape.db")
    for p in (gp, bp, tp):
        if not os.path.exists(p):
            raise SystemExit(f"missing {p}")
    grid = dict(sqlite3.connect(f"file:{gp}?mode=ro", uri=True)
                .execute("SELECT ts, v FROM px"))
    book = {(w, s): a for w, s, a in sqlite3.connect(
        f"file:{bp}?mode=ro", uri=True).execute(
        "SELECT wts, side, ask FROM book WHERE lead=? AND ask IS NOT NULL",
        (WINDOW + LEAD,))}
    tape = dict(sqlite3.connect(f"file:{tp}?mode=ro", uri=True)
                .execute("SELECT wts, winner FROM tape "
                         "WHERE winner IS NOT NULL"))
    print(f"{COIN} {FAMILY}: grid {len(grid)}s, "
          f"book at T-{LEAD} {len(book)//2} windows, tape {len(tape)} outcomes")

    rows = []
    for w, winner in sorted(tape.items()):
        t = tilt_at(grid, w)
        if not t or t[0] is None:
            continue
        tilt, pick = t[0], t[1]
        if abs(tilt) < GATE:
            continue
        ours = book.get((w, pick))
        other = book.get((w, "down" if pick == "up" else "up"))
        if ours is None or other is None:
            continue
        rows.append((ours, ours - other, pick == winner, w, tilt))
    print(f"{len(rows)} windows clear the {GATE}bp gate AND have a pre-open "
          f"book on both sides\n")
    if len(rows) < 60:
        raise SystemExit(
            "too few to say anything. The pre-open book leads (WINDOW+n) were\n"
            "only added to the recorders recently, so this needs more days\n"
            "before it can answer the question — do not read the tables below\n"
            "as evidence until this count is in the hundreds.")

    r3 = [(a, l, wn) for a, l, wn, _, _ in rows]
    s = stats(r3)
    print(f"ALL: {s[0]} windows, {100*s[1]:.1f}% settle the tilt side, "
          f"avg ask {s[2]:.4f}, break-even {100*s[3]:.2f}%, "
          f"edge {s[4]:+.2f}¢ [95% lo {s[5]:+.2f}]")

    show("BY THE ASK ON OUR SIDE (what PREOPEN_MAX_PX gates)",
         r3, ASK_EDGES, lambda r: r[0],
         lambda a, b: f"{a:.2f}-{b:.2f}")
    show("BY LEAN = our ask - their ask (the mechanism)",
         r3, LEAN_EDGES, lambda r: r[1],
         lambda a, b: f"{100*a:+.1f}..{100*b:+.1f}c")

    # ---- does the direction hold out of sample? ------------------------
    half = len(rows) // 2
    a_rows = [(x[0], x[1], x[2]) for x in rows[:half]]
    b_rows = [(x[0], x[1], x[2]) for x in rows[half:]]
    print("\n=== DOES IT HOLD OUT OF SAMPLE? ===")
    print(f"{'split':>22} {'cheap half':>22} {'dear half':>22}")
    for name, rr in (("first half", a_rows), ("SECOND half", b_rows)):
        # SPLIT BY RANK, NOT BY VALUE. A median split puts every row tied AT
        # the median into one side, and if the asks cluster on a few prices —
        # which quoted books do — the other side comes out EMPTY and the row
        # vanished silently. Ranking always yields two non-empty halves.
        srt = sorted(rr, key=lambda x: x[0])
        k = len(srt) // 2
        cheap, dear = stats(srt[:k]), stats(srt[k:])
        if not cheap or not dear:
            print(f"{name:>22}   too few rows to split "
                  f"({len(rr)}) — not silently skipped")
            continue
        print(f"{name:>22} "
              f"{f'{cheap[0]}w {100*cheap[1]:.1f}% {cheap[4]:+.2f}c':>22} "
              f"{f'{dear[0]}w {100*dear[1]:.1f}% {dear[4]:+.2f}c':>22}"
              f"   {'DEAR' if dear[4] > cheap[4] else 'cheap'} ahead")
    print("\nThe live ledger says DEAR beats CHEAP on btc, which would invert")
    print("preopen.py's stated premise. If both halves above agree with that,")
    print("it is worth acting on. If only one does, it is the same shape as")
    print("the vol-scaled gate — retracted twice — and it is not.")


if __name__ == "__main__":
    main()
