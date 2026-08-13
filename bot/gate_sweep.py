"""Where should the gate sit? The per-decision money-gate sweep, full tape.

  venv/bin/python -m bot.gate_sweep
  COIN=eth venv/bin/python -m bot.gate_sweep
  FAMILY=15m venv/bin/python -m bot.gate_sweep

"THIS BASIS POINT THING", measured the way the money is made. The paper
gates (btc 5m 0.5, eth 5m 1.0, btc 15m 1.0, eth 15m 1.7) were cut on early
row-level data, and the live money gate for btc 5m was provisionally set to
1.0bp on the strength of one tape read. A gate trades FREQUENCY for WIN
RATE, so neither number alone decides it: the objective is cents per DAY
per share = (settle% - break-even at the paid price) x windows/day. This
tool sweeps candidate gates over every settled window in the backfilled
tape, per decision, and reports that product with its time-split halves.

Three tables and what each is for:
  CUMULATIVE   each candidate gate's n, windows/day, settle% (Wilson), edge
               vs break-even at PAID, and c/day -- the ranking that matters.
  MARGINAL     the windows BETWEEN consecutive band edges. The live money-
               gate question is exactly "does the lowest band pay for
               itself?" -- a band that loses is a fee donor no matter how
               good the windows above it are.
  BOOK-PRICED  the same bands priced at their own recorded T-3 ask instead
               of the fleet-average PAID, where the book recorder has the
               window. High-tilt windows tend to cost more -- a win-rate
               advantage that dies at its own ask is not an advantage
               (entry_ceiling already showed pricing can invert a result).

THE PRE-REGISTERED BAR. A different gate is actionable on this unit only if
its c/day beats the current gate's in BOTH time halves -- and even then it
is a config PROPOSAL to re-verify after more uncensored days, not an
immediate edit. The marginal-band money-gate verdict needs the same edge
sign in both halves. Read-only.
"""
import os
import sqlite3

from bot.pnl_daily import breakeven, wilson
from bot.scalp_backtest import CACHE, tilt_at
from bot.twap_verify import COIN, DB_DIR, FAMILY, WINDOW

BOOK_DIR = os.environ.get("BOOK_DIR", "bot/data/bookcal")
LEAD = 3
_GATES = {("btc", "5m"): 0.5, ("btc", "15m"): 1.0,
          ("eth", "5m"): 1.0, ("eth", "15m"): 1.7}
CUR = float(os.environ.get("CUR", _GATES.get((COIN, FAMILY), 1.0)))
_SWEEP = {"5m": "0.5,0.75,1.0,1.25,1.5,2.0",
          "15m": "1.0,1.25,1.5,1.7,2.0,2.5"}
GATES = sorted({CUR} | {float(x) for x in os.environ.get(
    "GATES", _SWEEP.get(FAMILY, _SWEEP["5m"])).split(",")})
_BANDS = {"5m": "0.5,1.0,2.0", "15m": "1.0,1.7,2.5"}
BANDS = [float(x) for x in os.environ.get(
    "BANDS", _BANDS.get(FAMILY, _BANDS["5m"])).split(",")]
PAID = float(os.environ.get("PAID", "0.5253"))     # measured fleet average
MIN_N = int(os.environ.get("MIN_N", "40"))


def split_stats(sub, days, be):
    """(n, settle%, wilson, edge_c, wday, cday) + the same for each half."""
    def one(s, d):
        n = len(s)
        if not n or not d:
            return None
        k = sum(1 for r in s if r[1] == r[2])
        lo, hi = wilson(k, n)
        edge = 100 * (k / n - be)
        wday = n / d
        return {"n": n, "wr": k / n, "lo": lo, "hi": hi,
                "edge": edge, "wday": wday, "cday": edge * wday}
    half = len(sub) // 2
    return (one(sub, days), one(sub[:half], days / 2),
            one(sub[half:], days / 2))


def main():
    gp = os.path.join(DB_DIR, f"{COIN}_1s.db")
    tp = os.path.join(CACHE, f"{COIN}_{FAMILY}_tape.db")
    for p in (gp, tp):
        if not os.path.exists(p):
            raise SystemExit(f"missing {p}")
    grid = dict(sqlite3.connect(f"file:{gp}?mode=ro", uri=True)
                .execute("SELECT ts, v FROM px"))
    wl = list(sqlite3.connect(f"file:{tp}?mode=ro", uri=True).execute(
        "SELECT wts, winner FROM tape WHERE winner IS NOT NULL ORDER BY wts"))
    book = {}
    bp = os.path.join(BOOK_DIR, f"{COIN}_{FAMILY}_book.db")
    if os.path.exists(bp):
        for w, s, a in sqlite3.connect(
                f"file:{bp}?mode=ro", uri=True).execute(
                "SELECT wts, side, ask FROM book WHERE lead=? "
                "AND ask IS NOT NULL", (WINDOW + LEAD,)):
            book[(w, s)] = a

    rows = []
    for w, win in wl:
        t = tilt_at(grid, w)
        if not t or t[0] is None:
            continue
        rows.append((w, t[1], win, abs(t[0])))
    if not wl:
        raise SystemExit("empty tape")
    days = (wl[-1][0] - wl[0][0] + WINDOW) / 86400
    be = breakeven(PAID)
    print(f"{COIN} {FAMILY}: {len(rows)} priceable settled windows over "
          f"{days:.1f} days of tape; current gate {CUR}bp; break-even at "
          f"the measured paid {PAID} = {100*be:.2f}%")
    if len(rows) < 2 * MIN_N:
        print("Too few windows; backfill the tape or wait.")
        return

    print(f"\nCUMULATIVE -- the objective is c/day, not settle%")
    print(f"{'gate':>6} {'n':>5} {'w/day':>6} {'settle%':>8} "
          f"{'95% CI':>15} {'edge c':>7} {'c/day':>8} "
          f"{'h1 c/day':>9} {'h2 c/day':>9}")
    stats = {}
    for g in GATES:
        sub = [r for r in rows if r[3] >= g]
        full, h1, h2 = split_stats(sub, days, be)
        if not full:
            continue
        stats[g] = (full, h1, h2)
        print(f"{g:>6} {full['n']:>5} {full['wday']:>6.1f} "
              f"{100*full['wr']:>7.1f}% "
              f"[{100*full['lo']:>5.1f}, {100*full['hi']:>5.1f}] "
              f"{full['edge']:>+7.2f} {full['cday']:>+8.1f} "
              f"{h1['cday'] if h1 else 0:>+9.1f} "
              f"{h2['cday'] if h2 else 0:>+9.1f}")

    print("\nMARGINAL BANDS -- does each slice pay for itself at PAID?")
    print(f"{'band':>12} {'n':>5} {'settle%':>8} {'95% CI':>15} "
          f"{'edge c':>7} {'h1%':>6} {'h2%':>6}")
    edges = BANDS + [float("inf")]
    bandstats = {}
    for lo_b, hi_b in zip(edges, edges[1:]):
        sub = [r for r in rows if lo_b <= r[3] < hi_b]
        full, h1, h2 = split_stats(sub, days, be)
        if not full or full["n"] < 5:
            continue
        lab = f"{lo_b}-{hi_b}bp" if hi_b != float("inf") else f"{lo_b}bp+"
        bandstats[lo_b] = (full, h1, h2, lab)
        print(f"{lab:>12} {full['n']:>5} {100*full['wr']:>7.1f}% "
              f"[{100*full['lo']:>5.1f}, {100*full['hi']:>5.1f}] "
              f"{full['edge']:>+7.2f} "
              f"{100*h1['wr'] if h1 else 0:>5.1f}% "
              f"{100*h2['wr'] if h2 else 0:>5.1f}%")

    print("\nBOOK-PRICED BANDS -- same bands at their own recorded T-3 ask")
    print(f"{'band':>12} {'n_book':>6} {'ask':>7} {'settle%':>8} "
          f"{'b/e%':>6} {'edge c':>7}")
    for lo_b, hi_b in zip(edges, edges[1:]):
        sub = [r for r in rows
               if lo_b <= r[3] < hi_b and (r[0], r[1]) in book]
        if len(sub) < 5:
            continue
        lab = f"{lo_b}-{hi_b}bp" if hi_b != float("inf") else f"{lo_b}bp+"
        ask = sum(book[(r[0], r[1])] for r in sub) / len(sub)
        k = sum(1 for r in sub if r[1] == r[2])
        bb = breakeven(ask)
        print(f"{lab:>12} {len(sub):>6} {ask:>7.4f} {100*k/len(sub):>7.1f}% "
              f"{100*bb:>5.1f}% {100*(k/len(sub)-bb):>+7.2f}")
    if not book:
        print("  (no book db -- the recorders were started 08-11; wait)")

    print("\nVERDICTS (pre-registered)")
    elig = [g for g in stats if stats[g][0]["n"] >= MIN_N]
    if elig and CUR in stats:
        best = max(elig, key=lambda g: stats[g][0]["cday"])
        cf, c1, c2 = stats[CUR]
        bf, b1, b2 = stats[best]
        if best == CUR:
            print(f"  gate: the current {CUR}bp already has the best c/day "
                  f"of the eligible candidates -- keep it.")
        elif (b1 and b2 and c1 and c2
                and b1["cday"] > c1["cday"] and b2["cday"] > c2["cday"]):
            print(f"  gate: {best}bp beats the current {CUR}bp on c/day in "
                  f"BOTH halves ({b1['cday']:+.1f} vs {c1['cday']:+.1f}; "
                  f"{b2['cday']:+.1f} vs {c2['cday']:+.1f}). Actionable as "
                  f"a PROPOSAL: re-run after more uncensored days before "
                  f"touching config.")
        else:
            print(f"  gate: {best}bp has the best overall c/day but does "
                  f"NOT beat {CUR}bp in both halves -- not actionable, "
                  f"keep the current gate.")
    lo_b = BANDS[0]
    if lo_b in bandstats:
        full, h1, h2, lab = bandstats[lo_b]
        if h1 and h2 and full["n"] >= MIN_N:
            s1 = 100 * h1["wr"] - 100 * be
            s2 = 100 * h2["wr"] - 100 * be
            if s1 < 0 and s2 < 0:
                print(f"  money gate: the {lab} band loses in BOTH halves "
                      f"({s1:+.1f}c, {s2:+.1f}c) -- it is a fee donor; the "
                      f"money gate belongs at {BANDS[1]}bp or above.")
            elif s1 > 0 and s2 > 0:
                print(f"  money gate: the {lab} band pays in BOTH halves "
                      f"({s1:+.1f}c, {s2:+.1f}c) -- the money gate can "
                      f"match the paper gate at {lo_b}bp.")
            else:
                print(f"  money gate: the {lab} band is mixed across halves "
                      f"({s1:+.1f}c, {s2:+.1f}c) -- unresolved; keep "
                      f"collecting before deciding.")


if __name__ == "__main__":
    main()
