"""Would a RESTING BID have been filled, and would it have paid?

  venv/bin/python -m bot.maker_report
  COIN=eth venv/bin/python -m bot.maker_report

Crossing the ask is dead: measured on the real Chainlink grid over 332 BTC
and 97 ETH windows, our accuracy is 94-98% at every lead where liquidity
exists and the ask demands 95-99%. We are short by 1-3 points, consistently,
on both coins. The market has priced our forecast.

The one thing that changes that arithmetic is not forecasting better — it is
not paying the spread. Maker fee on this venue is ZERO, so a bid resting at
0.96 that gets hit is a completely different trade from lifting a 0.984 ask.

This estimates fill probability from the book snapshots we already record.
A bid resting at P is counted FILLED if any later snapshot in that window
shows an ask at or below P — someone was willing to sell there, and a
resting bid would have been the counterparty. Read-only.

Known biases, both stated rather than hidden:
  OPTIMISTIC - assumes we win the queue at that price; in reality other
    resting bids may absorb the flow first.
  PESSIMISTIC - only 9 snapshots per window are seen, so an offer that dips
    below P between snapshots is missed entirely.
Treat the fill rate as an order of magnitude, not a forecast.
"""
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from bot.twap_verify import COIN, FAMILY, NSEC, WINDOW, load_grid, official

# Accuracy MEASURED on the real grid over hundreds of windows
# (bot/timing_scan.py). EV must use this, never the realised outcome of a
# handful of filled trades — a clean run of 17 is not a 100% hit rate, and
# reading it as one is how this file lied the first time it was run.
ACC = {("btc", 120): 0.946, ("btc", 90): 0.953, ("btc", 60): 0.985,
       ("btc", 45): 0.985, ("eth", 120): 0.943, ("eth", 90): 0.969,
       ("eth", 60): 0.944, ("eth", 45): 1.000}
ENTRY_LEADS = [int(x) for x in os.environ.get("ENTRY", "120,90,60,45").split(",")]
BIDS = [float(x) for x in os.environ.get("BIDS", "0.98,0.96,0.94,0.92,0.90").split(",")]
GAP_BPS = float(os.environ.get("GAP_BPS", "2.0"))
CLIP = float(os.environ.get("CLIP", "250"))
BOOK_DIR = os.environ.get("BOOK_DIR", "bot/data/bookcal")
MIN_COVER = 0.9


def main():
    g = load_grid()
    path = os.path.join(BOOK_DIR, f"{COIN}_{FAMILY}_book.db")
    if not os.path.exists(path):
        raise SystemExit(f"no book recording at {path}")
    db = sqlite3.connect(path)
    book = {}
    for wts, lead, side, ask, sz in db.execute(
            "SELECT wts, lead, side, ask, ask_sz FROM book"):
        book[(wts, lead, side)] = (ask, sz)
    leads_seen = sorted({l for (_, l, _) in book}, reverse=True)

    lo, hi = min(g), max(g)
    wtss = sorted({w for (w, _, _) in book})
    wtss = [w for w in wtss if w - NSEC >= lo and w + WINDOW <= hi]
    with ThreadPoolExecutor(16) as ex:
        won = {w: r for w, r in ex.map(official, wtss) if r}
    span_h = (hi - lo) / 3600.0
    print(f"{COIN} {FAMILY}: {len(won)} windows, {span_h:.1f}h of grid | "
          f"gate |gap|>{GAP_BPS}bp | maker fee 0 | clip {CLIP:.0f}\n")
    if not won:
        print("nothing joinable yet")
        return

    def mean(a, b):
        v = [g[s] for s in range(a, b) if s in g]
        return (sum(v) / len(v), len(v) / (b - a)) if v else (None, 0.0)

    def spot(t):
        for kk in (t, t - 1, t - 2):
            if kk in g:
                return g[kk]
        return None

    def signal(w, L):
        """(side, ok) using only data available at C-L."""
        C = w + WINDOW
        k, ck = mean(w - NSEC, w)
        if k is None or ck < MIN_COVER:
            return None
        el = max(0, min(NSEC, (C - L) - (C - NSEC)))
        if el:
            km, cov = mean(C - NSEC, C - NSEC + el)
            if km is None or cov < MIN_COVER:
                return None
            known = km * el
        else:
            known = 0.0
        s = spot(C - L)
        if s is None:
            return None
        est = (known + (NSEC - el) * s) / NSEC
        gap = (est - k) / k * 1e4
        if abs(gap) < GAP_BPS:
            return None
        return "up" if gap > 0 else "down"

    print(f"{'entry':>6} {'bid':>6} {'sig':>5} {'filled':>7} {'fill%':>7} "
          f"{'@touch':>7} {'obs%':>6} {'EV c/sh':>8} {'$ total':>9} {'$/day':>8}")
    best = None
    for L in ENTRY_LEADS:
        for P in BIDS:
            sig = fills = wins = real = 0
            touch = 0
            pnl = 0.0
            obs = []
            for w, winner in sorted(won.items()):
                side = signal(w, L)
                if side is None:
                    continue
                sig += 1
                entry_ask = book.get((w, L, side), (None, None))[0]
                px = None
                if entry_ask is not None and entry_ask <= P:
                    px, t_flag = entry_ask, True      # crossable right away
                else:
                    t_flag = False
                    for l2 in [x for x in leads_seen if x < L]:
                        a2 = book.get((w, l2, side), (None, None))[0]
                        if a2 is not None and a2 <= P:
                            px = P                     # our resting bid is hit
                            break
                if px is None:
                    continue
                fills += 1
                touch += t_flag
                won_it = side == winner
                wins += won_it
                if t_flag:
                    continue          # crossable at entry = a TAKER trade,
                                      # already measured and already priced
                real += 1
                obs.append(won_it)
                pnl += (ACC.get((COIN, L), 0.0) - px) * CLIP
            if not sig or not real:
                continue
            ev = pnl / (real * CLIP)
            per_day = pnl * 24.0 / span_h if span_h else 0.0
            print(f"{L:>6} {P:>6.2f} {sig:>5} {real:>7} {100*real/sig:>6.0f}% "
                  f"{100*touch/fills:>6.0f}% {100*sum(obs)/len(obs):>5.0f}% "
                  f"{100*ev:>7.2f}c {pnl:>9.2f} {per_day:>8.2f}")
            if best is None or per_day > best[2]:
                best = (L, P, per_day, fills, 100 * ev)

    print("\n'filled' counts GENUINE resting fills only. @touch is the share of")
    print("raw matches that were already crossable at entry — those are taker")
    print("trades, already measured as break-even, and are EXCLUDED from EV.")
    print("EV uses measured accuracy; obs% is the realised rate of the filled")
    print("few, shown only so you can see how small it is.")
    print("Fill counts assume we win the queue at our price and that offers")
    print("only move at the 9 sampled instants — optimistic on the first,")
    print("pessimistic on the second. Order of magnitude only.")
    if best:
        print(f"\nbest cell: enter {best[0]}s out, bid {best[1]:.2f} -> "
              f"${best[2]:.2f}/day on {best[3]} fills at {best[4]:.2f}c/share")
        print("Before believing it: this is one calm week, and a resting bid")
        print("is exactly what informed sellers hunt. Paper it first.")


if __name__ == "__main__":
    main()
