"""The feed-divergence trade: buy the side a spot-driven book is wrong about.

  venv/bin/python -m bot.divergence_report
  COIN=eth venv/bin/python -m bot.divergence_report

WHY THIS EXISTS. The pre-2026-08-07 edge was never speed or forecasting: at
knife-edge margins the order book priced the exchange TAPE's answer while
Polymarket settled on CHAINLINK, and those disagree ~44% of the time below
0.5bp. We were buying the resolver's answer at the tape's price. Rescoring
the old trades against the Binance close turns +$2,536 into -$2,082.

The rule change did not delete that discrepancy — it MOVED it. Settlement is
now a 30s Chainlink TWAP, but a participant estimating the outcome from spot
(the exact mistake this repo made and had to fix on 08-09) is systematically
wrong whenever spot and the TWAP point different ways. Measured on Aug 7-8,
that happens in ~6% of windows, and the TWAP side then wins:

    BTC  Aug-7 82.4%  Aug-8 64.7%  pooled 73.5% (n=34)
    ETH  Aug-7 92.0%  Aug-8 78.9%  pooled 86.4% (n=44)

Four of four day-asset cells above chance.

AND IT INVERTS THE LIQUIDITY PROBLEM. Every drought we measured was on the
side the book agrees is winning — quoted 13-27% of the time near the close.
In a divergence window the side we want is the one the book thinks is
LOSING, and that side is quoted 95-100% of the time throughout. The trade
wants exactly the inventory that is always there, at exactly the cheap
prices where 81% of the historical profit was made.

THIS FILE DOES NOT ASSUME ANY OF THAT. It measures what the book actually
charged for the TWAP side in divergence windows, and prices the trade
against the win rate measured on the WHOLE signal population, not on the
handful that filled. Read-only.
"""
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

from bot.twap_verify import COIN, FAMILY, NSEC, WINDOW, load_grid, official

LEADS = [int(x) for x in os.environ.get("LEADS", "60,45,30,20,10,6,3").split(",")]
CLIP = float(os.environ.get("CLIP", "250"))
BOOK_DIR = os.environ.get("BOOK_DIR", "bot/data/bookcal")
MIN_COVER = 0.9


def fee(p):
    return 0.07 * p * (1 - p)


def wilson(k, n):
    """95% interval — small n is the whole risk here, so never hide it."""
    if not n:
        return (0.0, 1.0)
    z, p = 1.96, k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, c - h), min(1.0, c + h))


def main():
    g = load_grid()
    path = os.path.join(BOOK_DIR, f"{COIN}_{FAMILY}_book.db")
    book, has_err = {}, False
    if os.path.exists(path):
        db = sqlite3.connect(path)
        has_err = "err" in {r[1] for r in db.execute("PRAGMA table_info(book)")}
        q = ("SELECT wts, lead, side, ask, ask_sz, err FROM book" if has_err
             else "SELECT wts, lead, side, ask, ask_sz, 0 FROM book")
        for wts, lead, side, ask, sz, err in db.execute(q):
            book[(wts, lead, side)] = (ask, sz, err)

    bk_lo = min((w for (w, _, _) in book), default=None)
    bk_hi = max((w for (w, _, _) in book), default=None)
    if bk_lo is not None:
        print(f"book covers {time.strftime('%m-%d %H:%M', time.gmtime(bk_lo))}"
              f" -> {time.strftime('%m-%d %H:%M', time.gmtime(bk_hi))} UTC "
              f"({(bk_hi-bk_lo)/3600:.1f}h); divergence is ~6% of windows, so "
              f"expect ~{0.06*(bk_hi-bk_lo)/WINDOW:.0f} priced samples so far.\n")
    lo, hi = min(g), max(g)
    wtss = [w for w in range(lo - lo % WINDOW, hi + WINDOW, WINDOW)
            if w - NSEC >= lo and w + WINDOW <= hi]
    with ThreadPoolExecutor(16) as ex:
        won = {w: r for w, r in ex.map(official, wtss) if r}
    span_h = (hi - lo) / 3600.0
    print(f"{COIN} {FAMILY}: {len(won)} settled windows, {span_h:.1f}h of grid"
          f"{'' if book else '  (NO BOOK DATA — signal only)'}\n")
    if not won:
        print("nothing settled yet")
        return

    def mean(a, b):
        v = [g[s] for s in range(a, b) if s in g]
        return (sum(v) / len(v), len(v) / (b - a)) if v else (None, 0.0)

    def spot(t):
        for kk in (t, t - 1, t - 2):
            if kk in g:
                return g[kk]
        return None

    print(f"{'lead':>5} {'diverge':>8} {'%win':>6} {'95% CI':>14} | "
          f"{'quoted':>7} {'avg ask':>8} {'EV c/sh':>8} {'$/trade':>8} {'$/day':>8}")
    for L in LEADS:
        rows = []
        for w, winner in sorted(won.items()):
            C = w + WINDOW
            k_tw, ck = mean(w - NSEC, w)
            if k_tw is None or ck < MIN_COVER:
                continue
            k_sp = spot(w)
            el = max(0, min(NSEC, (C - L) - (C - NSEC)))
            if el:
                km, cov = mean(C - NSEC, C - NSEC + el)
                if km is None or cov < MIN_COVER:
                    continue
                known = km * el
            else:
                known = 0.0
            s = spot(C - L)
            if s is None or k_sp is None:
                continue
            est = (known + (NSEC - el) * s) / NSEC
            twap_side = "up" if est >= k_tw else "down"
            spot_side = "up" if s >= k_sp else "down"
            if twap_side == spot_side:
                continue                      # no divergence, no trade
            ask, sz, err = book.get((w, L, twap_side), (None, None, 0))
            in_book = bk_lo is not None and bk_lo <= w <= bk_hi
            rows.append((twap_side == winner, ask, sz, err, in_book))
        if not rows:
            print(f"{L:>5} {0:>8}")
            continue
        n = len(rows)
        k = sum(r[0] for r in rows)
        p = k / n
        clo, chi = wilson(k, n)
        # priced subset: a failed fetch is unknown, NOT an absent quote
        pr = [r for r in rows if r[1] is not None and not r[3]]
        known_side = [r for r in rows if not r[3]]
        inb = sum(1 for r in rows if r[4])
        head = (f"{L:>5} {n:>8} {100*p:>5.0f}% "
                f"[{100*clo:>4.0f},{100*chi:>4.0f}]%")
        if not pr or not known_side:
            print(head + f" |   {inb}/{n} of these windows fall inside the "
                         f"book recording; {sum(1 for r in rows if r[1] is not None)} had a quote")
            continue
        avg = sum(r[1] for r in pr) / len(pr)
        # EV uses the win rate of the WHOLE divergence population, and its
        # lower confidence bound alongside — never the filled subset's own
        # outcome, which is the error that produced two retracted results.
        ev = p - avg - fee(avg)
        ev_lo = clo - avg - fee(avg)
        sh = sum(min(r[2] or 0, CLIP) for r in pr) / len(pr)
        per_trade = ev * sh
        per_day = per_trade * len(pr) * 24.0 / span_h if span_h else 0.0
        print(head + f" | {100*len(pr)/len(known_side):>6.0f}% {avg:>8.3f} "
              f"{100*ev:>7.2f}c {per_trade:>8.2f} {per_day:>8.2f}")
        print(f"{'':>5} {'':>8} {'':>6} {'':>14} |  at the 95% lower bound: "
              f"{100*ev_lo:>+6.2f}c/sh, {ev_lo*sh*len(pr)*24.0/span_h:>+8.2f}/day")

    print("\n'diverge' = windows where a spot view and the TWAP point different")
    print("ways; '%win' = how often the TWAP side actually won. 'quoted' is the")
    print("share of those where the TWAP side had a real ask (failed fetches are")
    print("excluded as unknown, not counted as absent).")
    print("EV = %win - ask - fee, priced on the FULL divergence population.")
    print("The lower-bound line is the one to trust: n is small and four of the")
    print("last five promising results on this project died out of sample.")


if __name__ == "__main__":
    main()
