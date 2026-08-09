"""Where in the window does the signal actually live now?

  venv/bin/python -m bot.timing_scan
  COIN=eth FAMILY=15m venv/bin/python -m bot.timing_scan

The 2026-08-07 rule change settles a window on its closing TWAP, and at any
decision time that average is part history, part future:

    est(L) = (known over [C-N, C-L) + L * spot) / N      L = seconds left

Two forces pull against each other as L grows:
  - LATE  (small L): the average is nearly locked, so it barely responds to
    the price — a big spot move buys only L/N of the gap, and the market has
    already withdrawn its offers.
  - EARLY (large L): the spot carries the full weight of the estimate, but
    more of the window is still unwritten, so the call can be wrong.

This scans the recorded 1s Chainlink grid against official outcomes and asks,
for each lead time, how often the projected average called the winner and how
big the projected gap was. It answers whether the edge MOVED IN TIME rather
than disappearing. Read-only: no bot, no orders, no config touched.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor

from bot.twap_verify import COIN, FAMILY, NSEC, WINDOW, load_grid, official

LEADS = [int(x) for x in os.environ.get(
    "LEADS", "3,6,10,15,20,30,45,60,90,120").split(",")]
MIN_COVER = 0.9


def main():
    g = load_grid()
    lo, hi = min(g), max(g)
    print(f"grid {len(g)} samples, {(hi-lo)/3600:.1f}h "
          f"({time.strftime('%m-%d %H:%M', time.gmtime(lo))} -> "
          f"{time.strftime('%m-%d %H:%M', time.gmtime(hi))} UTC)")
    print(f"{COIN} {FAMILY}: window {WINDOW}s, TWAP {NSEC}s\n")

    def mean(a, b):
        v = [g[s] for s in range(a, b) if s in g]
        return (sum(v) / len(v), len(v) / (b - a)) if v else (None, 0.0)

    def spot(t):
        for kk in (t, t - 1, t - 2):
            if kk in g:
                return g[kk]
        return None

    wtss = [w for w in range(lo - lo % WINDOW, hi - WINDOW, WINDOW)
            if w - NSEC >= lo and w + WINDOW <= hi]
    with ThreadPoolExecutor(16) as ex:
        outcomes = {w: r for w, r in ex.map(official, wtss) if r}
    print(f"windows with grid + official outcome: {len(outcomes)}\n")
    if not outcomes:
        return

    print(f"{'lead':>5} {'n':>5} {'called':>7} {'hit%':>6} {'|gap| p50':>10} "
          f"{'|gap| p90':>10} {'hit% |gap|>2bp':>15} {'n>2bp':>6}")
    for L in LEADS:
        rows = []
        for w, winner in sorted(outcomes.items()):
            C = w + WINDOW
            k, ck = mean(w - NSEC, w)                 # strike: TWAP at open
            if k is None or ck < MIN_COVER:
                continue
            t = C - L                                  # decision instant
            elapsed = max(0, min(NSEC, t - (C - NSEC)))
            if elapsed:
                km, cov = mean(C - NSEC, C - NSEC + elapsed)
                if km is None or cov < MIN_COVER:
                    continue
                known_sum = km * elapsed
            else:
                known_sum = 0.0
            s = spot(t)
            if s is None:
                continue
            est = (known_sum + (NSEC - elapsed) * s) / NSEC
            gap_bp = (est - k) / k * 1e4
            rows.append((gap_bp, "up" if est >= k else "down", winner))
        if not rows:
            print(f"{L:>5} {'—':>5}")
            continue
        hit = sum(1 for _, c, wn in rows if c == wn)
        gaps = sorted(abs(r[0]) for r in rows)
        big = [r for r in rows if abs(r[0]) > 2.0]
        bh = sum(1 for _, c, wn in big if c == wn)
        print(f"{L:>5} {len(rows):>5} {hit:>7} {100*hit/len(rows):>5.1f}% "
              f"{gaps[len(gaps)//2]:>9.2f} {gaps[min(len(gaps)-1, 9*len(gaps)//10)]:>9.2f} "
              f"{(f'{100*bh/len(big):.1f}%' if big else '—'):>15} {len(big):>6}")

    print("\nreading it: hit% is direction accuracy of the PROJECTED average.")
    print("A lead time is only interesting if accuracy stays high AND the")
    print("projected gap is big enough to clear the entry filter. Accuracy")
    print("here is signal-only — it says nothing about whether an ask was")
    print("resting at that moment, which is the separate liquidity question.")


if __name__ == "__main__":
    main()
