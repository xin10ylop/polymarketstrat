"""Would a RESTING BID at T-3 beat taking the ask? The maker-entry probe.

  venv/bin/python -m bot.maker_probe
  COIN=eth venv/bin/python -m bot.maker_probe

WHY THIS IS THE LARGEST LEVER ON THE TABLE. Verified from the venue's own
fee schedule (2026-08-13): takerOnly=true, rate 0.07, maker rebate 20%.
Every entry today TAKES: ~1.75c fee + 1.5-2.4c measured slippage on a ~3-5c
edge. A resting bid pays neither and would additionally earn rebates (not
counted here — conservative). The cost is fill risk, and the danger is
ADVERSE SELECTION: a bid fills when someone sells INTO our side, and that
seller may know something. This tool measures both from data that already
exists — no bot changes, no fill model.

HOW A FILL IS DECIDED, conservatively. The tape normalises every print to
Up-space and keeps the taker's side. A resting Up bid at B has provably
filled only when the market TRADES THROUGH it — a taker SELL print strictly
below B (price priority means our level was cleared first). For a Down bid
at B_d the same event is a taker BUY print strictly above 1-B_d. Prints AT
the level are queue-dependent and are counted separately as the optimistic
bound. Fills are tested over three cancel horizons: pre-open only [T-3,0),
and stale-bid variants to T+5 and T+15 — the post-open fills are where
adverse selection should live if it lives anywhere.

WHAT DECIDES IT. Per gated window: maker EV = fill * (settle - B) * 100,
zero fee, unfilled = 0. Taker baseline on the SAME windows: win% - ask -
fee(ask) at the same T-3 book. win|filled vs win|unfilled is the adverse-
selection tell. Time-split halves, per the house discipline. Read-only.
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
GATE = float(os.environ.get("GATE", _GATES.get((COIN, FAMILY), 1.0)))
HORIZONS = (0, 5, 15)           # cancel at open, T+5, T+15


def filled_at(prints, pick, level, until, strict=True):
    """Did the tape trade THROUGH a resting bid at `level` before t=until?

    Up-space: our Up bid at B is cleared by a taker SELL strictly below B;
    our Down bid at B_d (= Up level 1-B_d) by a taker BUY strictly above it.
    """
    for t, side, q, _sz in prints:
        if t >= until:
            break
        if t < -LEAD:
            continue
        if pick == "up" and side == "SELL" and (
                q < level if strict else q <= level):
            return True
        if pick == "down" and side == "BUY" and (
                q > 1.0 - level if strict else q >= 1.0 - level):
            return True
    return False


def stats(evs):
    n = len(evs)
    m = sum(evs) / n if n else 0.0
    return n, m


def main():
    gp = os.path.join(DB_DIR, f"{COIN}_1s.db")
    bp = os.path.join(BOOK_DIR, f"{COIN}_{FAMILY}_book.db")
    tp = os.path.join(CACHE, f"{COIN}_{FAMILY}_tape.db")
    for p in (gp, bp, tp):
        if not os.path.exists(p):
            raise SystemExit(f"missing {p}")
    import json
    grid = dict(sqlite3.connect(f"file:{gp}?mode=ro", uri=True)
                .execute("SELECT ts, v FROM px"))
    book = {(w, s): (a, b) for w, s, a, b in sqlite3.connect(
        f"file:{bp}?mode=ro", uri=True).execute(
        "SELECT wts, side, ask, bid FROM book WHERE lead=?",
        (WINDOW + LEAD,))}
    tape = {}
    for w, win, pr in sqlite3.connect(f"file:{tp}?mode=ro", uri=True).execute(
            "SELECT wts, winner, prints FROM tape WHERE winner IS NOT NULL"):
        try:
            tape[w] = (win, json.loads(pr))
        except ValueError:
            continue

    rows = []
    for w, (winner, prints) in sorted(tape.items()):
        t = tilt_at(grid, w)
        if not t or t[0] is None or abs(t[0]) < GATE:
            continue
        pick = t[1]
        q = book.get((w, pick))
        if not q or q[0] is None or q[1] is None:
            continue
        ask, bid = q
        if bid <= 0 or ask > 0.99:
            continue
        rows.append((w, pick, winner, ask, bid, prints))
    print(f"{COIN} {FAMILY}: {len(rows)} gated windows with tape + both-sided "
          f"T-{LEAD} book (gate {GATE}bp)")
    if len(rows) < 40:
        print("Too few to conclude anything; collect more days first.")
        return

    # taker baseline on the SAME windows, slippage-free (flattering to the
    # taker: if the maker beats even this, the verdict needs no fill model)
    wins_all = sum(1 for _, p, win, *_ in rows if p == win)
    ask_bar = sum(r[3] for r in rows) / len(rows)
    taker_ev = 100 * (wins_all / len(rows) - ask_bar - 0.07 * ask_bar
                      * (1 - ask_bar))
    lo_t, _ = wilson(wins_all, len(rows))
    print(f"\nTAKER baseline (same windows, ZERO slippage — flattering): "
          f"{100*wins_all/len(rows):.1f}% at ask {ask_bar:.4f} "
          f"-> {taker_ev:+.2f}c/share per window "
          f"[wilson lo {100*lo_t:.1f}% vs b/e {100*breakeven(ask_bar):.2f}%]")
    print("Real takers also pay 1.5-2.4c measured slippage on top.\n")

    print(f"{'cancel':>8} {'rule':>10} {'fill%':>6} {'win|fill':>9} "
          f"{'win|no':>7} {'EV/window¢':>11} {'vs taker':>9}")
    for until in HORIZONS:
        for strict, lab in ((True, "through"), (False, "at-level")):
            f_rows = [(r, filled_at(r[5], r[1], r[4], until, strict))
                      for r in rows]
            filled = [r for r, f in f_rows if f]
            unfilled = [r for r, f in f_rows if not f]
            if not filled:
                continue
            wf = sum(1 for w_, p, win, *_ in filled if p == win)
            wu = sum(1 for w_, p, win, *_ in unfilled if p == win)
            # maker pays the BID, no fee; unfilled windows earn zero
            ev = 100 * sum((1.0 if p == win else 0.0) - bid
                           for w_, p, win, a, bid, _pr in filled) / len(rows)
            print(f"{f'T+{until}':>8} {lab:>10} "
                  f"{100*len(filled)/len(rows):>5.1f}% "
                  f"{100*wf/max(1,len(filled)):>8.1f}% "
                  f"{100*wu/max(1,len(unfilled)):>6.1f}% "
                  f"{ev:>+11.2f} {ev-taker_ev:>+9.2f}")

    # the adverse-selection tell and the time split, on the headline policy
    # (conservative fills, cancel at the open)
    half = len(rows) // 2
    print("\nOUT-OF-SAMPLE (conservative rule, cancel at open):")
    for name, sub in (("first half", rows[:half]), ("second half",
                                                    rows[half:])):
        f_rows = [(r, filled_at(r[5], r[1], r[4], 0, True)) for r in sub]
        filled = [r for r, f in f_rows if f]
        if not filled or not sub:
            print(f"  {name}: no fills")
            continue
        wf = sum(1 for w_, p, win, *_ in filled if p == win)
        ev = 100 * sum((1.0 if p == win else 0.0) - b
                       for w_, p, win, a, b, _pr in filled) / len(sub)
        print(f"  {name}: fill {100*len(filled)/len(sub):.1f}%, "
              f"win|fill {100*wf/len(filled):.1f}%, EV {ev:+.2f}c/window")

    print("\nHOW TO READ IT. 'win|fill' far below 'win|no' is adverse")
    print("selection — the bid fills exactly when the flow disagrees with")
    print("the tilt — and kills the idea regardless of the fee saved. The")
    print("EV/window column already nets that cost; compare it to the")
    print("(flattering) taker baseline. Post-open horizons show what a")
    print("STALE bid suffers; the pre-open row is the honest policy. The")
    print("20% maker rebate is NOT counted — real maker EV is slightly")
    print("better than shown.")


if __name__ == "__main__":
    main()
