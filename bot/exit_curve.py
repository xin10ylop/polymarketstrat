"""THE SCALP, priced properly: buy at T-3, rest a sell at +Xc, how often fills?

  venv/bin/python -m bot.exit_curve
  COIN=eth GATE=1.0 venv/bin/python -m bot.exit_curve

THIS IS THE STRATEGY AS SPECIFIED, which is not the one currently running.
The spec was: a few seconds before the open, buy one side; immediately rest a
limit sell at entry + 5c; the open reprices the book and the limit fills. The
deployed bot does the first half and then HOLDS TO SETTLEMENT instead, because
holding measured richer on a small sample and because simulating a resting
sell needs fill machinery the paper executor does not have.

That deviation is not a detail — the two versions take completely different
risk. The scalp is exposed for seconds and does not care who wins the window.
The hold is exposed for five minutes and cares about nothing else. Every
clustering result computed for the hold (correlated settlements, btc and eth
being one bet) describes a risk the scalp never takes.

So this prices the scalp from the book recordings, which already hold the ask
at T-3 and the BID at T+2, T+15 and T+30 on both sides.

THREE THINGS IT GETS RIGHT, each of which I have gotten wrong before:

  FEES ONCE, NOT TWICE. The entry is a marketable take and pays
  0.07*p*(1-p); the exit is a RESTING limit and pays nothing. At a 0.50 entry
  that is 1.75c in and 0c out, so a 5c exit clears +3.25c net.

  THE NO-FILL BUCKET IS NOT THE AVERAGE WINDOW. Windows where the limit fills
  are exactly the windows that moved our way, so what is left over settles
  WORSE than the unconditional rate. Pairing an unconditional win rate with a
  filtered population is the specific error that killed two earlier results
  here, so the leftover bucket is scored on its own outcomes.

  P(FILL) IS A LOWER BOUND. The recorder takes three snapshots after the open,
  so a price that touches the target between them is not counted. Every fill
  rate below understates the truth, which is the safe direction to be wrong.

Read-only. Never trades, touches no bot state, writes nothing.
"""
import os
import sqlite3

from bot.twap_verify import COIN, DB_DIR, FAMILY, NSEC, WINDOW, load_grid

LEAD = int(os.environ.get("LEAD", "3"))
GATE = float(os.environ.get("GATE", "0.5"))
COVER = float(os.environ.get("COVER", "0.75"))
POST = [int(x) for x in os.environ.get("POST", "2,15,30").split(",")]
EXITS = [float(x) for x in os.environ.get(
    "EXITS", "0.02,0.03,0.04,0.05,0.07,0.10").split(",")]
BOOK_DIR = os.environ.get("BOOK_DIR", "bot/data/bookcal")
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


def load_book():
    path = os.path.join(BOOK_DIR, f"{COIN}_{FAMILY}_book.db")
    if not os.path.exists(path):
        raise SystemExit(f"no book recording at {path}")
    db = sqlite3.connect(path)
    cols = {r[1] for r in db.execute("PRAGMA table_info(book)")}
    e = "err" if "err" in cols else "0"
    c = "COALESCE(ask_cum, ask_sz)" if "ask_cum" in cols else "ask_sz"
    return {(w, L, s): (a, b, z, x) for w, L, s, a, b, z, x in db.execute(
        f"SELECT wts, lead, side, ask, bid, {c}, {e} FROM book")}


def windows(g, book):
    """[(wts, pick, entry_ask, depth, {post: bid}, won)] — honest at T-3."""
    lo, hi = min(g), max(g)
    out = []
    for w in sorted({x for (x, L, _) in book if L > WINDOW}):
        if w - NSEC < lo or w + WINDOW > hi:
            continue
        # --- the call, carry-forward, using only what exists at T-LEAD
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
                break
            total += carry
        if carry is None or present < (NSEC - LEAD) * COVER:
            continue
        spot = next((g[s] for s in range(w - LEAD, w - LEAD - 4, -1)
                     if s in g), None)
        if spot is None:
            continue
        k = (total + LEAD * spot) / NSEC
        if k <= 0:
            continue
        tilt = (spot - k) / k * 1e4
        if abs(tilt) < GATE:
            continue
        pick = "up" if tilt >= 0 else "down"
        ent = book.get((w, WINDOW + LEAD, pick))
        if not ent or ent[3] or ent[0] is None:
            continue
        bids = {}
        for p_ in POST:
            r = book.get((w, WINDOW - p_, pick))
            bids[p_] = None if (not r or r[3] or r[1] is None) else r[1]
        if all(v is None for v in bids.values()):
            continue
        # --- self-settle from the grid (twap_verify scored this at 100%)
        def twap(end):
            v = [g[s] for s in range(end - NSEC, end) if s in g]
            return (sum(v) / len(v), len(v) / NSEC) if v else (None, 0.0)
        o, co = twap(w)
        c, cc = twap(w + WINDOW)
        if o is None or c is None or min(co, cc) < 0.9:
            continue
        out.append((w, pick, ent[0], ent[2] or 0.0, bids,
                    (("up" if c >= o else "down") == pick)))
    return out


def main():
    rows = windows(load_grid(), load_book())
    if len(rows) < 5:
        raise SystemExit(f"only {len(rows)} joinable windows — needs book, "
                         "grid and pre-open rows to overlap")
    span_h = (rows[-1][0] - rows[0][0] + WINDOW) / 3600.0
    ask = sum(r[2] for r in rows) / len(rows)
    held = sum(r[5] for r in rows)
    hold_ev = held / len(rows) - ask - fee(ask)
    print(f"{COIN} {FAMILY}: {len(rows)} windows with pre-open book + grid + "
          f"outcome over {span_h:.1f}h, |tilt| >= {GATE}bp")
    print(f"average entry {ask:.4f}, entry fee {100*fee(ask):.2f}c, "
          f"settles our way {100*held/len(rows):.1f}%")
    print(f"HOLD TO SETTLEMENT (what is running now): "
          f"{100*hold_ev:+.2f}c/share\n")

    print("THE SCALP — rest a sell at entry + X, give up at T+H and hold")
    print(f"{'exit':>5} {'by':>5} {'n':>5} {'fill%':>7} {'if filled':>10} "
          f"{'leftover win%':>14} {'blend c/sh':>11} {'lo':>7} {'$/day':>9}")
    best = []
    for x in EXITS:
        for h in POST:
            sel = [r for r in rows if r[4].get(h) is not None]
            if len(sel) < 5:
                continue
            # a resting sell fills once the BID reaches the target; using the
            # bid rather than the ask is the conservative test
            hit = {r[0] for r in sel if max(
                (v for p_, v in r[4].items() if p_ <= h and v is not None),
                default=-1) >= r[2] + x - 1e-9}
            filled = [r for r in sel if r[0] in hit]
            left = [r for r in sel if r[0] not in hit]
            nf = len(filled)
            # exit is a maker fill: no fee on the way out
            gain = x - fee(ask)
            lw = sum(r[5] for r in left)
            lev = ((lw / len(left)) - ask - fee(ask)) if left else 0.0
            blend = (nf * gain + len(left) * lev) / len(sel)
            # lower bound: worst case on BOTH the fill rate and the leftover
            flo, _ = wilson(nf, len(sel))
            llo, _ = wilson(lw, len(left)) if left else (0.0, 1.0)
            lev_lo = (llo - ask - fee(ask)) if left else 0.0
            blo = flo * gain + (1 - flo) * min(lev_lo, lev)
            sh = min(CLIP, sum(r[3] for r in sel) / len(sel))
            per_day = blend * sh * len(sel) * 24.0 / span_h if span_h else 0.0
            print(f"{100*x:>4.0f}c {f'T+{h}':>5} {len(sel):>5} "
                  f"{100*nf/len(sel):>6.1f}% {100*gain:>+9.2f}c "
                  f"{(100*lw/len(left) if left else float('nan')):>13.1f}% "
                  f"{100*blend:>+10.2f}c {100*blo:>+6.2f}c {per_day:>9.0f}")
            best.append((blend, blo, x, h, len(sel), 100 * nf / len(sel)))

    if not best:
        print("  (no priced windows yet)")
        return
    # A row that almost never fills IS the hold wearing a different label —
    # its blend is the hold EV by construction. Only rows that actually scalp
    # a meaningful share of windows can be called the better strategy.
    real = [z for z in best if z[5] >= 10.0]
    if not real:
        print("\nNo exit level fills often enough to be a scalp at all — every")
        print("row here is the hold in disguise. The book does not reprice far")
        print("enough, fast enough, to sell into.")
        return
    real.sort(key=lambda z: -z[0])
    b = real[0]
    print(f"\nBEST SCALP: exit +{100*b[2]:.0f}c by T+{b[3]} -> "
          f"{100*b[0]:+.2f}c/share (lower bound {100*b[1]:+.2f}c), "
          f"filling {b[5]:.0f}% of {b[4]} windows")
    print(f"HOLD TO SETTLEMENT for the same windows: {100*hold_ev:+.2f}c/share")
    d = b[0] - hold_ev
    print(f"=> the scalp is {abs(100*d):.2f}c/share "
          f"{'BETTER' if d > 0 else 'WORSE'} than holding.")
    if d > 0:
        print("   It also carries the risk for seconds instead of five")
        print("   minutes, which is worth more than the cents: the settlement")
        print("   correlation between coins and across windows stops applying.")
    print("\nCAVEATS THAT DECIDE WHETHER THIS IS REAL.")
    print("  - fill% is a LOWER bound: three snapshots per window, so a touch")
    print("    between them is not seen. The true rate is higher.")
    print("  - it assumes the resting sell is filled at our price with no")
    print("    queue position modelled. On a book this deep that is the")
    print("    optimistic half of the assumption and it needs a live test.")
    print("  - the leftover column is scored on ITS OWN outcomes, not on the")
    print("    unconditional rate, because filled windows are exactly the ones")
    print("    that moved our way and what is left settles worse.")


if __name__ == "__main__":
    main()
