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

    pk = [r for r in rows if r.get("peak") is not None]
    if pk:
        peaks = sorted(100 * (r["peak"] - r["entry"]) for r in pk)
        print(f"peak bid vs entry : median {st.median(peaks):+.2f}c, "
              f"best {peaks[-1]:+.2f}c, worst {peaks[0]:+.2f}c")
        pt = [r["peak_t"] for r in pk if r.get("peak_t") is not None]
        if pt:
            print(f"time to that peak : median {st.median(pt):.1f}s")
    lw = [r for r in rows if r.get("low") is not None]
    if lw:
        lows = sorted(100 * (r["low"] - r["entry"]) for r in lw)
        print(f"low  bid vs entry : median {lows[len(lows)//2]:+.2f}c, "
              f"worst {lows[0]:+.2f}c")
        bad = sum(1 for r in pk if r["peak"] <= r["entry"] + 1e-9)
        print(f"never traded above what we paid: {bad} of {len(pk)} "
              f"({100*bad/len(pk):.0f}%)  <- the scalp's failure mode")
    print()

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

    # ------------------------------------------------ does the jump SCALE?
    # The tilt's DIRECTION is measured (98.9% sign agreement at T-3). Its
    # MAGNITUDE never was, and that is what picks the exit level: if 1bp
    # moves the book 2c and 5bp moves it 10c, a fixed 5c exit leaves money on
    # the big ones and never fills on the small ones.
    have = [r for r in rows if r.get("peak") is not None and "tilt" in r]
    if len(have) >= 6:
        print("\nDOES THE JUMP SCALE WITH THE TILT?")
        print(f"{'|tilt| bp':>12} {'n':>4} {'med peak':>10} {'med low':>10} "
              f"{'med 5c s':>10} {'wrong way':>10}")
        band = [(0, 1), (1, 2), (2, 4), (4, 8), (8, 1e9)]
        for lo_, hi_ in band:
            sel = [r for r in have if lo_ <= abs(r["tilt"]) < hi_]
            if not sel:
                continue
            pk = st.median(100 * (r["peak"] - r["entry"]) for r in sel)
            lw = st.median(100 * (r["low"] - r["entry"]) for r in sel
                           if r.get("low") is not None)
            t5 = [r["hit"]["5"] for r in sel if "5" in r["hit"]]
            # "wrong way": the book never traded above what we paid
            bad = sum(1 for r in sel if r["peak"] <= r["entry"] + 1e-9)
            lbl = f"{lo_:.0f}-{hi_:.0f}" if hi_ < 1e9 else f"{lo_:.0f}+"
            print(f"{lbl:>12} {len(sel):>4} {pk:>+9.2f}c {lw:>+9.2f}c "
                  f"{(st.median(t5) if t5 else float('nan')):>10.1f} "
                  f"{100*bad/len(sel):>9.0f}%")
        print("A rising 'med peak' column means the exit should SCALE with the")
        print("tilt rather than sit at a fixed 5c. A flat one means 5c-ish is")
        print("right for every window and the tilt only picks the side.")
        print("'wrong way' is the scalp's real failure mode: the book repriced")
        print("against the side the tilt chose and no resting sell ever fills.")

    print("\nTOUCH RATE IS NOT PROFIT. A level that fills is worth its cents")
    print("minus the entry fee only; the windows that DO NOT fill are still")
    print("open positions, and what those settle at decides the strategy. That")
    print("half needs the settled ledger, not this table.")
    print("These rates supersede exit_curve's fill column, which sampled three")
    print("instants and therefore could not see a touch between them.")


if __name__ == "__main__":
    main()
