"""RULE2 (5m TWAP 30s->60s, 2026-08-14) migration behaviors, pinned.

  venv/bin/python scripts/test_rule2_migration.py

Three things, each of which guards real money or real measurement:

  nsec_at        the era-aware strike lookback: 5m windows before RULE2
                 computed a 30s strike, after it a 60s one; 15m is 60s in
                 both eras. Every full-tape tool prices tilt through this.

  the migration  scripts/clear_twap60_mismatches.py must clear a flag ONLY
                 when the 60s recomputation from the recorder's own grid
                 explains it (AGREE or NEAR-TIE), must refuse a unit with
                 any DISAGREE/NO-GRID window, must never touch flags
                 outside [RULE2, VERIFIED_BY2), and must skip 15m units.

  the tripwire   the stream-name regex the reconciler uses to shout about
                 the NEXT silent rule change.
"""
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILED = []
PY = sys.executable
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULE2 = 1786665600


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        FAILED.append(name)


def check_true(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name} {detail}")
    if not cond:
        FAILED.append(name)


print("nsec_at: the strike lookback follows the era")
for fam, wts, want in (("5m", RULE2 - 300, 30), ("5m", RULE2, 60),
                       ("5m", RULE2 + 300, 60), ("15m", RULE2 - 900, 60),
                       ("15m", RULE2 + 900, 60)):
    got = subprocess.run(
        [PY, "-c", "from bot.twap_verify import nsec_at; "
         f"print(nsec_at({wts}))"],
        cwd=REPO, env=dict(os.environ, FAMILY=fam), capture_output=True,
        text=True).stdout.strip()
    check(f"{fam} window at RULE2{wts - RULE2:+d}s", got, str(want))

print("\nthe reconciler's stream regex reads gamma's only remaining trace")
src = "https://data.chain.link/streams/btc-usd-twap-60s-streams"
check("60s stream parsed", re.search(r"twap-(\d+)s", src).group(1), "60")
check("no twap stream -> no match",
      re.search(r"twap-(\d+)s", "https://binance.com/BTCUSDT"), None)

print("\nthe migration clears only what the new rule explains")
tmp = tempfile.mkdtemp()
A = 100000.0


def grid(coin, spans, root=None):
    p = os.path.join(root or tmp, "twapcal")
    os.makedirs(p, exist_ok=True)
    db = sqlite3.connect(os.path.join(p, f"{coin}_1s.db"))
    db.execute("CREATE TABLE IF NOT EXISTS px(ts INTEGER PRIMARY KEY, v REAL)")
    for a, b, v in spans:
        for s in range(a, b):
            db.execute("INSERT OR REPLACE INTO px VALUES(?,?)", (s, v))
    db.commit()
    db.close()


def ledger(name, flags, root=None):
    d = os.path.join(root or tmp, name)
    os.makedirs(d, exist_ok=True)
    db = sqlite3.connect(os.path.join(d, "paper.db"))
    db.execute("CREATE TABLE settlements(wts INTEGER PRIMARY KEY, "
               "winner TEXT, oracle_winner TEXT, mismatch INTEGER, "
               "settle_ts REAL)")
    db.execute("CREATE TABLE events(ts REAL, kind TEXT, detail TEXT)")
    for wts, winner in flags:
        db.execute("INSERT INTO settlements VALUES(?,?,?,1,?)",
                   (wts, winner, "?", wts + 400))
    db.commit()
    db.close()
    return os.path.join(d, "paper.db")


# windows spaced 900s+ apart: window K's settle region [K+240, K+300) and
# window K+300's strike region [K+240, K+300) are the SAME grid seconds,
# so adjacent-window fixtures would clobber each other's planted values
W1, W2, WPRE = RULE2 + 3000, RULE2 + 4200, RULE2 - 600
W3, W4 = RULE2 + 3600, RULE2 + 4500
# btc: W1 decisive AGREE (+10bp up, exchange up), W2 NEAR-TIE (+0.3bp),
# WPRE out of scope. eth: W3 DISAGREE (60s says up, exchange down),
# W4 NO-GRID.
grid("btc", [(W1 - 60, W1, A), (W1 + 240, W1 + 300, A * 1.001),
             (W2 - 60, W2, A), (W2 + 240, W2 + 300, A * 1.00003)])
grid("eth", [(W3 - 60, W3, A), (W3 + 240, W3 + 300, A * 1.001)])
btc_db = ledger("preopen-btc", [(W1, "up"), (W2, "down"), (WPRE, "up")])
eth_db = ledger("preopen-eth", [(W3, "down"), (W4, "up")])
ledger("preopen-btc15", [(RULE2 + 9000, "up")])

env = dict(os.environ, PAPER_GLOB=os.path.join(tmp, "preopen-*", "paper.db"),
           TWAP_DIR=os.path.join(tmp, "twapcal"))


def flags(path):
    return dict(sqlite3.connect(path).execute(
        "SELECT wts, mismatch FROM settlements"))


try:
    dry = subprocess.run([PY, "scripts/clear_twap60_mismatches.py"],
                         cwd=REPO, env=env, capture_output=True, text=True)
    check("dry run exits 1 (eth unverifiable)", dry.returncode, 1)
    for needle, what in ((" AGREE", "W1 classed AGREE"),
                         ("NEAR-TIE", "W2 classed NEAR-TIE"),
                         ("DISAGREE", "W3 classed DISAGREE"),
                         ("NO-GRID", "W4 classed NO-GRID"),
                         ("15m family", "15m unit skipped"),
                         ("1 flagged OUTSIDE", "WPRE reported out of scope"),
                         ("DRY RUN", "btc offered, not applied"),
                         ("REFUSED", "eth refused")):
        check_true(what, needle in dry.stdout)
    check("dry run changed nothing (btc)", flags(btc_db),
          {W1: 1, W2: 1, WPRE: 1})

    ap = subprocess.run([PY, "scripts/clear_twap60_mismatches.py", "--apply"],
                        cwd=REPO, env=env, capture_output=True, text=True)
    check("apply still exits 1 overall", ap.returncode, 1)
    check("btc: in-scope cleared, out-of-scope kept", flags(btc_db),
          {W1: 0, W2: 0, WPRE: 1})
    check("eth: refused unit untouched", flags(eth_db), {W3: 1, W4: 1})
    ev = sqlite3.connect(btc_db).execute(
        "SELECT COUNT(*) FROM events WHERE kind='rule2_migration'"
    ).fetchone()[0]
    check("the clearing is on the record", ev, 1)
    ev_eth = sqlite3.connect(eth_db).execute(
        "SELECT COUNT(*) FROM events").fetchone()[0]
    check("no record written for the refused unit", ev_eth, 0)

    print("\nthe blackout override is surgical: NO-GRID only, on the record")
    # W5 sits in a total recorder blackout (no grid rows at all); W6 is a
    # decisive AGREE. Accepting W5 by wts must clear the unit and say so.
    ov = os.path.join(tmp, "ov")
    W5, W6, W7 = RULE2 + 6000, RULE2 + 6900, RULE2 + 7800
    grid("btc", [(W6 - 60, W6, A), (W6 + 240, W6 + 300, A * 1.001)],
         root=ov)
    ov_db = ledger("preopen-btc", [(W5, "up"), (W6, "up")], root=ov)
    env_ov = dict(os.environ,
                  PAPER_GLOB=os.path.join(ov, "preopen-*", "paper.db"),
                  TWAP_DIR=os.path.join(ov, "twapcal"),
                  ALLOW_UNVERIFIED_WTS=str(W5))
    r1 = subprocess.run([PY, "scripts/clear_twap60_mismatches.py",
                         "--apply"], cwd=REPO, env=env_ov,
                        capture_output=True, text=True)
    check("an accepted blackout no longer blocks the unit",
          r1.returncode, 0)
    check_true("and is printed as ACCEPTED", "ACCEPTED" in r1.stdout)
    check("both flags cleared", flags(ov_db), {W5: 0, W6: 0})
    det = sqlite3.connect(ov_db).execute(
        "SELECT detail FROM events WHERE kind='rule2_migration'"
    ).fetchone()[0]
    check_true("the acceptance is in the clearing record",
               "accepted-unverifiable" in det and str(W5) in det,
               f"({det})")
    # a DISAGREE can NEVER be overridden, listed or not
    grid("btc", [(W7 - 60, W7, A), (W7 + 240, W7 + 300, A * 1.001)],
         root=ov)
    d2 = sqlite3.connect(ov_db)
    d2.execute("INSERT INTO settlements VALUES(?,?,?,1,?)",
               (W7, "down", "?", W7 + 400))
    d2.commit()
    d2.close()
    env_ov["ALLOW_UNVERIFIED_WTS"] = str(W7)
    r2 = subprocess.run([PY, "scripts/clear_twap60_mismatches.py",
                         "--apply"], cwd=REPO, env=env_ov,
                        capture_output=True, text=True)
    check("a listed DISAGREE still refuses", r2.returncode, 1)
    check("and stays flagged", flags(ov_db)[W7], 1)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("ALL PASS")
