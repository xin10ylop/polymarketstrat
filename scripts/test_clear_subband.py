"""The mismatch clearer clears what it should and REFUSES what it should not.

  venv/bin/python scripts/test_clear_subband.py

This script edits the one flag that stops the fleet trading on a suspected
oracle defect. Its value is entirely in what it declines to do, so the
refusal paths are what is tested hardest:

  inside the band          -> cleared, with an audit row carrying the margin
  outside the band         -> REFUSED, non-zero exit so a chained restart
                              cannot run behind it
  not re-priceable         -> REFUSED. "cannot check" is not "fine"
  one bad row in a batch   -> the whole run exits non-zero, even though the
                              good rows were cleared

Dry run must never write. Winner and oracle_winner must never change — only
the mismatch flag.
"""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W, N = 300, 30
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


def build(tmp, windows):
    """windows: [(wts, close_over_open_bp or None-for-no-grid)]"""
    gd = os.path.join(tmp, "bot", "data", "twapcal")
    ld = os.path.join(tmp, "bot", "data", "preopen-btc")
    os.makedirs(gd), os.makedirs(ld)
    px = {}
    for wts, bp in windows:
        if bp is None:
            continue                      # leave the grid empty here
        for s in range(wts - N, wts):
            px[s] = 50000.0
        for s in range(wts + W - N, wts + W):
            px[s] = 50000.0 * (1.0 + bp * 1e-4)
    g = sqlite3.connect(os.path.join(gd, "btc_1s.db"))
    g.execute("CREATE TABLE px(ts INTEGER PRIMARY KEY, v REAL)")
    g.executemany("INSERT INTO px VALUES(?,?)", sorted(px.items()))
    g.commit()
    d = sqlite3.connect(os.path.join(ld, "paper.db"))
    d.execute("CREATE TABLE settlements(wts INTEGER PRIMARY KEY, winner TEXT,"
              " oracle_winner TEXT, mismatch INTEGER, ts REAL)")
    d.execute("CREATE TABLE events(ts REAL, kind TEXT, detail TEXT)")
    d.executemany("INSERT INTO settlements VALUES(?,?,?,?,?)",
                  [(w, "up", "down", 1, 0) for w, _ in windows])
    d.commit()
    return os.path.join(ld, "paper.db")


def run(tmp, apply_it, band="0.6"):
    cmd = [sys.executable, os.path.join(ROOT, "scripts",
                                        "clear_subband_mismatches.py")]
    if apply_it:
        cmd.append("--apply")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                       env=dict(os.environ, REPO_ROOT=tmp, PYTHONPATH=ROOT,
                                BAND_BP=band))
    return r.returncode, r.stdout + r.stderr


def state(path):
    d = sqlite3.connect(path)
    flags = dict(d.execute("SELECT wts, mismatch FROM settlements"))
    winners = dict(d.execute("SELECT wts, winner || '/' || oracle_winner "
                             "FROM settlements"))
    kinds = [k for (k,) in d.execute("SELECT kind FROM events")]
    return flags, winners, kinds


BASE = 1786000000 - (1786000000 % W)

print("a dry run reports but never writes")
tmp = tempfile.mkdtemp()
try:
    p = build(tmp, [(BASE, -0.30)])
    rc, out = run(tmp, apply_it=False)
    flags, _, kinds = state(p)
    check("dry run exits clean", rc, 0)
    check("the flag is untouched", flags[BASE], 1)
    check("and nothing was written to events", kinds, [])
    check_true("it says what it would do", "would clear" in out)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\ninside the band: cleared, with the margin on the record")
tmp = tempfile.mkdtemp()
try:
    p = build(tmp, [(BASE, -0.30)])
    rc, out = run(tmp, apply_it=True)
    flags, winners, kinds = state(p)
    check("exits clean", rc, 0)
    check("the flag is cleared", flags[BASE], 0)
    check("an audit row was written", kinds, ["mismatch_cleared"])
    check("the recorded winners are NOT touched", winners[BASE], "up/down")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\noutside the band: refused, and the exit code stops a chained restart")
tmp = tempfile.mkdtemp()
try:
    p = build(tmp, [(BASE, -1.50)])
    rc, out = run(tmp, apply_it=True)
    flags, _, kinds = state(p)
    check("exits NON-ZERO", rc, 1)
    check("the flag is left standing", flags[BASE], 1)
    check("nothing was written", kinds, [])
    check_true("and it says why", "OUTSIDE THE BAND" in out)
    check_true("it warns against widening the band to hide it",
               "Do NOT widen the band" in out)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\na window the archive cannot re-price is refused, not assumed fine")
tmp = tempfile.mkdtemp()
try:
    p = build(tmp, [(BASE, None)])
    rc, out = run(tmp, apply_it=True)
    flags, _, _ = state(p)
    check("exits NON-ZERO", rc, 1)
    check("the flag is left standing", flags[BASE], 1)
    check_true("and it says so", "NOT RE-PRICEABLE" in out)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\none bad row in a batch fails the whole run")
# The dangerous shape: a genuine defect riding along with explainable rows,
# where a zero exit would let a chained restart resume trading on it.
tmp = tempfile.mkdtemp()
try:
    p = build(tmp, [(BASE, -0.30), (BASE + 10 * W, -1.50)])
    rc, out = run(tmp, apply_it=True)
    flags, _, _ = state(p)
    check("exits NON-ZERO despite the good row", rc, 1)
    check("the explainable row was still cleared", flags[BASE], 0)
    check("the genuine one was NOT", flags[BASE + 10 * W], 1)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\nthe band is what decides it, not the script")
tmp = tempfile.mkdtemp()
try:
    p = build(tmp, [(BASE, -0.45)])
    rc, _ = run(tmp, apply_it=True, band="0.4")
    check("0.45bp is refused under a 0.4bp band", rc, 1)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
tmp = tempfile.mkdtemp()
try:
    p = build(tmp, [(BASE, -0.45)])
    rc, _ = run(tmp, apply_it=True, band="0.6")
    check("and cleared under a 0.6bp band", rc, 0)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("ALL PASS")
