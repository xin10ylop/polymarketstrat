"""Why isn't the snipe trading? — read the per-window eval snapshots.

  BOT_DATA_DIR=bot/data/snipe venv/bin/python -m bot.snap_report [hours]

After the 2026-08-07 TWAP rule change the fv gate stopped selecting: nearly
every tick is confident, so a fill drought is now a BOOK story, not a signal
story. The strategy logs one `eval_snap` per window on the
confident-but-no-trade path; this summarises them.

The decisive number it prints is REFUSED-BUT-PRICED: rejects where a real
ask at or under our cap, with enough size, was sitting there and we skipped
it only because the book had not ticked recently. That distinguishes "the
market repriced away from us" (nothing to buy) from "our own freshness gate
is the binding constraint" (plenty to buy, we self-vetoed).
Read-only.
"""
import json
import os
import sqlite3
import sys
import time

from bot.config import CFG


def main(hours):
    db_path = os.path.join(CFG.data_dir, "paper.db")
    if not os.path.exists(db_path):
        print(f"no ledger at {db_path}")
        return
    db = sqlite3.connect(db_path)
    since = time.time() - hours * 3600
    rows = []
    for (d,) in db.execute("SELECT detail FROM events WHERE kind='eval_snap' "
                           "AND ts > ? ORDER BY ts", (since,)):
        try:
            rows.append(json.loads(d))
        except ValueError:
            pass
    print(f"eval snapshots, last {hours}h: {len(rows)}")
    if not rows:
        print("(none — either the bot is trading, or it never reached a "
              "confident tick; check the STATUS line's evals/near counters)")
        return

    ups = sum(1 for r in rows if r["s"] == "up")
    print(f"\nside split: up {ups} / down {len(rows)-ups}"
          f"   <- a healthy market should be roughly balanced;"
          f" all-one-side means the signal is stuck")

    gaps = sorted(abs(r.get("gap_bp") or 0.0) for r in rows)
    print(f"|margin| bp: p10 {gaps[len(gaps)//10]:.2f} "
          f"p50 {gaps[len(gaps)//2]:.2f} p90 {gaps[min(len(gaps)-1, 9*len(gaps)//10)]:.2f}")

    why = {}
    for r in rows:
        why[r.get("why")] = why.get(r.get("why"), 0) + 1
    print("\nreject reason:")
    for k, v in sorted(why.items(), key=lambda x: -x[1]):
        print(f"  {str(k):12s} {v:5d}  ({100*v/len(rows):4.1f}%)")

    priced = [r for r in rows if r.get("ask") is not None]
    print(f"\nof {len(rows)} snapshots, {len(priced)} had a visible ask")
    if priced:
        asks = sorted(r["ask"] for r in priced)
        print(f"  ask px: p10 {asks[len(asks)//10]:.3f} p50 {asks[len(asks)//2]:.3f} "
              f"p90 {asks[min(len(asks)-1, 9*len(asks)//10)]:.3f}")
        ages = sorted(r.get("age") or 0 for r in priced)
        print(f"  book age s: p50 {ages[len(ages)//2]:.1f} "
              f"p90 {ages[min(len(ages)-1, 9*len(ages)//10)]:.1f}")

    # THE question: would we have traded if only the book had been fresh?
    refused_but_priced = [
        r for r in rows
        if r.get("why") == "stale_book" and r.get("ask") is not None
        and CFG.snipe_price_floor <= r["ask"] <= CFG.snipe_ask_max
        and (r.get("sz") or 0) >= CFG.snipe_min_ask_size
        and (r.get("sz") or 0) <= CFG.snipe_skip_ask_above]
    print(f"\nREFUSED-BUT-PRICED (stale gate only): {len(refused_but_priced)} "
          f"of {len(rows)} ({100*len(refused_but_priced)/len(rows):.1f}%)")
    if refused_but_priced:
        a = sorted(r["ask"] for r in refused_but_priced)
        g = sorted(r.get("age") or 0 for r in refused_but_priced)
        print(f"  those asks: p50 {a[len(a)//2]:.3f} | book age p50 {g[len(g)//2]:.1f}s "
              f"(gate is {CFG.book_max_age_s:.1f}s)")
        print("  -> our freshness gate is the binding constraint, not the market.")
        print("     Do NOT loosen it on this alone: a stale book is exactly where")
        print("     a phantom ask lives. Measure fill-through first.")
    else:
        print("  -> nothing tradeable was refused for staleness; the winning")
        print("     side genuinely has no offer we could hit.")


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 12)
