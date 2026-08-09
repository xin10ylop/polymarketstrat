"""Unit tests for the post-2026-08-07 TWAP resolution math.

  venv/bin/python scripts/test_twap_math.py

Covers Oracle's rolling-TWAP reconstruction — the strike the snipe now
prices against and the settlement cross-check the mismatch halt rides on —
plus a guard that the ORIGINAL confidence model, and therefore the ~6bp
distance filter the validated edge was built on, is still what gates a
trade. Pure arithmetic on synthetic samples: no network, ledger or bot.
"""
import math as _m
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import CFG                      # noqa: E402
from bot.feeds.oracle import Oracle             # noqa: E402

FAILED = []


def check(name, got, want, tol=1e-9):
    ok = (got is want) if want is None else (
        got is not None and abs(got - want) <= tol)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        FAILED.append(name)


def check_true(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name} {detail}")
    if not cond:
        FAILED.append(name)


def oracle_with(samples):
    o = Oracle(CFG)
    o.samples = dict(samples)
    o.last_sample_s = max(samples) if samples else 0
    return o


# ---------------------------------------------------------------- twap_at
print("\n[1] twap_at: window edges, coverage, gaps")
C = 1000000
flat = {s: 100.0 for s in range(C - 60, C + 1)}
ramp = {s: 100.0 + (s - (C - 30)) for s in range(C - 30, C)}   # 100..129 over [C-30,C)

o = oracle_with(flat)
v, cov = o.twap_at(C, 30)
check("flat 30s TWAP value", v, 100.0)
check("flat 30s TWAP coverage", cov, 1.0)

o = oracle_with(ramp)
v, _ = o.twap_at(C, 30, "left")
check("ramp [t-n,t) mean", v, sum(range(100, 130)) / 30.0)
# right edge shifts the window one second later: drops C-30, would add C
o2 = oracle_with({**ramp, C: 130.0})
v2, _ = o2.twap_at(C, 30, "right")
check("ramp (t-n,t] mean", v2, sum(range(101, 131)) / 30.0)
check_true("edges differ by exactly one sample",
           abs(v2 - v) - 1.0 < 1e-9, f"({v2 - v:.4f})")

gappy = {s: 100.0 for s in range(C - 30, C) if s % 3}          # 2/3 present
o = oracle_with(gappy)
_, cov = o.twap_at(C, 30)
check("gappy coverage", round(cov, 4), round(20 / 30, 4))
check("empty range -> None", o.twap_at(C - 500, 30)[0], None)

# ------------------------------------------------------------ twap_winner
print("\n[2] twap_winner: agreement, ties, coverage refusal")
up = {s: 100.0 for s in range(C - 330, C - 300)}
up.update({s: 101.0 for s in range(C - 30, C)})
o = oracle_with(up)
check_true("clear up -> 'up'", o.twap_winner(C - 300, C, 30) == "up")

dn = {s: 101.0 for s in range(C - 330, C - 300)}
dn.update({s: 100.0 for s in range(C - 30, C)})
o = oracle_with(dn)
check_true("clear down -> 'down'", o.twap_winner(C - 300, C, 30) == "down")

# boundary-straddling tie: the two conventions land on OPPOSITE sides of the
# strike, so the window is not callable. [C-30,C) holds the 99 and averages
# below the open; (C-30,C] drops it, picks up the 101, and averages above.
tie = {s: 100.0 for s in range(C - 330, C - 299)}    # open: flat under both edges
tie.update({s: 100.0 for s in range(C - 29, C)})
tie[C - 30] = 99.0
tie[C] = 101.0
o = oracle_with(tie)
_lft, _ = o.twap_at(C, 30, "left")
_rgt, _ = o.twap_at(C, 30, "right")
check_true("fixture really straddles", _lft < 100.0 < _rgt, f"({_lft:.4f}/{_rgt:.4f})")
check_true("convention disagreement -> None", o.twap_winner(C - 300, C, 30) is None)

thin = {s: 100.0 for s in range(C - 330, C - 300)}
thin.update({s: 101.0 for s in range(C - 30, C, 4)})           # 25% coverage
o = oracle_with(thin)
check_true("thin coverage -> None", o.twap_winner(C - 300, C, 30, 0.9) is None)

# ------------------------------------------------------------- twap_known
print("\n[3] twap_known: how much of the closing average is already history")
known = {s: 100.0 for s in range(C - 30, C - 5)}               # 25 of 30 elapsed
o = oracle_with(known)
s_sum, n_present, n_elapsed = o.twap_known(C, 30, C - 5)
check("known n_elapsed at 5s to go", n_elapsed, 25)
check("known n_present", n_present, 25)
check("known sum", s_sum, 2500.0)

# --------------------------------------------------- adapted snipe strike
print("\n[4] the snipe adaptation: new strike, ORIGINAL confidence model")
o = oracle_with({s_: 100.0 + (s_ - (C - 30)) * 0.1 for s_ in range(C - 30, C)})
k_twap, cov = o.twap_at(C, 30)
check("strike is the TWAP, not the last print", k_twap, sum(100.0 + i * 0.1 for i in range(30)) / 30)
check_true("strike differs from the closing tick",
           abs(k_twap - 102.9) > 1.0, f"(twap {k_twap:.2f} vs tick 102.9)")
check("strike coverage full", cov, 1.0)

VOL = 1e-4
def fv_old(s_adj, k, tau):        # the untouched formula the edge was validated on
    return 0.5 * (1 + _m.erf((_m.log(s_adj / k) / (VOL * _m.sqrt(tau))) / _m.sqrt(2)))
check_true("6bp at 6s to go still clears 0.995",
           fv_old(100.0 * (1 + 6.5e-4), 100.0, 6) >= 0.995,
           f"(fv {fv_old(100.0 * (1 + 6.5e-4), 100.0, 6):.4f})")
check_true("2bp at 6s to go still does NOT clear 0.995",
           fv_old(100.0 * (1 + 2e-4), 100.0, 6) < 0.995,
           f"(fv {fv_old(100.0 * (1 + 2e-4), 100.0, 6):.4f})")
check_true("the distance filter is preserved: threshold ~6bp",
           5.5 < 2.5758 * VOL * _m.sqrt(6) * 1e4 < 6.5,
           f"({2.5758 * VOL * _m.sqrt(6) * 1e4:.2f}bp)")

print("\n" + ("ALL TESTS PASSED" if not FAILED else f"FAILURES: {FAILED}"))
sys.exit(1 if FAILED else 0)
