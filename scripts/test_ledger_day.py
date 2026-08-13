"""Ledger accounting fixes from the 2026-08-13 audit, pinned.

  venv/bin/python scripts/test_ledger_day.py

Three behaviors, each of which was wrong or absent before the audit:

  SETTLE-DAY P&L. realized_pnl_today filtered on FILL ts while pnl is
  written at settlement, so a 23:58 fill settling 00:06 landed in
  yesterday's bucket — whose breaker never runs again. A -$300 boundary
  loss produced no halt and no shadow event at all.

  has_fill. The persistent memory behind the in-memory `done` sets: a crash
  plus fast restart re-entered an already-filled window.

  needs_ack. Live sticky halts lived only in RiskManager memory; the ledger
  events now re-arm them until a human writes the ack row.

Plus the live _parse_fill non-dict rule: an abnormal body from a POST that
did not raise books as AMBIGUOUS (-1), never as a clean miss.
"""
import os
import shutil
import sys
import tempfile
import time
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import CFG                      # noqa: E402
from bot.engine.ledger import Ledger            # noqa: E402
from bot.engine.live import LiveExecutor        # noqa: E402

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
    led = Ledger(replace(CFG, data_dir=tmp))
    day0 = time.time() - time.time() % 86400

    print("P&L lands in the day the money moved, not the day of the fill")
    # a fill just before midnight, settled just after: must count TODAY
    led.db.execute(
        "INSERT INTO fills VALUES(1, ?, 1000, 'preopen', 't', 0.52, 100, "
        "1.0, 0, 0.0, -300.0)", (day0 - 120,))
    led.db.execute("INSERT INTO settlements VALUES(1000, 'down', 'down', 0, ?)",
                   (day0 + 360,))
    # a fill fully settled yesterday: must NOT count today
    led.db.execute(
        "INSERT INTO fills VALUES(2, ?, 2000, 'preopen', 't', 0.52, 100, "
        "1.0, 0, 1.0, 50.0)", (day0 - 7200,))
    led.db.execute("INSERT INTO settlements VALUES(2000, 'up', 'up', 0, ?)",
                   (day0 - 6800,))
    # a fill today, not yet settled: pnl NULL contributes nothing
    led.db.execute(
        "INSERT INTO fills VALUES(3, ?, 3000, 'preopen', 't', 0.52, 100, "
        "1.0, 0, NULL, NULL)", (day0 + 600,))
    led.db.commit()
    check("the boundary loss is visible to TODAY's breaker",
          led.realized_pnl_today(), -300.0)

    print("\nhas_fill is the restart-proof memory")
    check_true("a filled window is remembered", led.has_fill(1000, "preopen"))
    check_true("strategy-scoped", not led.has_fill(1000, "snipe"))
    check_true("an unfilled window is not", not led.has_fill(9999, "preopen"))

    print("\nneeds_ack survives restarts until a human writes the ack")
    check("no incidents -> nothing to ack", led.needs_ack(), None)
    led.event("live_unconfirmed", "w123 resp=garbage")
    check_true("an incident demands an ack", led.needs_ack() is not None)
    led.event("ack", "reconciled by a human")
    check("the ack releases it", led.needs_ack(), None)
    led.event("live_error", "w456 take: boom")
    check_true("a NEW incident re-arms past the old ack",
               led.needs_ack() is not None)

    print("\nlive _parse_fill: an abnormal body is ambiguous, never a miss")
    sh, px = LiveExecutor._parse_fill("matched", 0.56)
    check("non-dict response books as ambiguous (-1)", sh, -1.0)
    sh, _ = LiveExecutor._parse_fill(
        {"takingAmount": "10000000", "makingAmount": "5300000"}, 0.56)
    check("a real matched body still parses shares", sh, 10.0)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("ALL PASS")
