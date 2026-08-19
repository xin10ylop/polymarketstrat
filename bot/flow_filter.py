"""Does confirming pre-open FLOW recover the backtest's 66% population?

  venv/bin/python -m bot.flow_filter
  COIN=eth venv/bin/python -m bot.flow_filter
  FAMILY=15m venv/bin/python -m bot.flow_filter

WHERE THE 66% WENT, AND WHY IT MIGHT BE RECOVERABLE. The 08-10 tape
backtest reported 66.9% settle for the tilt side — but to price an entry it
required at least one pre-open taker BUY on the tilt side, silently keeping
only windows where informed flow already agreed with the signal. The live
bot trades ALL gated windows and settles ~57-60%. The audit decomposition
put ~9.5pp of the gap on exactly this.

That is a selection bias in a backtest — and a candidate FILTER for a bot.
Pre-open trades are visible in real time on the CLOB feed, so if
"tilt + confirming flow" genuinely settles far above "tilt alone", the bot
can require confirmation and trade the 66% population on purpose:
fewer entries, each at the better rate. This tool measures whether that
conditional edge is real, on the FULL backfilled tape, split in time.

CLASSES, per gated window, from taker prints in [FLOW_FROM, 0) seconds
before the open (Up-space: BUY = buying Up, SELL = buying Down):
  confirm   net taker volume AGREES with the tilt side
  oppose    net taker volume is against it
  silent    no prints in the flow window at all
  flat      prints but net exactly zero

THE PRE-REGISTERED BAR, same as every act-on-it decision here: the confirm
class must beat the others in BOTH time halves, and the direction must hold
on btc AND eth, before any bot change is designed. One half or one coin is
the vol-gate shape — retracted twice — and it is not actionable.

Also reports the tilt-magnitude buckets crossed with flow, because the gate
("this basis point thing") and the flow filter are competing explanations
for the same conditional edge, and the cross-table shows which one carries
it. Read-only.
"""
import json
import os
import sqlite3

from bot.pnl_daily import breakeven, wilson
from bot.scalp_backtest import CACHE, tilt_at
from bot.twap_verify import COIN, DB_DIR, FAMILY

_GATES = {("btc", "5m"): 0.5, ("btc", "15m"): 1.0,
          ("eth", "5m"): 0.5, ("eth", "15m"): 1.7}
GATE = float(os.environ.get("GATE", _GATES.get((COIN, FAMILY), 1.0)))
FLOW_FROM = int(os.environ.get("FLOW_FROM", "-60"))   # start of flow window
PAID = float(os.environ.get("PAID", "0.5253"))        # measured real entry


def classify(prints, pick):
    """confirm | oppose | silent | flat from net taker volume pre-open."""
    conf = opp = 0.0
    n = 0
    for t, side, _q, sz in prints:
        if not (FLOW_FROM <= t < 0):
            continue
        n += 1
        agrees = (side == "BUY") == (pick == "up")
        if agrees:
            conf += sz
        else:
            opp += sz
    if n == 0:
        return "silent"
    if conf > opp:
        return "confirm"
    if opp > conf:
        return "oppose"
    return "flat"


def table(rows, title):
    print(f"\n{title}")
    print(f"{'class':>9} {'n':>5} {'settle%':>8} {'95% CI':>16} "
          f"{'edge vs paid':>13}")
    be = breakeven(PAID)
    out = {}
    for cls in ("confirm", "oppose", "silent", "flat"):
        sub = [r for r in rows if r[3] == cls]
        if len(sub) < 5:
            continue
        w = sum(1 for r in sub if r[1] == r[2])
        lo, hi = wilson(w, len(sub))
        out[cls] = w / len(sub)
        print(f"{cls:>9} {len(sub):>5} {100*w/len(sub):>7.1f}% "
              f"[{100*lo:>5.1f}, {100*hi:>5.1f}] "
              f"{100*(w/len(sub)-be):>+12.2f}c")
    return out


def main():
    gp = os.path.join(DB_DIR, f"{COIN}_1s.db")
    tp = os.path.join(CACHE, f"{COIN}_{FAMILY}_tape.db")
    for p in (gp, tp):
        if not os.path.exists(p):
            raise SystemExit(f"missing {p}")
    grid = dict(sqlite3.connect(f"file:{gp}?mode=ro", uri=True)
                .execute("SELECT ts, v FROM px"))
    rows = []
    for w, win, pr in sqlite3.connect(f"file:{tp}?mode=ro", uri=True).execute(
            "SELECT wts, winner, prints FROM tape WHERE winner IS NOT NULL "
            "ORDER BY wts"):
        t = tilt_at(grid, w)
        if not t or t[0] is None or abs(t[0]) < GATE:
            continue
        try:
            prints = json.loads(pr)
        except ValueError:
            continue
        rows.append((w, t[1], win, classify(prints, t[1]), abs(t[0])))
    print(f"{COIN} {FAMILY}: {len(rows)} gated windows on the full tape "
          f"(gate {GATE}bp, flow window [{FLOW_FROM}, 0)s, "
          f"break-even at the measured paid {PAID} = "
          f"{100*breakeven(PAID):.2f}%)")
    if len(rows) < 80:
        print("Too few windows; backfill the tape or wait.")
        return

    table(rows, "ALL GATED WINDOWS BY PRE-OPEN FLOW")

    half = len(rows) // 2
    a = table(rows[:half], f"FIRST HALF ({half})")
    b = table(rows[half:], f"SECOND HALF ({len(rows) - half})")

    print("\nTILT BUCKET x FLOW (settle% (n)) — which one carries the edge?")
    print(f"{'bucket':>10} {'confirm':>14} {'other':>14}")
    for lo_b, hi_b, lab in ((GATE, 1.0, f"{GATE}-1bp"), (1.0, 2.0, "1-2bp"),
                            (2.0, 99.0, "2bp+")):
        sub = [r for r in rows if lo_b <= r[4] < hi_b]
        cf = [r for r in sub if r[3] == "confirm"]
        ot = [r for r in sub if r[3] != "confirm"]
        def fmt(s):
            if len(s) < 5:
                return f"{'-':>9} ({len(s)})"
            w = sum(1 for r in s if r[1] == r[2])
            return f"{100*w/len(s):>8.1f}% ({len(s)})"
        if len(sub) >= 10:
            print(f"{lab:>10} {fmt(cf):>14} {fmt(ot):>14}")

    print("\nVERDICT RULE (pre-registered): actionable ONLY if `confirm`")
    print("beats every other class in BOTH halves here AND the same holds on")
    print("the other coin. Then the bot change is: require net confirming")
    print("flow in the final minute before entering — trading the backtest's")
    print("population on purpose. If `silent` or `oppose` matches `confirm`,")
    print("the 66% was era luck and the current all-windows design stands.")
    if a and b and "confirm" in a and "confirm" in b:
        oth_a = max((v for k, v in a.items() if k != "confirm"), default=0)
        oth_b = max((v for k, v in b.items() if k != "confirm"), default=0)
        if a["confirm"] > oth_a and b["confirm"] > oth_b:
            print("-> confirm leads in BOTH halves on this coin. Check the "
                  "other coin before acting.")
        else:
            print("-> confirm does NOT lead in both halves on this coin: "
                  "not actionable.")


if __name__ == "__main__":
    main()
