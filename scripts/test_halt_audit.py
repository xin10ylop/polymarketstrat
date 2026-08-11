"""halt_audit pairs halts with how they ended, and cannot overstate the dark.

  venv/bin/python scripts/test_halt_audit.py

WHY THIS EXISTS. The first version of halt_audit ran every halt with no
recorded lift forward to the present instant and reported 1,022 dark hours
and 12,270 lost windows across the fleet. Retired snipe-* units, whose
services were removed in early August, accounted for nearly all of it — time
in which those bots did not exist. The real figure across the two live bots
was 142 windows. That is a two-order-of-magnitude error in a number meant to
decide how much to distrust the ledgers, and it was produced by a tool
written to measure trustworthiness.

Two rules keep it honest, and both are tested here:

  A HALT ENDS WHERE THE EVIDENCE ENDS. A halt cleared by a RESTART writes no
  HALT_LIFTED — the in-memory halt set is simply gone — so the end marker is
  the first ledger row that a halted bot could not have written.

  NOT EVERY ROW COUNTS AS REVIVAL. The settlement healer writes while a bot
  is halted. Counting those would end the dark period early and UNDERSTATE
  the censoring, which is the direction that quietly makes a bad ledger look
  usable. The whitelist errs the other way on purpose.

Pure in-memory sqlite: no files, no services, no bot.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.halt_audit import spans                # noqa: E402

FAILED = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        FAILED.append(name)


def db(rows):
    d = sqlite3.connect(":memory:")
    d.execute("CREATE TABLE events(ts REAL, kind TEXT, detail TEXT)")
    d.executemany("INSERT INTO events VALUES(?,?,?)", rows)
    return d


print("how a halt ended is recorded, not assumed")
per, sh = spans(db([(100, "HALT", "daily"), (400, "HALT_LIFTED", "daily")]))
check("the breaker lifting it closes the period",
      (per[0][0], per[0][1], per[0][3]), (100, 400, "lifted"))

# THE CASE THAT WAS WRONG IN PRODUCTION: btc 5m halted 07:15, never lifted,
# but the shadow-stop deploy restarted it and it traded again from 12:17.
# Without this rule the tool reports it dark and grows the figure hourly.
per, sh = spans(db([(100, "HALT", "daily"), (500, "SHADOW_HALT", "daily")]))
check("a restart-cleared halt ends at the revival",
      (per[0][1], per[0][3]), (500, "restart"))
check("and the shadow row still reports as a shadow", len(sh), 1)

per, _ = spans(db([(100, "HALT", "daily"), (700, "preopen_entry", '{"w":1}')]))
check("a trade after the halt ends it too",
      (per[0][1], per[0][3]), (700, "restart"))

print("\nand what does NOT count as coming back")
per, _ = spans(db([(100, "HALT", "daily"), (700, "late_settlement", "w1 up")]))
check("a settlement-healer row is not revival", (per[0][1], per[0][3]),
      (None, None))
per, _ = spans(db([(50, "preopen_entry", "x"), (100, "HALT", "daily")]))
check("a trade BEFORE the halt is not revival", per[0][1], None)

print("\none dark period, however many times the breaker re-fires")
# every restart re-derives the same losing day and re-halts; three HALT rows
# are one outage, not three
per, _ = spans(db([(100, "HALT", "daily"), (200, "HALT", "daily"),
                   (300, "HALT", "daily")]))
check("repeated HALT rows collapse to one period", len(per), 1)
check("starting at the first", per[0][0], 100)

print("\nnothing invented from a clean ledger")
check("no halts, no shadows", spans(db([(100, "start", "")])), ([], []))

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("ALL PASS")
