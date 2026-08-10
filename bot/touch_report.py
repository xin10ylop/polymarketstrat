"""Would the resting sell have filled, and when? Read from touch telemetry.

  venv/bin/python -m bot.touch_report
  DATA=bot/data/preopen-eth venv/bin/python -m bot.touch_report

WHY THIS REPLACES exit_curve's FILL COLUMN. A resting limit sell fills the
instant the price touches it. bot/exit_curve.py could only ask whether the bid
was above the target at T+2, T+15 and T+30, because that is all the book
recorder stores — a strictly harder test that misses every touch in between,
so its fill rates (5c in 60-78% of windows within 30s) are lower bounds by an
unknown margin.

The bot now watches its own position continuously off the CLOB websocket it is
already subscribed to, at roughly twenty samples a second for 90 seconds, and
writes one `preopen_mark` row per position holding the peak bid, when the peak
happened, and the first second each exit level was touched. That is the real
fill set, and it prices EVERY exit level at once rather than committing to 5c
in advance.

WHAT TO READ. For each level: how often it was touched at all, and the median
seconds to touch. A level that fills 80% of the time but takes 70 seconds is a
different instrument from one that fills 60% of the time in four — the second
one is the scalp, the first is a slow hold with extra steps. The 'by T+n'
columns split exactly that.

This does NOT price the strategy. Touch rate is not profit: the windows that
do not touch are still open positions that have to be closed somehow, and what
they settle at is a separate question this cannot see. It answers the one
question exit_curve got structurally wrong, and nothing more.

Read-only.
"""
import json
import os
import sqlite3
import statistics as st

DATA = os.environ.get("DATA", "bot/data/preopen-btc")
BY = [float(x) for x in os.environ.get("BY", "5,15,30,60,90").split(",")]


def main():
    path = os.path.join(DATA, "paper.db")
    if not os.path.exists(path):
        raise SystemExit(f"no ledger at {path}")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    rows = []
    for (d,) in db.execute("SELECT detail FROM events WHERE kind=?",
                           ("preopen_mark",)):
        try:
            j = json.loads(d)
        except ValueError:
            continue
        if "hit" in j:                      # skip pre-tracker rows
            rows.append(j)
    if not rows:
        raise SystemExit("no touch telemetry yet — this needs positions taken "
                         "after the tracker shipped; older marks lack 'hit'")

    n = len(rows)
    samp = [r.get("samples", 0) for r in rows]
    print(f"{DATA}: {n} positions with continuous tracking, "
          f"median {st.median(samp):.0f} book samples each "
          f"over {rows[0].get('t', 0):.0f}s\n")

    peaks = sorted(100 * (r["peak"] - r["entry"]) for r in rows)
    print(f"peak bid above entry: median {st.median(peaks):+.2f}c, "
          f"best {peaks[-1]:+.2f}c, worst {peaks[0]:+.2f}c")
    pt = [r["peak_t"] for r in rows if r.get("peak_t")]
    if pt:
        print(f"time to that peak: median {st.median(pt):.1f}s\n")

    levels = sorted({int(k) for r in rows for k in r["hit"]})
    if not levels:
        print("no level was ever touched in any window")
        return
    print(f"{'exit':>5} {'touched':>9} {'rate':>7} {'median s':>9} "
          + "".join(f"{f'by T+{b:.0f}':>10}" for b in BY))
    for c in levels:
        k = str(c)
        ts = [r["hit"][k] for r in rows if k in r["hit"]]
        cells = "".join(
            f"{100*sum(1 for t in ts if t <= b)/n:>9.0f}%" for b in BY)
        print(f"{c:>4}c {len(ts):>9} {100*len(ts)/n:>6.0f}% "
              f"{(st.median(ts) if ts else float('nan')):>9.1f} {cells}")

    print("\nTOUCH RATE IS NOT PROFIT. A level that fills is worth its cents")
    print("minus the entry fee only; the windows that DO NOT fill are still")
    print("open positions, and what those settle at decides the strategy. That")
    print("half needs the settled ledger, not this table.")
    print("These rates supersede exit_curve's fill column, which sampled three")
    print("instants and therefore could not see a touch between them.")


if __name__ == "__main__":
    main()
