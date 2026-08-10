"""Did the LIVE bot compute the same tilt the backtest does? Logic vs timing.

  venv/bin/python -m bot.tilt_parity
  DATA=bot/data/preopen-eth COIN=eth venv/bin/python -m bot.tilt_parity

THE QUESTION. Live btc is settling 6/11 = 55% where the tape backtest says
66.7%. Eleven positions cannot distinguish those (the 95% interval on 6-of-11
is [28%, 79%]), so the sample size is a sufficient explanation — but only if
the two are computing the SAME NUMBER. If the live bot's tilt differs from the
backtest's, the gap is a defect and no amount of waiting fixes it.

WHY THEY COULD DIFFER EVEN WITH IDENTICAL CODE. The bot evaluates at wall
clock T-3.6..T-3.0 using whatever its websocket has DELIVERED by then. The
backtest reads bot/data/twapcal, written by a separate recorder process, after
the fact and complete. Two consequences:

  - the bot's `spot` can be several seconds staler. price_at falls back up to
    3 seconds, so a bot that has only received through T-6 prices the tilt off
    a T-6 print while the backtest uses T-3. Sign agreement at T-3 is 98.9%
    and at T-10 it is 82.6%, so that staleness is not free.
  - the bot's coverage of the strike window is lower, which changes both the
    strike itself and whether the 0.75 floor refuses the window at all.

Neither is a coding error. Both would show up as a worse live win rate, and
both are fixable — unlike sample size, which is only curable by waiting.

HOW THIS TESTS IT. Rather than reimplementing the maths and comparing two
reimplementations, this loads the archive into a real Oracle and calls the
REAL PreopenStrategy._tilt — the exact function the bot ran — then compares
its answer to the tilt the bot LOGGED for that same window at the time. Any
difference is therefore data (what had arrived), never implementation.

  same sign, small gap   -> logic is identical, the 55% is sample size
  differing signs        -> the bot is trading a different signal than the
                            one the backtest priced, and that is the bug

Read-only.
"""
import json
import os
import sqlite3
import statistics as st

from bot.config import CFG
from bot.feeds.oracle import Oracle
from bot.strategies.preopen import PreopenStrategy
from bot.twap_verify import COIN, DB_DIR, NSEC

DATA = os.environ.get("DATA", "bot/data/preopen-btc")


def main():
    path = os.path.join(DATA, "paper.db")
    if not os.path.exists(path):
        raise SystemExit(f"no ledger at {path}")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    live = []
    for (d,) in db.execute("SELECT detail FROM events WHERE kind=?",
                           ("preopen_entry",)):
        try:
            j = json.loads(d)
        except ValueError:
            continue
        if "w" in j and "tilt" in j:
            live.append(j)
    if not live:
        raise SystemExit("no preopen_entry events yet")

    gpath = os.path.join(DB_DIR, f"{COIN}_1s.db")
    if not os.path.exists(gpath):
        raise SystemExit(f"no grid archive at {gpath}")
    grid = dict(sqlite3.connect(gpath).execute("SELECT ts, v FROM px"))

    # the REAL production code path, fed the archive instead of a socket
    o = Oracle(CFG)
    o.samples = grid
    o.last_sample_s = max(grid) if grid else 0
    strat = PreopenStrategy(CFG, None, o, None, None, None, None)

    lead = CFG.preopen_lead_s
    print(f"{COIN}: {len(live)} live entries vs the archive, "
          f"lead {lead:g}s, gate {CFG.preopen_tilt_min_bp}bp, "
          f"coverage floor {CFG.preopen_min_coverage}\n")
    print(f"{'window':>12} {'live tilt':>10} {'archive':>10} {'diff':>8} "
          f"{'sign':>6} {'live side':>10}")
    diffs, flips, missing = [], 0, 0
    for j in sorted(live, key=lambda x: x["w"]):
        w = int(j["w"])
        lt = float(j["tilt"])
        r = strat._tilt(w, lead)
        if r is None:
            missing += 1
            print(f"{w:>12} {lt:>+9.2f}b {'refused':>10} {'':>8} {'':>6} "
                  f"{j.get('side',''):>10}")
            continue
        at = r[0]
        d = at - lt
        same = (at >= 0) == (lt >= 0)
        flips += (not same)
        diffs.append(abs(d))
        print(f"{w:>12} {lt:>+9.2f}b {at:>+9.2f}b {d:>+8.2f} "
              f"{'ok' if same else 'FLIP':>6} {j.get('side',''):>10}")

    n = len(diffs)
    print(f"\n{n} comparable, {missing} the archive itself cannot price")
    if not n:
        return
    print(f"sign agreement      : {n - flips}/{n} "
          f"({100*(n-flips)/n:.1f}%)")
    print(f"median |difference| : {st.median(diffs):.3f}bp")
    print(f"worst |difference|  : {max(diffs):.3f}bp")
    over = sum(1 for d in diffs if d > CFG.preopen_tilt_min_bp / 2)
    print(f"differences above half the gate ({CFG.preopen_tilt_min_bp/2:.2f}bp)"
          f": {over}/{n}")

    print()
    if flips:
        print("SIGN FLIPS EXIST. The live bot picked the opposite side to the")
        print("archive on those windows, so it is not trading the signal the")
        print("backtest priced. That is a defect, not sample size, and the")
        print("cause is almost certainly arrival lag — the bot pricing off a")
        print("staler spot than the archive holds. Widening price_at's")
        print("tolerance would make it WORSE, not better; the fix is to")
        print("evaluate later or to require a fresher print.")
    elif st.median(diffs) > CFG.preopen_tilt_min_bp / 4:
        print("NO FLIPS, but the tilts differ by an appreciable fraction of")
        print("the gate. The SIDE is the same, so the trades are the ones the")
        print("backtest priced; the gate is landing on a slightly different")
        print("population than the backtest assumed. Worth watching, not")
        print("worth acting on.")
    else:
        print("THE LIVE BOT AND THE BACKTEST COMPUTE THE SAME NUMBER. The gap")
        print("between the live win rate and the backtest's is therefore")
        print("sample size, which only waiting fixes — there is no logic")
        print("difference to chase.")


if __name__ == "__main__":
    main()
