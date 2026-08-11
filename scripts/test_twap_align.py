"""twap_align finds a real misalignment, and refuses to invent one.

  venv/bin/python scripts/test_twap_align.py

WHY BOTH HALVES MATTER. This tool scans boundary offsets and window lengths
against official outcomes, which is exactly the shape of exercise that
manufactures findings — this project has already retracted a vol-scaled gate
twice for it. A scanner that only ever says "yes" is worse than no scanner,
because its answer arrives attached to a code change in the settlement path.

So two fixtures:
  PLANTED   settle every window by a TWAP taken 2s later than the bot looks.
            The scan must recover +2 and it must survive out of sample.
  CLEAN     settle by exactly the alignment the bot already uses. The scan
            must report that the current setting stands, despite having 44
            cells to fish in.

The clean case needs the same near-tie population as the planted one, or it
proves nothing: a surface of easy windows is flat for every alignment and
would "pass" without the tool having any power at all.

Pure synthetic prices, in-memory sqlite mirrored to a temp dir. No network.
"""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import math
import random

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W, N, NW = 300, 30, 260
FAILED = []


def check_true(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name} {detail}")
    if not cond:
        FAILED.append(name)


def build(tmp, truth_shift, seed):
    """A grid whose windows are mostly near-ties, settled at truth_shift."""
    random.seed(seed)
    start = 1786000000 - (1786000000 % W)
    px, walk = {}, 0.0
    for s in range(start - N - 10, start + (NW + 1) * W + N + 10):
        walk += random.gauss(0, 0.004)          # slow drift -> many near-ties
        # a 37s period is coprime enough with 30 that the 30s mean depends on
        # phase, which is what gives a 2-second shift anything to move
        px[s] = round(50000.0 + walk + 5.0 * math.sin(2 * math.pi * s / 37.0), 5)

    def mean(t, n):
        return sum(px[x] for x in range(t - n, t) if x in px) / n

    rows = []
    for i in range(NW):
        w = start + i * W
        k = mean(w + truth_shift, N)
        c = mean(w + W + truth_shift, N)
        rows.append((w, "up" if c >= k else "down", None, 0, 0))
    gd = os.path.join(tmp, "bot", "data", "twapcal")
    ld = os.path.join(tmp, "bot", "data", "preopen-btc")
    os.makedirs(gd), os.makedirs(ld)
    g = sqlite3.connect(os.path.join(gd, "btc_1s.db"))
    g.execute("CREATE TABLE px(ts INTEGER PRIMARY KEY, v REAL)")
    g.executemany("INSERT INTO px VALUES(?,?)", sorted(px.items()))
    g.commit()
    d = sqlite3.connect(os.path.join(ld, "paper.db"))
    d.execute("CREATE TABLE settlements(wts INTEGER PRIMARY KEY, winner TEXT,"
              " oracle_winner TEXT, mismatch INTEGER, ts REAL)")
    d.executemany("INSERT INTO settlements VALUES(?,?,?,?,?)", rows)
    d.commit()


def run(truth_shift, seed):
    tmp = tempfile.mkdtemp()
    try:
        build(tmp, truth_shift, seed)
        env = dict(os.environ, PYTHONPATH=ROOT)
        r = subprocess.run([sys.executable, "-m", "bot.twap_align"],
                           cwd=tmp, env=env, capture_output=True, text=True,
                           timeout=300)
        return r.stdout + r.stderr
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


print("a planted +2s misalignment is recovered and survives the split")
out = run(2, 3)
check_true("the scan names shift +2", "shift +2s, n=30" in out,
           "" if "shift +2s, n=30" in out else "(not found in output)")
check_true("and calls it real rather than fitted",
           "SURVIVES THE SPLIT" in out)
check_true("it ran on the near-tie population, not everything",
           "where alignment can matter at all" in out)

print("\nwith NO misalignment it does not invent one, despite 45 cells")
out = run(0, 3)
check_true("the current alignment is reported as standing",
           ("CURRENT ALIGNMENT ALREADY WINS" in out
            or "NOTHING BEATS THE CURRENT ALIGNMENT" in out),
           "" if ("CURRENT ALIGNMENT ALREADY WINS" in out
                  or "NOTHING BEATS THE CURRENT ALIGNMENT" in out)
           else "(it claimed a discovery)")
check_true("and it does NOT claim a surviving alternative",
           "SURVIVES THE SPLIT" not in out)
# the clean run must still have had power, or passing meant nothing
check_true("the clean case still had a near-tie population to fish in",
           "where alignment can matter at all" in out
           and "Too few to scan" not in out)

print("\nthe tie-band sweep prices both costs, not just the one we want")
# A band is only defensible if the table shows what it BLINDS as well as what
# it silences. audit D10 found 2.0bp switched the tripwire off on 34-71% of
# windows; a sweep that reported only "no false alarms" would recommend 2.0.
check_true("the sweep reports the blind cost alongside the errors",
           "blind%" in out and "errors left" in out)
check_true("and marks the first band that clears the false alarms",
           "first band with NO false alarms" in out
           or "NO BAND CLEARS THE ERRORS" in out)
check_true("the current setting is marked so the change is visible",
           "<- current" in out)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("ALL PASS")
