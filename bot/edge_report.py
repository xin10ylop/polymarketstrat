"""Price the earlier-in-the-window idea: signal + book + outcome, joined.

  venv/bin/python -m bot.edge_report            # btc 5m
  GAP_BPS=2 venv/bin/python -m bot.edge_report

Three sources, one table:
  bot/twap_record.py  -> the 1s Chainlink grid  -> our projected-TWAP signal
  bot/book_record.py  -> what was actually offered at each lead
  gamma               -> who actually won

For each lead time it asks the only question that matters: when our signal
fired, was the side it picked ON OFFER, at what price, and would buying it
have made money after fees?

EV per share = P(win) - ask - 0.07*ask*(1-ask). A 98% call bought at 0.99
LOSES; accuracy alone is not an edge, which is why this exists.
Read-only.
"""
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

from bot.twap_verify import COIN, FAMILY, NSEC, WINDOW, load_grid, official

LEADS = [int(x) for x in os.environ.get(
    "LEADS", "120,90,60,45,30,20,10,6,3").split(",")]
GAP_BPS = float(os.environ.get("GAP_BPS", "2.0"))
CLIP = float(os.environ.get("CLIP", "250"))
BOOK_DIR = os.environ.get("BOOK_DIR", "bot/data/bookcal")
MIN_COVER = 0.9


def fee(p):
    return 0.07 * p * (1 - p)


def load_book():
    path = os.path.join(BOOK_DIR, f"{COIN}_{FAMILY}_book.db")
    if not os.path.exists(path):
        raise SystemExit(f"no book recording at {path} — run bot.book_record")
    db = sqlite3.connect(path)
    out = {}
    for wts, lead, side, ask, sz in db.execute(
            "SELECT wts, lead, side, ask, ask_sz FROM book"):
        out[(wts, lead, side)] = (ask, sz)
    return out


def main():
    g = load_grid()
    book = load_book()
    lo, hi = min(g), max(g)
    wtss = sorted({w for (w, _, _) in book} & {
        w for w in range(lo - lo % WINDOW, hi + WINDOW, WINDOW)})
    wtss = [w for w in wtss if w - NSEC >= lo and w + WINDOW <= hi]
    with ThreadPoolExecutor(16) as ex:
        won = {w: r for w, r in ex.map(official, wtss) if r}
    print(f"{COIN} {FAMILY}: {len(won)} windows with grid + book + outcome "
          f"| signal gate |gap| > {GAP_BPS}bp | clip {CLIP:.0f}\n")
    if not won:
        print("nothing joinable yet — the book recorder needs to overlap "
              "windows the grid also covers, and those windows must have settled")
        return

    def mean(a, b):
        v = [g[s] for s in range(a, b) if s in g]
        return (sum(v) / len(v), len(v) / (b - a)) if v else (None, 0.0)

    def spot(t):
        for kk in (t, t - 1, t - 2):
            if kk in g:
                return g[kk]
        return None

    # break-even accuracy is the number to beat: anything below it loses,
    # however confident we feel. Printing it next to the observed hit rate
    # stops a small-sample 100% from reading as an edge.
    print(f"{'lead':>5} {'sig':>4} {'offered':>8} {'avg ask':>8} {'need%':>7} "
          f"{'hit%':>6} {'EV c/sh':>8} {'$/trade':>8} {'$ total':>8}   ask win/lose")
    best = None
    for L in LEADS:
        sig = 0
        rows = []
        w_av = w_tot = l_av = l_tot = 0
        for w, winner in sorted(won.items()):
            C = w + WINDOW
            k, ck = mean(w - NSEC, w)
            if k is None or ck < MIN_COVER:
                continue
            elapsed = max(0, min(NSEC, (C - L) - (C - NSEC)))
            if elapsed:
                km, cov = mean(C - NSEC, C - NSEC + elapsed)
                if km is None or cov < MIN_COVER:
                    continue
                known = km * elapsed
            else:
                known = 0.0
            s = spot(C - L)
            if s is None:
                continue
            est = (known + (NSEC - elapsed) * s) / NSEC
            gap = (est - k) / k * 1e4
            # availability by whether the side actually won (the diagnostic
            # that explains the empty book near the close)
            for side in ("up", "down"):
                a = book.get((w, L, side), (None, None))[0]
                if side == winner:
                    w_tot += 1
                    w_av += a is not None
                else:
                    l_tot += 1
                    l_av += a is not None
            if abs(gap) < GAP_BPS:
                continue
            sig += 1
            pick = "up" if gap > 0 else "down"
            ask, sz = book.get((w, L, pick), (None, None))
            if ask is None or sz is None:
                continue
            rows.append((ask, min(sz, CLIP), 1.0 if pick == winner else 0.0))
        if not sig:
            print(f"{L:>5} {0:>4}")
            continue
        av = (f"{100*w_av/w_tot:.0f}%/{100*l_av/l_tot:.0f}%"
              if w_tot and l_tot else "—")
        if not rows:
            print(f"{L:>5} {sig:>4} {0:>8} {'—':>8} {'—':>7} {'—':>6} {'—':>8} "
                  f"{'—':>8} {'—':>8}   {av}")
            continue
        hit = sum(r[2] for r in rows) / len(rows)
        ev = sum(r[2] - r[0] - fee(r[0]) for r in rows) / len(rows)
        dollars = sum((r[2] - r[0] - fee(r[0])) * r[1] for r in rows)
        avg_ask = sum(r[0] for r in rows) / len(rows)
        need = 100 * (avg_ask + fee(avg_ask))
        print(f"{L:>5} {sig:>4} {len(rows):>8} {avg_ask:>8.3f} {need:>6.1f}% "
              f"{100*hit:>5.0f}% {100*ev:>7.2f}c {dollars/len(rows):>7.2f} "
              f"{dollars:>8.2f}   {av}")
        if best is None or dollars / len(rows) > best[1]:
            best = (L, dollars / len(rows), len(rows))

    print("\n'need%' = accuracy required just to break even (ask + fee).")
    print("Compare it to a hit% measured on HUNDREDS of windows, not to the")
    print("hit% on this page — a clean run of 7 trades is not 100% accuracy.")
    print("'offered' = signals where the picked side actually had an ask.")
    print("'ask on winner/loser' = how often each side was quoted at all —")
    print("  the winner's number collapsing near the close IS the drought.")
    if best:
        print(f"\nbest lead so far: {best[0]}s at ${best[1]:.2f}/trade "
              f"over {best[2]} trades — treat as a SHAPE, not a number, until")
        print("  the sample is days not hours and covers a livelier regime.")


if __name__ == "__main__":
    main()
