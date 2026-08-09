"""Unit tests for the post-2026-08-07 TWAP resolution math.

  venv/bin/python scripts/test_twap_math.py

Covers the two pieces that now decide real money: Oracle's rolling-TWAP
reconstruction (used for the strike and the settlement cross-check) and
SnipeStrategy._twap_fv (the confidence model against the new target).
Pure arithmetic on synthetic samples — no network, no ledger, no bot.
"""
import dataclasses
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import CFG                      # noqa: E402
from bot.feeds.oracle import Oracle             # noqa: E402
from bot.strategies.snipe import SnipeStrategy  # noqa: E402

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

# ----------------------------------------------------------------- _twap_fv
print("\n[4] _twap_fv: mean, variance shrinkage, refusals")


class _StubSpot:
    pass


def strat(oracle, **over):
    cfg = dataclasses.replace(CFG, **over) if over else CFG
    return SnipeStrategy(cfg, None, oracle, _StubSpot(), None, None, None)


VOL = 1e-4                    # per-sqrt-second log vol
o = oracle_with({s: 100.0 for s in range(C - 30, C - 5)})       # 25s known at 100
s = strat(o, snipe_min_ticks=0.0, snipe_min_gap_bps=0.0, spot_tick=0.0)

fv, u = s._twap_fv(C, 30, 100.0, 100.0, VOL)
check("u at 5 unknown seconds", u, 5)
check_true("flat market, strike at price -> fv ~ 0.5", abs(fv - 0.5) < 1e-6, f"({fv:.6f})")

# projection: 5 unknown seconds at 106 lifts the mean by 5*(6)/30 = 1.0
fv_hi, _ = s._twap_fv(C, 30, 100.0, 106.0, VOL)
mu = (2500.0 + 5 * 106.0) / 30.0
var = 5 * (5 + 1) * (2 * 5 + 1) / 6.0
sd = (106.0 * VOL / 30.0) * math.sqrt(var)
want = 0.5 * (1 + math.erf(((mu - 100.0) / sd) / math.sqrt(2)))
check("projected mean/sd match closed form", fv_hi, want, tol=1e-12)
check_true("mu is the blend, not the spot price", abs(mu - 101.0) < 1e-9, f"(mu={mu})")

# THE headline: same market move, far more certainty than the old rule
old_sd_rel = VOL * math.sqrt(5)                 # spot close, 5s horizon
new_sd_rel = (VOL / 30.0) * math.sqrt(var)      # 30s TWAP, 5s unknown
check_true("TWAP sd ~10x smaller than spot-close sd",
           9.0 < old_sd_rel / new_sd_rel < 12.0,
           f"(ratio {old_sd_rel / new_sd_rel:.1f}x)")

# refusals
s_floor = strat(o, snipe_min_ticks=0.0, snipe_min_gap_bps=1.0, spot_tick=0.0)
check_true("sub-threshold gap refused",
           s_floor._twap_fv(C, 30, 100.0, 100.03, VOL) is None)
o_gappy = oracle_with({s_: 100.0 for s_ in range(C - 30, C - 5, 5)})   # 5/25 present
s_gappy = strat(o_gappy, snipe_min_ticks=0.0, snipe_min_gap_bps=0.0, spot_tick=0.0)
check_true("thin coverage refused", s_gappy._twap_fv(C, 30, 100.0, 101.0, VOL) is None)
o_done = oracle_with({s_: 100.0 for s_ in range(C - 30, C)})
s_done = strat(o_done, snipe_min_ticks=0.0, snipe_min_gap_bps=0.0, spot_tick=0.0)
check_true("no unknown seconds left refused",
           s_done._twap_fv(C, 30, 100.0, 101.0, VOL) is None)

# variance formula continuity at u == n (average has just started)
u_, n_, a_ = 30, 30, 0
v_at_n = u_ * u_ * a_ + u_ * (u_ + 1) * (2 * u_ + 1) / 6.0
check_true("V(u=n,a=0) ~ n^3/3", abs(v_at_n - 30 ** 3 / 3.0) / (30 ** 3 / 3.0) < 0.06,
           f"({v_at_n:.0f} vs {30 ** 3 / 3.0:.0f})")

print("\n" + ("ALL TESTS PASSED" if not FAILED else f"FAILURES: {FAILED}"))
sys.exit(1 if FAILED else 0)
