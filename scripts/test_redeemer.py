"""The redemption path (launch blocker #1), pinned before it ever runs.

  venv/bin/python scripts/test_redeemer.py

What must be true of code that will move real money:

  the work list   only WINNING settled windows old enough for finality;
                  losers, unsettled fills and fresh settlements stay out.
  dry mode        finds everything, sends NOTHING, records each window
                  once (not once per pass) — the shadow phase reads
                  these events against the venue UI before going wet.
  wet mode        sends once per window, records the tx, and never
                  re-redeems a recorded window.
  failure         a failed send is recorded and the window STAYS pending
                  — retried forever, never dropped, never crashing.

The chain call itself is a stub here; its first real execution happens
in the shadow phase on the live box, deliberately.
"""
import asyncio
import logging
import os
import shutil
import sys
import tempfile
import time
from dataclasses import replace

# the failure-path tests trip the code's own ERROR logging by design; the
# assertions read the LEDGER events, so silencing costs no coverage
logging.getLogger("redeem").setLevel(logging.CRITICAL)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import CFG                      # noqa: E402
from bot.engine.ledger import Ledger            # noqa: E402
from bot.engine.redeem import Redeemer          # noqa: E402

FAILED = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        FAILED.append(name)


def check_true(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name} {detail}")
    if not cond:
        FAILED.append(name)


tmp = tempfile.mkdtemp()
try:
    now = time.time()
    led = Ledger(replace(CFG, data_dir=tmp))

    def plant(wts, settle, pnl, settle_age, sz=100.0):
        led.db.execute(
            "INSERT INTO fills VALUES(1, ?, ?, 'preopen', 't', 0.52, ?, "
            "0.0, 0, ?, ?)", (wts - 3, wts, sz, settle, pnl))
        led.db.execute("INSERT INTO settlements VALUES(?, 'up', 'up', 0, ?)",
                       (wts, now - settle_age))
        led.db.commit()

    plant(1000, 1.0, 48.0, 600)     # winner, old enough      -> pending
    plant(2000, 0.0, -52.0, 600)    # loser                   -> never
    plant(3000, 1.0, 48.0, 10)      # winner, settled 10s ago -> too fresh
    led.db.execute(  # unsettled fill (pnl NULL) -> not redeemable yet
        "INSERT INTO fills VALUES(1, ?, 4000, 'preopen', 't', 0.52, 100, "
        "0.0, 0, NULL, NULL)", (now - 60,))
    led.db.commit()

    print("the work list: winners past finality, nothing else")
    check("only w1000 qualifies",
          led.winning_settled_windows(120.0), [(1000, 100.0)])

    class TestRedeemer(Redeemer):
        """conditionId lookup and the chain call stubbed; sends counted."""
        sent = []

        async def _condition_id(self, wts):
            return f"0xcid{wts}"

        def _send_redeem(self, condition_id):
            self.sent.append(condition_id)
            if condition_id == "0xcidBOOM":
                raise RuntimeError("rpc down")
            return "0xtx"

    print("\ndry mode: finds, records once, sends nothing")
    cfg_dry = replace(CFG, mode="live", redeem_dry=True, live_shadow=False,
                      redeem_min_age_s=120.0, redeem_max_batch=5)
    rd = TestRedeemer(cfg_dry, led)
    asyncio.run(rd.once())
    asyncio.run(rd.once())
    dry_events = led.db.execute(
        "SELECT COUNT(*) FROM events WHERE kind='redeem_dry'").fetchone()[0]
    check("one dry record across two passes", dry_events, 1)
    check("nothing was sent", TestRedeemer.sent, [])
    check("dry keeps the window OUT of the next pass", rd.pending(), [])

    print("\nwet mode: sends once, records the tx, never repeats")
    led.db.execute("DELETE FROM events WHERE kind='redeem_dry'")
    led.db.commit()
    cfg_wet = replace(cfg_dry, redeem_dry=False)
    rw = TestRedeemer(cfg_wet, led)
    asyncio.run(rw.once())
    check("exactly one send", TestRedeemer.sent, ["0xcid1000"])
    redeemed = led.db.execute(
        "SELECT detail FROM events WHERE kind='redeemed'").fetchall()
    check_true("the tx is on the record",
               len(redeemed) == 1 and "w1000" in redeemed[0][0]
               and "0xtx" in redeemed[0][0], f"({redeemed})")
    asyncio.run(rw.once())
    check("a recorded window is never re-sent",
          TestRedeemer.sent, ["0xcid1000"])

    print("\nfailure: recorded, retried, never dropped")
    plant(5000, 1.0, 48.0, 600)

    class BoomRedeemer(TestRedeemer):
        sent = []

        async def _condition_id(self, wts):
            return "0xcidBOOM"

    rb = BoomRedeemer(cfg_wet, led)
    asyncio.run(rb.once())
    errs = led.db.execute(
        "SELECT COUNT(*) FROM events WHERE kind='redeem_error'").fetchone()[0]
    check("the failure is on the record", errs, 1)
    check("the window stays pending", rb.pending(), [(5000, 100.0)])
    asyncio.run(rb.once())
    check("and is retried on the next pass", len(BoomRedeemer.sent), 2)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("ALL PASS")
