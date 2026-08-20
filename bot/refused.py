"""Who wins: the windows we buy, or the ones the market prices out of
reach? The adverse-selection check on the entry ceiling.

  venv/bin/python -m bot.refused
  COIN=eth venv/bin/python -m bot.refused

WHY, 2026-08-20. In the RULE2 era btc 5m's entered decisions win ~44%
while the full gated tape settles ~60%. The bot only enters when the ask
is at or under PREOPEN_MAX_PX (0.56); under the new rule the book prices
the (now twice-as-large) tilts richly, so the ceiling refuses roughly
half of all gated windows. The ugly hypothesis: the makers have adapted
to the new rule, their ask now carries information, and a CHEAP ask on a
gated window means they doubt the signal — the ceiling that used to
protect against overpaying would then be hand-picking the losers for us.

WHAT IT MEASURES, on the full tape (bot state does not matter): every
gated settled window with a recorded T-3 book ask for the picked side,
split by whether that ask was over the ceiling ("rich" — the bot cannot
buy it) or at/under it ("cheap" — the bot's hunting ground), plus the
subset the bot actually entered. Win rates with Wilson intervals, mean
ask, edge at each class's OWN price, and time halves.

READING IT. rich winning far above cheap, in both halves, on both
coins, means the market's price is now the better predictor and the
ceiling anti-selects — a design problem no gate tweak fixes. cheap and
rich winning alike means the ceiling is a fair price guard and the bad
stretch is variance. The pre-registered bar for ACTING is the usual:
both halves and both coins. Read-only.
"""
import os
import sqlite3

from bot.launch_ev import DEFAULT_ERA, ERAS
from bot.pnl_daily import breakeven, wilson
from bot.scalp_backtest import CACHE, tilt_at
from bot.twap_verify import COIN, DB_DIR, FAMILY, WINDOW

BOOK_DIR = os.environ.get("BOOK_DIR", "bot/data/bookcal")
LEAD = 3
_GATES = {("btc", "5m"): 0.5, ("btc", "15m"): 1.0,
          ("eth", "5m"): 0.5, ("eth", "15m"): 1.7}
GATE = float(os.environ.get("GATE", _GATES.get((COIN, FAMILY), 1.0)))
CEIL = float(os.environ.get("CEIL", "0.56"))       # PREOPEN_MAX_PX
UNIT = f"preopen-{COIN}" + ("15" if FAMILY == "15m" else "")
SINCE = int(os.environ.get("SINCE", ERAS.get(UNIT, DEFAULT_ERA)))
MIN_N = int(os.environ.get("MIN_N", "10"))


def row(label, sub, px_key):
    if len(sub) < MIN_N:
        print(f"{label:>9}  n={len(sub)} — too few")
        return None
    w = sum(1 for r in sub if r[1] == r[2])
    lo, hi = wilson(w, len(sub))
    ask = sum(r[px_key] for r in sub) / len(sub)
    be = breakeven(ask)
    half = len(sub) // 2
    w1 = sum(1 for r in sub[:half] if r[1] == r[2])
    w2 = sum(1 for r in sub[half:] if r[1] == r[2])
    print(f"{label:>9} {len(sub):>5} {100*w/len(sub):>7.1f}% "
          f"[{100*lo:>5.1f}, {100*hi:>5.1f}] {ask:>7.4f} "
          f"{100*(w/len(sub)-be):>+8.2f} "
          f"{100*w1/max(1,half):>6.1f}% {100*w2/max(1,len(sub)-half):>6.1f}%")
    return w / len(sub), w1 / max(1, half), w2 / max(1, len(sub) - half)


def main():
    gp = os.path.join(DB_DIR, f"{COIN}_1s.db")
    tp = os.path.join(CACHE, f"{COIN}_{FAMILY}_tape.db")
    bp = os.path.join(BOOK_DIR, f"{COIN}_{FAMILY}_book.db")
    lp = os.path.join("bot/data", UNIT, "paper.db")
    for p in (gp, tp, bp):
        if not os.path.exists(p):
            raise SystemExit(f"missing {p}")
    grid = dict(sqlite3.connect(f"file:{gp}?mode=ro", uri=True)
                .execute("SELECT ts, v FROM px"))
    book = {(w, s): a for w, s, a in sqlite3.connect(
        f"file:{bp}?mode=ro", uri=True).execute(
        "SELECT wts, side, ask FROM book WHERE lead=? AND ask IS NOT NULL",
        (WINDOW + LEAD,))}
    entered = set()
    if os.path.exists(lp):
        entered = {w for (w,) in sqlite3.connect(
            f"file:{lp}?mode=ro", uri=True).execute(
            "SELECT DISTINCT wts FROM fills WHERE strategy='preopen'")}

    rows = []
    for w, win in sqlite3.connect(f"file:{tp}?mode=ro", uri=True).execute(
            "SELECT wts, winner FROM tape WHERE winner IS NOT NULL "
            "AND wts >= ? ORDER BY wts", (SINCE,)):
        t = tilt_at(grid, w)
        if not t or t[0] is None or abs(t[0]) < GATE:
            continue
        ask = book.get((w, t[1]))
        if ask is None:
            continue
        rows.append((w, t[1], win, ask))
    print(f"{COIN} {FAMILY}: {len(rows)} gated settled windows with a T-3 "
          f"book since {SINCE} (gate {GATE}bp, ceiling {CEIL})")
    if len(rows) < 3 * MIN_N:
        print("Too few windows; wait for tape/book to accumulate.")
        return
    cheap = [r for r in rows if r[3] <= CEIL + 1e-9]
    rich = [r for r in rows if r[3] > CEIL + 1e-9]
    ent = [r for r in rows if r[0] in entered]

    print(f"\n{'class':>9} {'n':>5} {'settle%':>8} {'95% CI':>15} "
          f"{'ask':>7} {'edge@ask':>8} {'h1%':>6} {'h2%':>6}")
    c = row("cheap", cheap, 3)
    r = row("rich", rich, 3)
    row("entered", ent, 3)
    print(f"\n('entered' is the bot-touched subset of 'cheap' — its ask "
          f"column shows the T-3 quote, not the swept fill price. "
          f"{len(ent)} of {len(cheap)} cheap windows were entered; the "
          f"gap is halts, gaps and books thinner than the min order.)")

    if c and r:
        print("\nVERDICT (pre-registered: act only if it holds in both")
        print("halves here AND on the other coin):")
        if r[1] > c[1] and r[2] > c[2]:
            print(f"-> RICH beats CHEAP in BOTH halves "
                  f"({100*r[1]:.1f}/{100*r[2]:.1f} vs "
                  f"{100*c[1]:.1f}/{100*c[2]:.1f}): the makers' price is "
                  f"the better predictor and the ceiling anti-selects. "
                  f"Check the other coin; if it agrees, this is a design "
                  f"problem, not variance.")
        else:
            print(f"-> no consistent rich-over-cheap split "
                  f"(rich {100*r[1]:.1f}/{100*r[2]:.1f}, cheap "
                  f"{100*c[1]:.1f}/{100*c[2]:.1f}): the ceiling is not "
                  f"provably anti-selecting; the bad stretch reads as "
                  f"variance so far.")


if __name__ == "__main__":
    main()
