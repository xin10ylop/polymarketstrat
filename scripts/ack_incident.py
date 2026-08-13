"""Acknowledge a live incident after reconciling against the exchange.

  venv/bin/python scripts/ack_incident.py /path/to/live/data/dir --i-reconciled

WHY THIS EXISTS. Live sticky halts (ambiguous POST, unexpected order error,
balance-reconciler breach) used to live only in RiskManager memory: a restart
— including a systemd automatic one — cleared a halt whose own text said
"reconcile against the exchange before restarting" (audit 2026-08-13). The
triggering events now persist in the ledger and RiskManager re-halts on any
of them newer than the last 'ack' row. This script writes that row.

It is deliberately annoying: it requires the flag, prints exactly what it is
acknowledging, and refuses if there is nothing to acknowledge — an ack
written "just in case" would pre-forgive the NEXT incident.
"""
import os
import sqlite3
import sys
import time


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1 or "--i-reconciled" not in sys.argv:
        raise SystemExit(
            "usage: ack_incident.py <live BOT_DATA_DIR> --i-reconciled\n"
            "Run this ONLY after comparing the ledger's fills against the\n"
            "exchange's actual positions and balance. The flag is your\n"
            "signature that a human did that.")
    path = os.path.join(args[0], "paper.db")
    if not os.path.exists(path):
        raise SystemExit(f"no ledger at {path}")
    db = sqlite3.connect(path)
    rows = db.execute(
        "SELECT ts, kind, detail FROM events WHERE kind IN "
        "('live_unconfirmed','live_error','reconciler_breach') AND ts > "
        "(SELECT COALESCE(MAX(ts),0) FROM events WHERE kind='ack') "
        "ORDER BY ts").fetchall()
    if not rows:
        raise SystemExit("nothing to acknowledge — no unacked incident "
                         "exists, so no ack is written.")
    print(f"ACKNOWLEDGING {len(rows)} incident(s):")
    for ts, kind, detail in rows:
        print(f"  {time.strftime('%m-%d %H:%M', time.gmtime(ts))} "
              f"{kind}: {detail[:100]}")
    db.execute("INSERT INTO events VALUES(?,?,?)",
               (time.time(), "ack",
                f"{len(rows)} incident(s) reconciled by a human"))
    db.commit()
    print("\nack written. The halt lifts on the bot's next risk check "
          "(<=5s) or restart.")


if __name__ == "__main__":
    main()
