"""Paper-trading report: python3 -m bot.report [days]"""
import os
import sqlite3
import sys
import time

from bot.config import CFG


def main(days=7):
    db = sqlite3.connect(os.path.join(CFG.data_dir, "paper.db"))
    t0 = time.time() - days * 86400
    print(f"=== paper results, last {days}d ===")
    for row in db.execute(
            "SELECT strategy, COUNT(*), SUM(size), SUM(pnl), SUM(fee), "
            "AVG(CASE WHEN pnl>0 THEN 1.0 ELSE 0.0 END) "
            "FROM fills WHERE ts>=? AND pnl IS NOT NULL GROUP BY strategy", (t0,)):
        strat, n, sz, pnl, fee, wr = row
        print(f"{strat:8s} fills={n:5d} shares={sz:9.0f} pnl=${pnl:9.2f} "
              f"fees=${fee:7.2f} win={wr:5.1%} ev={100*pnl/sz if sz else 0:.3f}c/sh")
    print("\nby day:")
    for d, pnl, n in db.execute(
            "SELECT date(ts,'unixepoch'), SUM(pnl), COUNT(*) FROM fills "
            "WHERE ts>=? AND pnl IS NOT NULL GROUP BY 1 ORDER BY 1", (t0,)):
        print(f"  {d}  ${pnl:9.2f}  ({n} fills)")
    mism = db.execute("SELECT SUM(mismatch), COUNT(*) FROM settlements").fetchone()
    unchecked = db.execute(
        "SELECT COUNT(*) FROM settlements WHERE winner IS NOT NULL "
        "AND oracle_winner IS NULL").fetchone()[0]
    print(f"\nsettlements={mism[1]}, oracle/exchange mismatches={mism[0] or 0}"
          f" (cross-check missing on {unchecked})")
    unmarked = db.execute(
        "SELECT COUNT(*) FROM fills WHERE pnl IS NULL AND ts < ?",
        (time.time() - 900,)).fetchone()[0]
    if unmarked:
        print(f"WARNING: {unmarked} fills older than 15min still unmarked")
    for ts, kind, detail in db.execute(
            "SELECT ts, kind, detail FROM events WHERE kind IN ('HALT','no_outcome') "
            "AND ts>=? ORDER BY ts DESC LIMIT 10", (t0,)):
        print(f"  event {time.strftime('%m-%d %H:%M', time.gmtime(ts))} {kind}: {detail}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 7)
