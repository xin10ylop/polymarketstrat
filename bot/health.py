"""Is anything silently dead? One command, no waiting.

  venv/bin/python -m bot.health

WHY THIS EXISTS. Twice in two days a recorder reported `active` to systemd
while writing nothing at all. The book recorders threw on every insert for
two hours after a schema change with no migration; the pair recorder logged
one row in nine and a half hours because a lookup was cached at a window
boundary. Both times the loss was only noticed because a downstream report
looked thin, and both times the answer was "restart it and wait a few
hours" — which is the wrong answer twice over, because the waiting only
started once the damage was already done.

`systemctl is-active` cannot catch this. A process stuck in a retry loop,
or writing to a table it cannot insert into, is active and useless. The
only honest test is whether ROWS ARE ARRIVING, so that is what this checks.

Run it after any recorder change, and any time a report looks thinner than
it should. It takes about a second and touches nothing.
"""
import glob
import os
import sqlite3
import time

ROOT = os.environ.get("DATA_ROOT", "bot/data")
WARN_S = float(os.environ.get("WARN_S", "300"))     # quiet this long = suspect
DEAD_S = float(os.environ.get("DEAD_S", "900"))     # quiet this long = dead

# (glob, table, timestamp column, rough rows/hour when healthy)
SOURCES = [
    (f"{ROOT}/bookcal/*_book.db", "book", "ts", 300),
    (f"{ROOT}/twapcal/*_1s.db", "px", "ts", 3400),
    (f"{ROOT}/paircal/*.db", "pair", "ts", 150),
    (f"{ROOT}/*/paper.db", "fills", "ts", None),    # bots: rate varies, no floor
]


def main():
    now = time.time()
    print(f"{'source':>34} {'rows':>9} {'last write':>12} {'last 1h':>8} "
          f"{'verdict':>8}")
    worst = 0
    seen = False
    for pattern, table, tscol, expect in SOURCES:
        for path in sorted(glob.glob(pattern)):
            seen = True
            label = os.path.relpath(path, ROOT)
            try:
                db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                n, last = db.execute(
                    f"SELECT COUNT(*), MAX({tscol}) FROM {table}").fetchone()
                hr = db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {tscol} > ?",
                    (now - 3600,)).fetchone()[0]
            except Exception as e:  # noqa: BLE001
                print(f"{label:>34} {'-':>9} {'UNREADABLE':>12} {'-':>8} "
                      f"{'DEAD':>8}   {str(e)[:44]}")
                worst = max(worst, 2)
                continue
            if not n or last is None:
                # a bot ledger with no fills has never written and never
                # will until it trades — that is idle, not broken. Only a
                # RECORDER (one carrying a rate expectation) is dead here.
                v, lvl = ("DEAD", 2) if expect else ("idle", 0)
                print(f"{label:>34} {n or 0:>9} {'never':>12} {0:>8} {v:>8}")
                worst = max(worst, lvl)
                continue
            age = now - last
            # a bot ledger with no fills is normal; a recorder with no rows
            # is not. Only recorders carry a rate expectation.
            if age > DEAD_S:
                v, lvl = "DEAD", 2
            elif age > WARN_S:
                v, lvl = "STALE", 1
            elif expect and hr < 0.4 * expect:
                v, lvl = "THIN", 1
            else:
                v, lvl = "ok", 0
            if expect is None and age > DEAD_S:
                v, lvl = "idle", 0          # no fills is not a failure
            worst = max(worst, lvl)
            ago = (f"{age:.0f}s" if age < 120 else
                   f"{age/60:.0f}m" if age < 7200 else f"{age/3600:.1f}h")
            print(f"{label:>34} {n:>9} {ago:>12} {hr:>8} {v:>8}"
                  + (f"   (expect ~{expect}/h)" if expect and lvl else ""))
    if not seen:
        print(f"  no databases found under {ROOT}")
        return 2
    print("\nDEAD = nothing written in "
          f"{DEAD_S/60:.0f}m. THIN = writing, but well under the expected")
    print("rate — that is the shape a partly-broken recorder makes.")
    print("A bot ledger showing 'idle' just means no fills, which is normal.")
    if worst == 0:
        print("\nALL SOURCES WRITING.")
    else:
        print("\nSOMETHING IS WRONG ABOVE — check it before trusting any "
              "report that reads these tables.")
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
