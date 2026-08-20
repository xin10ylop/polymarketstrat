"""Sell at 0.95 instead of holding to resolution? The exit-ladder replay.

  venv/bin/python -m bot.exit_ladder
  COIN=eth venv/bin/python -m bot.exit_ladder
  LEVELS=0.85,0.90,0.95 venv/bin/python -m bot.exit_ladder

THE ECONOMICS, before any data. A token sold at 0.95 gives up the last
5c on true winners to dodge the losers that got to 0.95 and died. If the
market is well calibrated, the price IS the survival probability, so the
trade is EV-neutral by construction: selling wins only where tokens that
touch L go on to lose MORE often than (1-L). The one structural sweetener
is that a resting sell is a MAKER order — no taker fee, plus the 20%
rebate (not counted here; favors selling) — and, for LIVE trading, a sale
returns collateral through the order book instantly while a held winner
waits on the on-chain redemption step (launch blocker #1). So even an
EV-neutral exit can be architecturally valuable live. This tool measures
which case we are in, per decision, from data that already exists.

HOW A FILL IS DECIDED. The bot's own post-open tracking stops at 90s, but
certainty arrives in the LAST minute — so fills come from the TAPE, which
holds every print of the whole window. A resting sell of our side at L
has provably filled when a taker print goes THROUGH it (beyond L on our
side); a print exactly AT L is queue-dependent and reported separately as
the optimistic bound. Up-space both ways: selling UP at L fills on a
taker BUY at q>L; selling DOWN at L fills on a taker SELL at q<1-L.

WHAT DECIDES IT. Per settled decision (per-unit clean eras from
launch_ev): exit pnl = filled ? (L - paid) - entry fee : the real
hold-to-settle pnl. The `win|touch` column is the calibration read
directly: among windows that touched L, how often holding still won —
above L means holding those is worth more than L, below means less.

THE RULES, pre-registered. PAPER NEVER ADOPTS AN EXIT: holding records
the full outcome and this replay prices every exit for free, while a sold
position stops recording (same principle as the shadow stop). For LIVE:
sell-at-L is viable if its EV is not materially below hold in BOTH halves
on BOTH coins — the bar is "not worse", not "better", because the
redemption bypass is a real structural benefit. Read-only.
"""
import json
import os
import sqlite3

from bot.launch_ev import DEFAULT_ERA, ERAS
from bot.scalp_backtest import CACHE
from bot.twap_verify import COIN, FAMILY, WINDOW

LEVELS = [float(x) for x in
          os.environ.get("LEVELS", "0.85,0.90,0.95").split(",")]
MIN_N = int(os.environ.get("MIN_N", "20"))
UNIT = f"preopen-{COIN}" + ("15" if FAMILY == "15m" else "")
ERA = int(os.environ.get("ERA", ERAS.get(UNIT, DEFAULT_ERA)))


def touched(prints, side, level, strict):
    """Did the tape trade through (strict) or at (not strict) a resting
    sell of OUR side at `level` during the window?"""
    for t, pside, q, _sz in prints:
        if not (0 <= t < WINDOW):
            continue
        if side == "up" and pside == "BUY" and (
                q > level + 1e-9 if strict else q >= level - 1e-9):
            return True
        if side == "down" and pside == "SELL" and (
                q < 1.0 - level - 1e-9 if strict else q <= 1.0 - level + 1e-9):
            return True
    return False


def main():
    lp = os.path.join("bot/data", UNIT, "paper.db")
    tp = os.path.join(CACHE, f"{COIN}_{FAMILY}_tape.db")
    for p in (lp, tp):
        if not os.path.exists(p):
            raise SystemExit(f"missing {p}")
    db = sqlite3.connect(f"file:{lp}?mode=ro", uri=True)
    sides = {}
    for (d,) in db.execute(
            "SELECT detail FROM events WHERE kind='preopen_entry'"):
        try:
            j = json.loads(d)
            sides[j["w"]] = j["side"]
        except (ValueError, KeyError):
            continue
    decs = db.execute(
        "SELECT f.wts, SUM(f.price*f.size), SUM(f.size), SUM(f.fee), "
        "SUM(f.pnl) FROM fills f JOIN settlements s ON f.wts = s.wts "
        "WHERE f.pnl IS NOT NULL AND f.strategy='preopen' "
        "AND s.winner IS NOT NULL AND s.mismatch=0 "
        "GROUP BY f.wts HAVING SUM(f.size) >= 5 AND MAX(f.ts) >= ? "
        "ORDER BY MAX(f.ts)", (ERA,)).fetchall()
    tape = {}
    for w, pr in sqlite3.connect(f"file:{tp}?mode=ro", uri=True).execute(
            "SELECT wts, prints FROM tape WHERE winner IS NOT NULL"):
        try:
            tape[w] = json.loads(pr)
        except ValueError:
            continue

    rows, skipped = [], 0
    for w, cost, sz, fee, pnl in decs:
        side = sides.get(w)
        if side is None or w not in tape or not tape[w]:
            skipped += 1
            continue
        rows.append((w, side, cost / sz, sz, fee, 100 * pnl / sz, tape[w]))
    print(f"{UNIT}: {len(rows)} settled decisions with tape coverage "
          f"({skipped} without), era >= {ERA}")
    if len(rows) < MIN_N:
        print("Too few to conclude anything; collect more days first.")
        return
    hold = [r[5] for r in rows]
    m_hold = sum(hold) / len(hold)
    half = len(rows) // 2
    print(f"HOLD baseline: {m_hold:+.2f}c/share per decision "
          f"(what the fleet actually does)\n")
    print(f"{'level':>6} {'rule':>9} {'fill%':>6} {'win|touch':>10} "
          f"{'EV exit':>8} {'vs hold':>8} {'h1 d':>7} {'h2 d':>7}")
    for lv in LEVELS:
        for strict, lab in ((True, "through"), (False, "at-level")):
            evs, hit_w, hit_n = [], 0, 0
            for w, side, paid, sz, fee, hold_c, prints in rows:
                if touched(prints, side, lv, strict):
                    hit_n += 1
                    if hold_c > 0:
                        hit_w += 1
                    evs.append(100 * ((lv - paid) * sz - fee) / sz)
                else:
                    evs.append(hold_c)
            if not hit_n:
                continue
            m = sum(evs) / len(evs)
            d1 = (sum(evs[:half]) - sum(hold[:half])) / half
            d2 = (sum(evs[half:]) - sum(hold[half:])) / (len(rows) - half)
            print(f"{lv:>6} {lab:>9} {100*hit_n/len(rows):>5.1f}% "
                  f"{100*hit_w/hit_n:>9.1f}% {m:>+8.2f} {m-m_hold:>+8.2f} "
                  f"{d1:>+7.2f} {d2:>+7.2f}")

    print("\nHOW TO READ IT. win|touch ABOVE the level means the tokens")
    print("that reach it are worth MORE held than sold there (market")
    print("underprices near-certainty); below means selling captures value.")
    print("The 20% maker rebate is NOT counted — real selling is slightly")
    print("better than shown; 'through' is the certain-fill rule, 'at-level'")
    print("the optimistic bound. PAPER NEVER ADOPTS AN EXIT (holding records")
    print("the full outcome; this replay prices any exit for free). For")
    print("LIVE the bar is 'not materially worse than hold' in both halves")
    print("on both coins — a sale recycles collateral through the book and")
    print("skips the on-chain redemption a held winner requires.")


if __name__ == "__main__":
    main()
