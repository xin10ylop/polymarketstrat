"""The pre-open entry: is the book flat, is there size, and does it snap?

  venv/bin/python -m bot.preopen_report
  COIN=eth venv/bin/python -m bot.preopen_report

THE STRATEGY. At T-n seconds, before the window opens, buy the side the
opening tilt favours; rest a limit sell at +5c; the book reprices at the
open and the limit fills. The tilt is the distance between spot and the
strike, and since the rule change the strike is the 30s mean ending at T —
so at T-3 we already hold 27 of its 30 seconds and the SIDE is callable in
advance (98.9% btc / 97.4% eth sign agreement on windows over 1bp).

NO LOOKAHEAD HERE. At T-n only 30-n seconds of the strike exist. This
builds the strike from exactly those, imputing the unseen tail from the
last print — the same information a live bot would have. price_curve uses
all 30 seconds and therefore OVERSTATES the pre-open signal; that is why
this tool exists rather than another lead in that table.

FOUR THINGS DECIDE IT, and each has its own column:
  LEAN   the ask on the side we pick minus the ask on the other side. If
         the book already leans our way pre-open, the market makers have
         done the same arithmetic and the edge shrinks by the lean.
  SIZE   EXECUTABLE depth on our side — everything within 5c of the touch,
         which is what a marketable order actually takes. Recording only
         the touch produced a false alarm: one sample showed 14 shares at
         the best ask and I concluded liquidity collapses into the open.
         The full ladder at that instant held 1,889 shares inside five
         cents, and depth from T-30 to T-3 barely moved. Makers do not
         pull. The touch is not the tradeable size.
  SNAP   where our side trades at T+2/T+15. A +5c limit only fills if the
         snap clears it.
  WIN    whether our side actually settles the winner, which is what
         matters if the limit does NOT fill and we are left holding.

Read-only.
"""
import json
import os
import statistics as st
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from bot.twap_verify import COIN, FAMILY, HDRS, NSEC, WINDOW, load_grid, official
from bot.price_curve import load_book_depth as load_book

PRE = [int(x) for x in os.environ.get("PRE", "20,10,5,3").split(",")]
POST = [int(x) for x in os.environ.get("POST", "2,15,30").split(",")]
EXIT_C = float(os.environ.get("EXIT_C", "0.05"))
TILT_MIN = float(os.environ.get("TILT_MIN", "0"))     # bp gate
CLIP = float(os.environ.get("CLIP", "250"))


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


def main():
    g = load_grid()
    book = load_book()
    lo, hi = min(g), max(g)
    # pre-open rows are stored under the window they OPEN, at lead WINDOW+n
    wtss = sorted({w for (w, L, _) in book if L > WINDOW})
    wtss = [w for w in wtss if w - NSEC >= lo and w + WINDOW <= hi]
    if not wtss:
        raise SystemExit("no pre-open rows yet — set PREOPEN= on the recorders")
    with ThreadPoolExecutor(16) as ex:
        won = {w: r for w, r in ex.map(official, wtss) if r}
    print(f"{COIN} {FAMILY}: {len(wtss)} windows with pre-open book, "
          f"{len(won)} settled"
          + (f" | tilt gate >{TILT_MIN}bp" if TILT_MIN else "") + "\n")

    def spot(t):
        for kk in (t, t - 1, t - 2, t - 3):
            if kk in g:
                return g[kk]

    def rng(a, b):
        v = [g[s] for s in range(a, b) if s in g]
        return (sum(v), len(v)) if v else (None, 0)

    print(f"{'entry':>6} {'n':>5} {'lean':>7} {'our ask':>8} {'size':>7} "
          f"{'T+2':>7} {'T+15':>7} {'win%':>6} {'95% CI':>13} {'hold EV':>8} "
          f"{'EV lo':>8}")
    for n_ in PRE:
        rows = []
        for w in wtss:
            # strike from ONLY the seconds available at T-n
            s = spot(w - n_)
            psum, pcnt = rng(w - NSEC, w - n_)
            if s is None or pcnt < (NSEC - n_) * 0.8:
                continue
            k = (psum * ((NSEC - n_) / pcnt) + n_ * s) / NSEC
            tilt = (s - k) / k * 1e4
            if TILT_MIN and abs(tilt) < TILT_MIN:
                continue
            pick = "up" if tilt >= 0 else "down"
            other = "down" if pick == "up" else "up"
            a_us = book.get((w, WINDOW + n_, pick), (None, None, 1))
            a_th = book.get((w, WINDOW + n_, other), (None, None, 1))
            if a_us[2] or a_us[0] is None:
                continue
            post = {}
            for p_ in POST:
                v = book.get((w, WINDOW - p_, pick), (None, None, 1))
                post[p_] = None if (v[2] or v[0] is None) else v[0]
            rows.append((a_us[0], a_us[1] or 0.0,
                         None if a_th[2] else a_th[0], post,
                         won.get(w) == pick if w in won else None, tilt))
        if len(rows) < 5:
            print(f"{f'T-{n_}s':>6} {len(rows):>5}   (too few)")
            continue
        ask = st.mean(r[0] for r in rows)
        sz = sorted(r[1] for r in rows)
        oth = [r[2] for r in rows if r[2] is not None]
        lean = (ask - st.mean(oth)) if oth else float("nan")
        p2 = [r[3].get(2) for r in rows if r[3].get(2) is not None]
        p15 = [r[3].get(15) for r in rows if r[3].get(15) is not None]
        settled = [r[4] for r in rows if r[4] is not None]
        if settled:
            k_ = sum(settled)
            n2 = len(settled)
            clo, chi = wilson(k_, n2)
            ev = k_ / n2 - ask - fee(ask)
            evlo = clo - ask - fee(ask)
            wtxt = (f"{100*k_/n2:>5.1f}% [{100*clo:>4.1f},{100*chi:>4.1f}]% "
                    f"{100*ev:>7.2f}c {100*evlo:>7.2f}c")
        else:
            wtxt = f"{'-':>6} {'-':>13} {'-':>8} {'-':>8}"
        print(f"{f'T-{n_}s':>6} {len(rows):>5} {100*lean:>+6.2f}c {ask:>8.3f} "
              f"{sz[len(sz)//2]:>7.0f} "
              f"{st.mean(p2) if p2 else float('nan'):>7.3f} "
              f"{st.mean(p15) if p15 else float('nan'):>7.3f} {wtxt}")

    print("\nLEAN is the whole question. Positive means the book already")
    print("charges more for the side we want — the makers ran the same")
    print("arithmetic and the edge shrinks by exactly that much.")
    print("SIZE is the median EXECUTABLE depth within 5c of the touch, not")
    print("the touch itself — the touch understated it by ~100x in the one")
    print("window that made me call liquidity a problem.")
    print(f"A +{100*EXIT_C:.0f}c limit fills only if the T+2 or T+15 column")
    print("clears entry + that much. If it does not, the win% column is what")
    print("you are actually holding.")


if __name__ == "__main__":
    main()
