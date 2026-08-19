"""The paper daily stop is RECORDED, not enforced — and nothing else moved.

  venv/bin/python scripts/test_risk_shadow_stop.py

WHY THIS EXISTS. On 2026-08-11 btc 5m sat dark for five hours and eth 5m for
six hours forty-seven, both stopped by the daily-loss breaker, and no counter
or log line in either bot said so. The visibility half of that is fixed in
preopen.py. This is the other half: a capital-preservation rule running on a
MEASUREMENT instrument censors the sample in one direction — losing runs are
truncated at the breaker, winning runs record in full — so every win rate and
every drawdown drawn from those ledgers reads better than the truth.

In paper there is no capital to preserve, so the stop is recorded and trading
continues. An uncensored record can always be censored in analysis; a censored
one can never be repaired.

THE RISK OF THIS CHANGE is that it quietly weakens something it should not.
Two things must survive untouched, and most of this file is about them:
  - live mode must still halt, and stickily
  - the MISMATCH halt must still bite in paper, because that one means our
    oracle disagrees with the exchange. It is a correctness signal, not a
    P&L signal, and it must stop every strategy in every mode.

Pure in-memory fakes: no network, no ledger file, no bot.
"""
import logging
import os
import sys
import time
from dataclasses import replace

# The code under test logs a WARNING on every shadow trip and an ERROR on
# every halt — by design. Left on, a run prints nine of them straight to the
# terminal, interleaved with the PASS lines, which makes the five-suite
# deploy gate unreadable. The assertions check the LEDGER rows, not the log,
# so silencing the logger costs no coverage.
logging.getLogger("risk").setLevel(logging.CRITICAL)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import CFG                      # noqa: E402
from bot.engine.risk import RiskManager         # noqa: E402

FAILED = []


def check_true(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name} {detail}")
    if not cond:
        FAILED.append(name)


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        FAILED.append(name)


class FakeLedger:
    """Only what RiskManager._check reads."""

    def __init__(self, pnl=0.0, mismatches=0):
        self._pnl, self._mismatches = pnl, mismatches
        self.events = []

    def event(self, kind, detail=""):
        self.events.append((kind, detail))

    def realized_pnl_today(self):
        return self._pnl

    def lifetime_pnl(self):
        return self._pnl

    def mismatches(self):
        return self._mismatches

    def snipe_trailing_pnl(self, n):
        return (0.0, 0)

    def preopen_trailing_pnl(self, n):
        # fast-bleed breaker input (launch blocker #2): default = no
        # trailing window yet; the breaker's own tests override this
        return getattr(self, "_trail", (0.0, 0))

    def snipe_fills_since_trailing_halt(self):
        return 0

    def unmarked_old_fills(self):
        return 0

    def needs_ack(self):
        # live incident re-arm (audit 2026-08-13): the fake defaults to no
        # pending incident; the incident-halt path has its own tests below
        return None

    def kinds(self):
        return [k for k, _ in self.events]


class FakeSpot:
    """halted("snipe") reaches into the spot feed for its silence check."""

    def silence(self):
        return 0.0


def rm(mode="paper", shadow=True, pnl=0.0, mismatches=0):
    cfg = replace(CFG, mode=mode, paper_shadow_daily_stop=shadow,
                  max_daily_loss=250.0, bankroll=1000.0)
    led = FakeLedger(pnl, mismatches)
    r = RiskManager(cfg, led, None, FakeSpot())
    r._started = time.time() - 3600      # past warmup
    return r, led


print("paper: a losing day is recorded and trading continues")
r, led = rm(pnl=-308.68)
r._check()
check_true("the breaker did NOT halt the strategy", not r.halted("preopen"))
check_true("nor any other scope", not r.halted("snipe"))
check("but the day is on the record", led.kinds(), ["SHADOW_HALT"])
check_true("and the event carries the number that tripped it",
           "-308.68" in led.events[0][1], f"({led.events[0][1]})")
# the check runs every 5s; a day-long loss must not write 17,000 rows
for _ in range(50):
    r._check()
check("recorded once per day, not once per check", len(led.events), 1)
check_true("and still not halted after 50 more checks",
           not r.halted("preopen"))

print("\npaper: a NEW day records again")
r2, led2 = rm(pnl=-400.0)
r2._check()
r2._shadow_day -= 1                      # pretend the UTC day rolled
r2._check()
check("the next day gets its own row", len(led2.events), 2)

print("\nlive is untouched: the stop still halts, and stickily")
rl, ledl = rm(mode="live", pnl=-308.68)
rl._check()
check_true("live halts on the daily loss", rl.halted("preopen"))
check_true("and it is a real HALT, not a shadow row",
           "HALT" in ledl.kinds() and "SHADOW_HALT" not in ledl.kinds(),
           f"({ledl.kinds()})")
check_true("sticky: no auto-lift time, so a human must restart",
           rl._halts["all"][1] is None)
# the shadow flag must not be able to reach live behaviour at all
rl2, _ = rm(mode="live", shadow=True, pnl=-308.68)
rl2._check()
check_true("the paper flag cannot unlock live, even set to shadow",
           rl2.halted("preopen"))

print("\npaper with the flag off enforces exactly as before")
ro, ledo = rm(shadow=False, pnl=-308.68)
ro._check()
check_true("halts", ro.halted("preopen"))
check_true("with a next-UTC-day auto-lift, not sticky",
           ro._halts["all"][1] is not None)
check_true("and lifting is in the future", ro._halts["all"][1] > time.time())

print("\nTHE HALT THAT MUST STILL BITE: oracle/exchange mismatch")
# This is not a P&L rule. It means our read of who won disagrees with the
# exchange's, so every downstream number is suspect and every strategy must
# stop -- in paper as much as in live, shadow flag or not.
rmm, ledm = rm(pnl=0.0, mismatches=1)
rmm._check()
check_true("a mismatch halts paper even with the shadow stop on",
           rmm.halted("preopen"))
check_true("and it is sticky", rmm._halts["all"][1] is None)
check_true("recorded as a real HALT", "HALT" in ledm.kinds(),
           f"({ledm.kinds()})")
# and a mismatch on a losing day must not be downgraded to a shadow row
rmb, ledb = rm(pnl=-308.68, mismatches=1)
rmb._check()
check_true("a mismatch on a losing day still halts",
           rmb.halted("preopen"))

print("\nan unacknowledged live incident halts live and only live")
rli, ledli = rm(mode="live", pnl=0.0)
rli.needs = 123.0
rli.ledger.needs_ack = lambda: 1786500000.0
rli._check()
check_true("live halts on an unacked incident", rli.halted("preopen"))
check_true("and it is sticky", rli._halts["all"][1] is None)
rlp, _ = rm(mode="paper", pnl=0.0)
rlp.ledger.needs_ack = lambda: 1786500000.0
rlp._check()
check_true("paper ignores live incident events", not rlp.halted("preopen"))

print("\nthe fast-bleed breaker: live halts preopen, paper records")
# floor = 0.6 * max_daily_loss(250) = 150; a -160 trailing window trips it
rt, ledt = rm(mode="live")
rt.ledger._trail = (-160.0, 20)
rt._check()
check_true("live halts the preopen scope", rt.halted("preopen"))
check_true("but not the snipe scope", not rt.halted("snipe"))
check_true("sticky: a human must restart", rt._halts["preopen"][1] is None)
rp, ledp = rm(mode="paper")
rp.ledger._trail = (-160.0, 20)
rp._check()
check_true("paper does NOT halt", not rp.halted("preopen"))
check("but the trip is on the record", ledp.kinds(), ["SHADOW_TRAIL"])
check_true("with the numbers that tripped it",
           "-160.00" in ledp.events[0][1], f"({ledp.events[0][1]})")
for _ in range(50):
    rp._check()
check("recorded once per day, not once per check", len(ledp.events), 1)
rs, leds = rm(mode="live")
rs.ledger._trail = (-160.0, 19)
rs._check()
check_true("a short window (n<N) never trips, even live",
           not rs.halted("preopen"))
rg2, ledg2 = rm(mode="live")
rg2.ledger._trail = (-140.0, 20)
rg2._check()
check_true("above the floor never trips", not rg2.halted("preopen"))

print("\na profitable day does nothing at all")
rg, ledg = rm(pnl=+500.0)
rg._check()
check_true("no halt", not rg.halted("preopen"))
check("no rows", ledg.events, [])

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("ALL PASS")
