"""Acknowledge the mismatches caused by the 2026-08-07 venue rule change.

  venv/bin/python scripts/clear_rule_change_mismatches.py            # dry run
  venv/bin/python scripts/clear_rule_change_mismatches.py --apply

The mismatch halt is sticky by design: RiskManager re-halts whenever the
ledger holds ANY mismatch row, so a restart alone cannot resume trading.
That is correct — a mismatch normally means our oracle read disagrees with
the exchange, i.e. our bug.

The 08-07/08-08 mismatches have a proven different cause: Polymarket moved
the 5m/15m families from spot to a rolling Chainlink TWAP at 08-07 00:00
UTC, and our oracle was still reading spot. The oracle now implements the
new rule (verified 100% on 76 windows, bot/twap_verify.py), so those rows
are explained history, not an open defect.

SAFETY: only rows at or after the cutover can be acknowledged. Anything
earlier is a genuine disagreement and is refused, loudly. The recorded
winner and oracle_winner are never touched — only the `mismatch` flag is
cleared, and every change writes a `mismatch_cleared` audit event.
"""
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 2026-08-07 00:00 UTC — the first window minted under the TWAP rule
CUTOVER = 1786060800
REASON = ("venue resolution-rule change 2026-08-07 (spot -> rolling Chainlink "
          "TWAP); oracle migrated and re-verified")

BOTS = [
    ("snipe", "bot/data/snipe"),
    ("snipe-eth", "bot/data/snipe-eth"),
    ("snipe-sol", "bot/data/snipe-sol"),
    ("snipe-btc15", "bot/data/snipe-btc15"),
    ("snipe-eth15", "bot/data/snipe-eth15"),
    ("snipe-btc1h", "bot/data/snipe-btc1h"),
    ("xwin-btc", "bot/data/xwin-btc"),
]


def main(apply_it):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print(f"{'APPLYING' if apply_it else 'DRY RUN'} — cutover "
          f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(CUTOVER))}\n")
    blocked = total = 0
    for name, rel in BOTS:
        path = os.path.join(root, rel, "paper.db")
        if not os.path.exists(path):
            continue
        db = sqlite3.connect(path)
        rows = db.execute(
            "SELECT wts, winner, oracle_winner FROM settlements WHERE mismatch=1 "
            "ORDER BY wts").fetchall()
        if not rows:
            print(f"{name}: clean")
            continue
        old = [r for r in rows if r[0] < CUTOVER]
        new = [r for r in rows if r[0] >= CUTOVER]
        print(f"{name}: {len(rows)} mismatch rows "
              f"({len(new)} post-cutover, {len(old)} PRE-cutover)")
        for wts, w, ow in rows:
            era = "rule-change" if wts >= CUTOVER else "PRE-CUTOVER — NOT TOUCHED"
            print(f"   w{wts} {time.strftime('%m-%d %H:%M', time.gmtime(wts))} "
                  f"official={w} oracle={ow}   [{era}]")
        blocked += len(old)
        total += len(new)
        if apply_it and new:
            db.execute("UPDATE settlements SET mismatch=0 WHERE mismatch=1 AND wts>=?",
                       (CUTOVER,))
            db.execute("INSERT INTO events VALUES(?,?,?)",
                       (time.time(), "mismatch_cleared",
                        f"{len(new)} windows >= {CUTOVER}: {REASON}"))
            db.commit()
            left = db.execute(
                "SELECT COALESCE(SUM(mismatch),0) FROM settlements").fetchone()[0]
            print(f"   -> cleared {len(new)}; mismatches remaining: {left}"
                  f"{'  (STILL HALTED — pre-cutover rows)' if left else ''}")
    print(f"\n{'cleared' if apply_it else 'would clear'}: {total} rows")
    if blocked:
        print(f"REFUSED to touch {blocked} pre-cutover rows — those are real "
              f"oracle/exchange disagreements and must be investigated, not cleared.")
    if not apply_it:
        print("\nre-run with --apply to write the change")


if __name__ == "__main__":
    main("--apply" in sys.argv)
