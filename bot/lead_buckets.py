"""Does the achieved LEAD or the spot AGE predict the outcome? Timing probe.

  venv/bin/python -m bot.lead_buckets
  ERA=1786060800 venv/bin/python -m bot.lead_buckets   # rule era, larger n

WHY. The backtest prices the tilt at a fixed T-3; the live loop fires when
it fires, and every entry records the ACTUAL seconds-to-open ("lead") and
the staleness of the spot bar it decided on ("age") in its preopen_entry
event. If late entries (small lead) or stale-spot entries settle worse,
the fix is TIMING -- raise PREOPEN_MIN_LEAD_S, tighten the loop, or gate
on age==0 (an old agent-A finding for eth that was never measured) -- and
not the signal. If they settle the same, the loop's jitter is free and
the knobs should be left alone.

Per unit, per-decision (fills aggregated by window, launch_ev-style era
and mismatch filters), decisions bucketed by their recorded lead and,
separately, by their recorded spot age. Each bucket: n, EV c/share, win%,
and the EV of each time half.

CAVEAT PRINTED WITH THE NUMBERS: lead is not randomized. A slow loop
iteration correlates with feed load, and stale spot correlates with quiet
tape -- a bad bucket is a measured association, not yet a cause.

THE PRE-REGISTERED BAR, as everywhere: a timing config change needs the
offending bucket negative (or the clean bucket's advantage) to hold in
BOTH halves AND in the same direction on the other coin of the family.
Read-only.
"""
import glob
import json
import os
import sqlite3
import time

ERA = int(os.environ.get("ERA", "1786450800"))
MIN_N = int(os.environ.get("MIN_N", "5"))
DATA_GLOB = os.environ.get("DATA_GLOB", "bot/data/preopen-*/paper.db")
# Half-open [lo, hi) bins. The first run used inclusive edges and double-
# counted the mass at exactly 3.00s (the loop firing right on target) in
# two buckets; it also showed the whole population lives in [2.0, 3.0],
# so the bins are cut fine inside that band with the on-target mass alone
# in the last one.
LEADS = ((0.0, 2.5, "<2.5s"), (2.5, 2.9, "2.5-2.9"),
         (2.9, 3.0, "2.9-3.0"), (3.0, 99.0, "3.0s+"))
AGES = ((0, 2, "0-1s"), (2, 3, "2s"), (3, 4, "3s"), (4, 99, "4s+"))


def table(rows, key, buckets, title):
    print(title)
    print(f"{'bucket':>9} {'n':>5} {'EV c/sh':>8} {'win%':>6} "
          f"{'h1 EV':>7} {'h2 EV':>7}")
    for lo, hi, lab in buckets:
        sub = [r for r in rows if r[key] is not None and lo <= r[key] < hi]
        if len(sub) < MIN_N:
            continue
        half = len(sub) // 2

        def ev(s):
            return sum(x[3] for x in s) / len(s) if s else 0.0
        wins = sum(1 for x in sub if x[3] > 0)
        print(f"{lab:>9} {len(sub):>5} {ev(sub):>+8.2f} "
              f"{100*wins/len(sub):>5.1f}% "
              f"{ev(sub[:half]):>+7.2f} {ev(sub[half:]):>+7.2f}")


def unit(path):
    name = os.path.basename(os.path.dirname(path))
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        meta = {}
        for (detail,) in db.execute(
                "SELECT detail FROM events WHERE kind='preopen_entry'"):
            try:
                d = json.loads(detail)
                meta[d["w"]] = (d.get("lead"), d.get("age"))
            except (ValueError, KeyError):
                continue
        decs = db.execute(
            "SELECT f.wts, SUM(f.size), SUM(f.pnl), MAX(f.ts) "
            "FROM fills f JOIN settlements s ON f.wts = s.wts "
            "WHERE f.pnl IS NOT NULL AND f.strategy='preopen' "
            "AND s.winner IS NOT NULL AND s.mismatch=0 "
            "GROUP BY f.wts HAVING SUM(f.size) >= 5 AND MAX(f.ts) >= ? "
            "ORDER BY MAX(f.ts)", (ERA,)).fetchall()
    except sqlite3.Error as e:
        print(f"\n{name}: unreadable ({str(e)[:40]})")
        return
    rows, unmatched = [], 0
    for w, sz, pnl, _ in decs:
        if w not in meta:
            unmatched += 1
            continue
        lead, age = meta[w]
        rows.append((lead, age, w, 100 * pnl / sz))
    if len(rows) < 4 * MIN_N:
        print(f"\n{name}: {len(rows)} decisions with entry events -- too few")
        return
    print(f"\n{name}: {len(rows)} decisions with entry events"
          + (f" ({unmatched} settled decisions had no event -- pre-"
             f"instrumentation era)" if unmatched else ""))
    ls = sorted(r[0] for r in rows if r[0] is not None)
    if ls:
        def pct(q):
            return ls[min(len(ls) - 1, int(q * len(ls)))]
        print(f"  lead: min {ls[0]:.2f} p25 {pct(.25):.2f} "
              f"p50 {pct(.5):.2f} p75 {pct(.75):.2f} max {ls[-1]:.2f}; "
              f"exactly on target (3.00) n="
              f"{sum(1 for v in ls if v == 3.0)}")
    ages = {}
    for r in rows:
        if r[1] is not None:
            ages[int(r[1])] = ages.get(int(r[1]), 0) + 1
    print("  age counts: " + (", ".join(
        f"{k}s:{v}" for k, v in sorted(ages.items())) or "none recorded"))
    table(rows, 0, LEADS, "BY ACHIEVED LEAD (seconds before the open)")
    table(rows, 1, AGES, "BY SPOT AGE AT DECISION (staleness of the bar)")


def main():
    print(f"lead/age buckets, era >= "
          f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(ERA))} "
          f"(ERA= to override). Lead is NOT randomized -- associations, "
          f"not causes.")
    paths = sorted(glob.glob(DATA_GLOB))
    if not paths:
        raise SystemExit(f"no ledgers match {DATA_GLOB}")
    for p in paths:
        unit(p)
    print("\nTHE BAR: a timing change (min-lead floor, age gate) needs the")
    print("offending bucket to hold in BOTH halves AND in the same direction")
    print("on the other coin of the family. One bucket in one half is noise.")


if __name__ == "__main__":
    main()
