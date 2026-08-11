"""What is PREOPEN_MAX_PX costing us? Sweep the entry ceiling on real fills.

  venv/bin/python -m bot.entry_ceiling

WHAT PROVOKED THIS. The four bots' average entry prices, and their results,
rank together almost perfectly:

    btc 15m  entry 0.5061  break-even 52.35%  ->  61.9%
    btc 5m   entry 0.5244  break-even 54.19%  ->  56.2%
    eth 5m   entry 0.5338  break-even 55.12%  ->  42.0%
    eth 15m  entry 0.5471  break-even 56.45%  ->  55.6%

The bar moves 52.35% -> 56.45% across the fleet — a four-point swing, larger
than any edge being measured. The strategy's own docstring says the pre-open
book is "flat and symmetric, ~0.50 a side"; we are paying up to 0.5471.

THE ARITHMETIC THAT MAKES THIS URGENT. Fee is 0.07*p*(1-p), so break-even is
p + 0.07*p*(1-p):

    entry 0.50 -> 51.75%      entry 0.54 -> 55.74%
    entry 0.52 -> 53.75%      entry 0.56 -> 57.73%

PREOPEN_MAX_PX is 0.56 on every unit. A fill at the ceiling needs 57.73% to
break even, which is ABOVE the best settle rate this strategy has ever
measured (60.4% at the gate, and that was the whole-sample figure, not the
marginal one). The ceiling is admitting trades that cannot pay.

WHY THIS IS A LEGITIMATE BACKTEST AND NOT CURVE-FITTING. The entry price is
observable BEFORE the trade — the bot already reads best_ask and compares it
to the ceiling. Dropping fills above a tighter ceiling is therefore a
question we could have answered in advance, not a selection on the outcome.
Sweeping DOWN from 0.56 is valid; there is no data above it, so the sweep
cannot go up.

STILL GUARDED. Eight ceilings against one outcome is a small search, and this
project has retracted two findings for less. The best cell is chosen on the
first half of each ledger's fills and reported on the second.

Read-only.
"""
import glob
import math
import os
import sqlite3

from bot.pnl_daily import breakeven, wilson

CEILINGS = (0.50, 0.51, 0.52, 0.53, 0.54, 0.55, 0.56)
CURRENT = float(os.environ.get("PREOPEN_MAX_PX", 0.56))


def score(fills, ceiling):
    """(n, win%, avg entry, break-even, edge_pp, lower bound on edge, $)."""
    sub = [f for f in fills if f[1] <= ceiling + 1e-9]
    n = len(sub)
    if not n:
        return None
    wins = sum(1 for f in sub if f[3] > 0)
    sz = sum(f[2] for f in sub)
    px = sum(f[1] * f[2] for f in sub) / sz if sz else 0.5
    be = breakeven(px)
    wr = wins / n
    lo, _ = wilson(wins, n)
    return (n, wr, px, be, (wr - be) * 100, (lo - be) * 100,
            sum(f[3] for f in sub), sz)


def report(name, fills):
    print(f"=== {name} — {len(fills)} settled fills ===")
    print(f"{'ceiling':>8} {'kept':>6} {'%kept':>6} {'win%':>6} {'entry':>7} "
          f"{'b/e%':>6} {'edge¢':>7} {'95% lo':>8} {'P&L $':>10}")
    for c in CEILINGS:
        r = score(fills, c)
        if r is None:
            continue
        n, wr, px, be, edge, lo, pnl, _ = r
        mark = "  <- current" if abs(c - CURRENT) < 1e-9 else ""
        print(f"{c:>8.2f} {n:>6} {100*n/len(fills):>5.0f}% {100*wr:>5.1f}% "
              f"{px:>7.4f} {100*be:>5.2f}% {edge:>+7.2f} {lo:>+8.2f} "
              f"{pnl:>+10.2f}{mark}")

    # OUT OF SAMPLE. A ceiling picked on the whole history is a ceiling picked
    # with hindsight; the split is what separates a real effect from the best
    # of eight draws.
    half = len(fills) // 2
    a, b = fills[:half], fills[half:]
    if half < 15:
        print("  (too few fills to split; treat the table as exploratory)\n")
        return
    best, best_edge = None, None
    for c in CEILINGS:
        r = score(a, c)
        if r and r[0] >= 8 and (best_edge is None or r[4] > best_edge):
            best, best_edge = c, r[4]
    cur = score(b, CURRENT)
    new = score(b, best) if best else None
    if best is None or cur is None or new is None:
        print()
        return
    print(f"\n  first half picks {best:.2f} (edge {best_edge:+.2f}¢). "
          f"On the second half:")
    print(f"    ceiling {CURRENT:.2f}: n={cur[0]:>3} edge {cur[4]:+.2f}¢  "
          f"P&L {cur[6]:+.2f}")
    if best != CURRENT:
        print(f"    ceiling {best:.2f}: n={new[0]:>3} edge {new[4]:+.2f}¢  "
              f"P&L {new[6]:+.2f}")
    if best == CURRENT:
        print("    the current ceiling is already the best cell in sample.")
        # THE COMPARISON THAT MATTERS WHEN NOTHING BEATS THE INCUMBENT is
        # not which ceiling won, it is whether the incumbent's own edge
        # SURVIVED. btc 15m picked 0.56 at +21.45c in sample and delivered
        # -1.99c out of it; printing only "current wins" would have hidden
        # the collapse behind a reassuring sentence.
        drop = cur[4] - best_edge
        print(f"    but its in-sample edge was {best_edge:+.2f}¢ and out of "
              f"sample it is {cur[4]:+.2f}¢ ({drop:+.2f}¢).")
        if drop < -3.0:
            print("    THAT IS A COLLAPSE, not a ceiling question. The first")
            print("    half was a good draw; the second is the strategy.")
    elif new[4] > cur[4]:
        print("    the tighter ceiling holds out of sample. Worth acting on")
        print("    once the other bots agree — a real effect is shared.")
    else:
        print("    it does NOT hold out of sample. That is a fitted cell.")
    print()


def main():
    paths = sorted(glob.glob("bot/data/preopen-*/paper.db"))
    if not paths:
        raise SystemExit("no pre-open ledgers")
    print("break-even by entry price (fee = 0.07*p*(1-p)):")
    print("  " + "   ".join(f"{p:.2f}->{100*breakeven(p):.2f}%"
                            for p in (0.50, 0.52, 0.54, 0.56)))
    print("A fill at the 0.56 ceiling needs 57.73% to break even.\n")
    for path in paths:
        name = os.path.basename(os.path.dirname(path))
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            fills = db.execute(
                "SELECT ts, price, size, pnl FROM fills "
                "WHERE pnl IS NOT NULL ORDER BY ts").fetchall()
        except sqlite3.Error as e:
            print(f"{name}: unreadable ({str(e)[:40]})\n")
            continue
        if not fills:
            print(f"{name}: no settled fills yet\n")
            continue
        report(name, fills)
    print("edge¢ is cents per share: win_rate - break_even, which IS the")
    print("expected value of a share. '95% lo' is its lower bound — a ceiling")
    print("is only worth trading where that is positive.")


if __name__ == "__main__":
    main()
