"""Unit tests for the carry-forward strike the pre-open bot now trades on.

  venv/bin/python scripts/test_preopen_strike.py

THE CHANGE UNDER TEST. The strike used to fill a hole by rescaling the mean
of the seconds that were present across the whole window; it now carries the
last print forward into the hole. Measured on real hole patterns that is 4x
more accurate on btc and 5.5x on eth, but accuracy is not the risk — the risk
is that an imputation rule quietly reaches somewhere it should not, and a
strike that peeked at a price from after the decision instant would make every
downstream number a fiction while looking perfectly healthy.

So the first and most important test here is the boring one: change a price
the bot must not be able to see, and the strike must not move.

Pure arithmetic on synthetic samples: no network, ledger or bot.
"""
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


T = 1_000_000          # window open
N = 30                 # strike length
LEAD = 3


print("no lookahead — the decision cannot depend on prices after T-lead")
# complete grid at 100, but the three seconds at [T-3, T) hold something wild.
# The bot decides at T-3, so those must be invisible however extreme they are.
base = {s: 100.0 for s in range(T - N, T)}
for hidden in (100.0, 500.0, 0.01):
    g = dict(base)
    for s in range(T - LEAD, T):
        g[s] = hidden
    k, present, elapsed = oracle_with(g).twap_carry(T, N, T - LEAD, tail=100.0)
    check(f"strike ignores a hidden tail of {hidden}", k, 100.0)
check("elapsed range is the 27 seconds before the decision", elapsed, N - LEAD)
check("all 27 counted present on a complete grid", present, N - LEAD)

# and the same for a price stamped exactly at the decision second: T-3 IS
# visible at T-3 (it is not in the future), but T-2 is not
g = dict(base)
g[T - LEAD] = 130.0
k, _, _ = oracle_with(g).twap_carry(T, N, T - LEAD, tail=100.0)
check("the second AT T-lead is excluded from the elapsed range", k, 100.0)
g = dict(base)
g[T - LEAD - 1] = 127.0
k, _, _ = oracle_with(g).twap_carry(T, N, T - LEAD, tail=100.0)
check("the second BEFORE T-lead is included", k, 100.0 + 27.0 / N)


print("\ncarry-forward fills a hole with the last print, not the mean")


def step_grid(hole_at):
    """100 for the first 13 elapsed seconds, 200 after, one second missing."""
    g = {s: (100.0 if s < T - N + 13 else 200.0) for s in range(T - N, T - LEAD)}
    del g[hole_at]
    return g


# The hole IS the step second. Carrying looks backward, so it takes 100 —
# the price that was actually standing when that second began — even though
# the very next print is 200. This is the case that would tempt an
# implementation to peek forward, and it must not.
o = oracle_with(step_grid(T - N + 13))
k, present, elapsed = o.twap_carry(T, N, T - LEAD, tail=200.0)
check("one second is genuinely missing", present, N - LEAD - 1)
check("hole at the step carries the OLD price, not the new one",
      k, (14 * 100.0 + 13 * 200.0 + 3 * 200.0) / N)
# the OLD rule on the same data, for the record: rescale the present mean
old_mean = (13 * 100.0 + 13 * 200.0) / 26
old_k = (old_mean * (N - LEAD) + LEAD * 200.0) / N
check_true("carrying and rescaling really do differ here",
           abs(k - old_k) > 1.0, f"(carry {k:.3f} vs rescale {old_k:.3f})")

# A hole one second AFTER the step carries 200, so the rule is not simply
# biased low — it tracks whichever price was last standing.
k2, _, _ = oracle_with(step_grid(T - N + 14)).twap_carry(
    T, N, T - LEAD, tail=200.0)
check("hole after the step carries the NEW price",
      k2, (13 * 100.0 + 14 * 200.0 + 3 * 200.0) / N)
check_true("so the two holes land on different strikes", k2 > k,
           f"(step-second {k:.3f} vs after-step {k2:.3f})")

print("\nthe carry reaches backward only")
# hole at the very first second of the window: it must seed from BEFORE the
# window, never from the second after it
g = {s: 300.0 for s in range(T - N, T - LEAD)}
del g[T - N]
g[T - N - 1] = 111.0
k, _, _ = oracle_with(g).twap_carry(T, N, T - LEAD, tail=300.0)
check("leading hole seeds from the print before the window",
      k, (111.0 + 29 * 300.0) / N)
# with nothing at all before it, there is nothing to carry and it must refuse
g = {s: 300.0 for s in range(T - N, T - LEAD)}
del g[T - N]
k, present, _ = oracle_with(g).twap_carry(T, N, T - LEAD, tail=300.0)
check("no seed anywhere -> refuse rather than guess", k, None)

print("\nblackouts always refuse, whatever the floor")
o = oracle_with({T - N - 200: 100.0})           # one ancient print, nothing near
k, present, elapsed = o.twap_carry(T, N, T - LEAD, tail=100.0)
check("a blackout reports zero seconds present", present, 0)
check_true("and zero coverage refuses at every floor tested",
           all(present < elapsed * f for f in (0.9, 0.8, 0.75, 0.7, 0.6, 0.1)))

print("\ncoverage counts real prints, not carried ones")
g = {s: 100.0 for s in range(T - N, T - LEAD)}
for s in range(T - N + 5, T - N + 11):          # punch a 6-second hole
    del g[s]
k, present, elapsed = oracle_with(g).twap_carry(T, N, T - LEAD, tail=100.0)
check("21 of 27 seconds present", present, 21)
check("the 27 carried seconds do NOT inflate the count", elapsed, 27)
check_true("passes the 0.75 floor", present >= elapsed * 0.75)
check_true("would have failed the old 0.9 floor", present < elapsed * 0.9)
check("the value is still right despite the hole", k, 100.0)
# one second thinner and 0.75 refuses too: the floor needs 20.25 of 27, so
# 21 is the last passing count and 20 is the first refusal
del g[T - N + 11]
_, present20, _ = oracle_with(g).twap_carry(T, N, T - LEAD, tail=100.0)
check("20 present is one below the boundary", present20, 20)
check_true("and 0.75 refuses it", present20 < elapsed * 0.75)

print("\nthe tail is the caller's spot, and it is what moves the tilt")
g = {s: 100.0 for s in range(T - N, T - LEAD)}
k, _, _ = oracle_with(g).twap_carry(T, N, T - LEAD, tail=100.6)
check("tail imputed from spot over the unelapsed seconds",
      k, (27 * 100.0 + 3 * 100.6) / N)
tilt = (100.6 - k) / k * 1e4
check_true("a spot above the trailing mean tilts up", tilt > 0,
           f"(tilt {tilt:+.2f}bp)")

print("\nthe touch tracker catches what three snapshots cannot")
# The reason this exists: a resting limit sell fills on a momentary touch.
# exit_curve could only read the book at T+2/T+15/T+30, so a spike between
# those instants was invisible and its fill rates are lower bounds.
from bot.strategies.preopen import PreopenStrategy      # noqa: E402


class _State:
    best_bid = None


class _Clob:
    def __init__(self):
        self.st = _State()

    def state(self, _tok):
        return self.st


_clob = _Clob()
_p = PreopenStrategy(CFG, _clob, None, None, None, None, None)
_W = 1_000_000
_info = dict(side="up", token="t", px=0.50, sz=250, tilt=1.0,
             peak=None, peak_t=None, low=None, low_t=None, first=None,
             hit={}, n=0)
# the bid spikes to 0.56 for a single sample at t+7, then falls back
_PATH = [(0.0, 0.505), (2.0, 0.508), (5.0, 0.512), (7.0, 0.560),
         (9.0, 0.514), (15.0, 0.511), (30.0, 0.509)]
for _dt, _bid in _PATH:
    _clob.st.best_bid = _bid
    _p._track(_W, _info, _W + _dt)

check("peak bid recorded", _info["peak"], 0.560)
check("and when it happened", _info["peak_t"], 7.0)
check("a 5c touch lasting one sample is caught", _info["hit"].get("5"), 7.0)
check("1c is caught earlier, when the bid first crosses 0.51",
      _info["hit"].get("1"), 5.0)
check_true("10c was never reached, so it is absent rather than zero",
           "10" not in _info["hit"])
_snap = {t: b for t, b in _PATH if t in (2.0, 15.0, 30.0)}
check_true("the three-snapshot method would have missed the 5c fill entirely",
           all(b < 0.55 for b in _snap.values()), f"(it sees {_snap})")
# nothing before the open counts
_pre = dict(_info, hit={}, peak=None, peak_t=None, low=None, low_t=None)
_clob.st.best_bid = 0.99
_p._track(_W, _pre, _W - 1.0)
check_true("samples before the open are ignored",
           not _pre["hit"] and _pre["peak"] is None)

print("\nthe tracker can see the book repricing AGAINST us")
# The first version seeded the peak at the entry price and only ratcheted up,
# so a jump the wrong way was indistinguishable from one that went nowhere.
_c2 = _Clob()
_p2 = PreopenStrategy(CFG, _c2, None, None, None, None, None)
_bad = dict(side="up", token="t", px=0.50, sz=250, tilt=1.0, peak=None,
            peak_t=None, low=None, low_t=None, first=None, hit={}, n=0)
for _dt, _bid in [(0.5, 0.497), (2.0, 0.462), (6.0, 0.441), (20.0, 0.455)]:
    _c2.st.best_bid = _bid
    _p2._track(_W, _bad, _W + _dt)
check("peak is the best bid seen, BELOW entry here", _bad["peak"], 0.497)
check("the low is recorded", _bad["low"], 0.441)
check("and when the low happened", _bad["low_t"], 6.0)
check_true("no exit level was ever touched", not _bad["hit"])
check_true("this is now distinguishable from a flat window",
           _bad["peak"] < 0.50, f"(peak {_bad['peak']} < entry 0.50)")

print("\nthe bot records how stale its own view was")
# eth's live tilt differs from the archive's by a median 1.02bp against a
# 1.0bp gate, btc's by 0.08bp. With the maths proven identical the only
# possible cause is what had ARRIVED, so the bot now records it.
_o3 = _o = Oracle(CFG)
_T = 1_000_000
_N = CFG.oracle_twap_s
_L = int(CFG.preopen_lead_s)


def _tilt_with(latest):
    """_tilt on a complete grid whose NEWEST print is T-latest — i.e. a feed
    that has delivered everything up to that second and nothing after."""
    o = Oracle(CFG)
    o.samples = {s: 100.0 for s in range(_T - _N - 5, _T - latest + 1)}
    o.samples[_T - latest] = 100.5               # the newest print moved
    o.last_sample_s = max(o.samples)
    return PreopenStrategy(CFG, None, o, None, None, None, None)._tilt(_T, _L)


_r = _tilt_with(_L)                               # current: T-3 has arrived
check("a current feed reports spot age 0", _r[5], 0)
check_true("and full coverage", _r[3] == _r[4], f"({_r[3]}/{_r[4]})")
_r = _tilt_with(_L + 3)                           # newest print is T-6
check("three seconds behind reports age 3", _r[5], 3)
# age counts back from T-lead; coverage counts seconds INSIDE [T-N, T-lead),
# which ends one second earlier. Three seconds of staleness therefore costs
# two elapsed seconds, not three — they are different windows, not a bug.
check_true("coverage is short by two, one fewer than the age",
           _r[4] - _r[3] == 2, f"({_r[3]}/{_r[4]}, age {_r[5]})")
check("beyond price_at's 3s reach it refuses outright",
      _tilt_with(_L + 4), None)

print("\nthe live bot and the backtest are the same function")
# Two implementations of one signal is two chances to be wrong, and a live
# win rate below the backtest's is unreadable until they are known equal:
# only then is sample size a sufficient explanation for the gap.
import random as _rnd                                    # noqa: E402
from bot.feeds.oracle import Oracle as _Oracle           # noqa: E402
from bot.scalp_backtest import tilt_at as _tilt_at       # noqa: E402

_rnd.seed(17)
_T0 = 1_786_100_000 - 1_786_100_000 % 300
_g, _px = {}, 64000.0
for _s in range(_T0, _T0 + 4 * 3600):
    _px *= 1 + _rnd.gauss(0, 2e-5)
    if _rnd.random() > 0.06:            # ~6% holes, as the real grid has
        _g[_s] = _px
_o = _Oracle(CFG)
_o.samples, _o.last_sample_s = _g, max(_g)
_s2 = PreopenStrategy(CFG, None, _o, None, None, None, None)

_n = _worst = _mismatch = 0
for _w in range(_T0 + 600, _T0 + 4 * 3600 - 600, 300):
    _live = _s2._tilt(_w, CFG.preopen_lead_s)
    _bt = _tilt_at(_g, _w)
    if (_live is None) != (_bt is None):
        _mismatch += 1
    elif _live is not None:
        _n += 1
        _worst = max(_worst, abs(_live[0] - _bt[0]))
check_true("both paths price the same windows", _mismatch == 0,
           f"({_mismatch} disagreed on whether to refuse)")
check_true("and return the same tilt", _worst < 1e-9,
           f"(n={_n}, worst |diff| {_worst:.12f}bp)")

print("\n" + ("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}"))
raise SystemExit(1 if FAILED else 0)
