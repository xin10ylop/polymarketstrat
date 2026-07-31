"""Pattern + parameter analysis over a candidate tape (exact re-simulation).

    python3 analyze_tape.py <coin> [px_floor]

Baseline = first candidate per window with fvx>=0.995, px<=0.97, 12<=sz<=500,
gate pass, px>=px_floor. Every variant below re-derives entries from the tape
under its own filters (true first-touch semantics), so numbers are exact, not
post-hoc approximations. Split-half = first vs second half of calendar days.
"""
import sys

import numpy as np
import pandas as pd

COIN = sys.argv[1].lower()
FLOOR = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
SP = "/home/user/polymarketstrat/data/tapes"
t = pd.read_parquet(f"{SP}/cand_{COIN}.parquet")
t = t.sort_values(["wts", "toff"]).reset_index(drop=True)
days = sorted(t.date.unique())
half1 = set(days[: len(days) // 2])


def entries(fv_min=0.995, px_max=0.97, sz_min=12, sz_max=500, px_min=FLOOR,
            toff_min=294.0, need_gate=True):
    m = ((t.fvx >= fv_min) & (t.px <= px_max) & (t.px >= px_min)
         & (t.sz >= sz_min) & (t.sz <= sz_max) & (t.toff >= toff_min))
    if need_gate:
        m &= t.gate
    e = t[m].groupby("wts").first().reset_index()
    e["shares"] = np.minimum(e.sz, 250)
    e["fee"] = 0.07 * e.px * (1 - e.px)
    e["ev"] = e.win - e.px - e.fee
    e["pnl"] = e.ev * e.shares
    return e


def stat(e, label):
    if not len(e):
        return f"{label:30s} n=0"
    wev = 100 * e.ev.mul(e.shares).sum() / e.shares.sum()
    h1 = e[e.date.isin(half1)]
    h2 = e[~e.date.isin(half1)]
    f = lambda x: (100 * x.ev.mul(x.shares).sum() / x.shares.sum()) if len(x) else float("nan")
    return (f"{label:30s} n={len(e):4d} win={100*e.win.mean():5.1f}% "
            f"ev={wev:+6.2f}c pnl=${e.pnl.sum():+8.0f} "
            f"| halves: {f(h1):+6.2f}c ({len(h1)}) / {f(h2):+6.2f}c ({len(h2)})")


base = entries()
print(f"=== {COIN.upper()} tape: {t.wts.nunique()} windows with candidates, "
      f"{len(days)} days, floor={FLOOR} ===")
print(stat(base, "BASELINE (current params)"))

print("\n--- by 4h block (UTC) ---")
base["block"] = base.hour // 4
for b in range(6):
    g = base[base.block == b]
    if len(g):
        print(stat(g, f"  {b*4:02d}:00-{b*4+4:02d}:00"))

print("\n--- by day of week (0=Mon) ---")
for d in range(7):
    g = base[base.dow == d]
    if len(g):
        print(stat(g, f"  dow {d}"))

print("\n--- price bands within baseline ---")
for lo, hi in [(0, 0.80), (0.80, 0.90), (0.90, 0.98)]:
    g = base[(base.px >= lo) & (base.px < hi)]
    if len(g):
        print(stat(g, f"  px {lo:.2f}-{hi:.2f}"))

print("\n--- parameter scans (each an exact re-simulation) ---")
print(stat(entries(fv_min=0.997), "fv_min 0.997"))
print(stat(entries(fv_min=0.999), "fv_min 0.999"))
print(stat(entries(px_max=0.95), "ask_max 0.95"))
print(stat(entries(px_max=0.92), "ask_max 0.92"))
print(stat(entries(sz_max=250), "skip_ask_above 250"))
print(stat(entries(sz_min=20), "min_ask_size 20"))
print(stat(entries(toff_min=295.5), "eval from T-4.5s"))
print(stat(entries(toff_min=297.0), "eval from T-3.0s"))
print(stat(entries(px_min=max(FLOOR, 0.50)), "px floor 0.50"))
print(stat(entries(px_min=max(FLOOR, 0.80)), "px floor 0.80"))
print(stat(entries(px_min=max(FLOOR, 0.90)), "px floor 0.90"))
print(stat(entries(need_gate=False), "NO survival gate (fantasy)"))
