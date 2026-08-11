"""How much of each paper bot's life was spent halted, and what that censored.

  venv/bin/python -m bot.halt_audit

WHY THIS MATTERS MORE THAN IT SOUNDS. A halted bot records nothing, and until
2026-08-11 it also SAID nothing — no counter, no log line. So the gap does not
appear as missing data; it appears as a shorter, tidier history. That is the
dangerous shape, because the gaps are not random. The breaker fires precisely
when the day is going badly, so every halt truncates a losing run while every
winning run records in full.

Two consequences, both of which make the ledgers read better than the truth:

  WIN RATE IS BIASED UP. The losses that would have followed the breaker are
  absent from the denominator as well as the numerator.

  DRAWDOWN IS A FLOOR, NOT A MEASUREMENT. "-$580 from peak" means the bot
  stopped losing because it stopped trading, not because the strategy turned.
  The real path is unobserved.

Neither can be repaired from the ledger — the windows were never traded. They
CAN be priced from the tape archive (bot/scalp_backtest.py), which is why the
tape backfill matters. What this tool does is tell you how much to distrust,
and which intervals to re-price.

Read-only. Reports every paper-* and preopen-* ledger it finds.
"""
import glob
import os
import sqlite3
import time

# window seconds by family, for turning dark time into windows lost
FAMILY_SECS = {"5m": 300, "15m": 900, "1h": 3600}


def family_of(path):
    """15m units carry it in the directory name; everything else is 5m."""
    return "15m" if "-15m" in path else "5m"


def spans(db):
    """[(start, end_or_None, reason)] — HALT rows paired with their lifts.

    HALT_LIFTED carries the same reason text as its HALT, but the ledger does
    not link them by id, so this pairs strictly in time order and treats an
    unmatched HALT as still in force. That is the conservative reading: it
    can overstate a halt that was lifted by a restart rather than by the
    breaker, never understate one.
    """
    rows = list(db.execute(
        "SELECT ts, kind, detail FROM events "
        "WHERE kind IN ('HALT','HALT_LIFTED','SHADOW_HALT') ORDER BY ts"))
    out, open_at, open_why = [], None, ""
    shadows = []
    for ts, kind, detail in rows:
        if kind == "SHADOW_HALT":
            shadows.append((ts, detail))
        elif kind == "HALT":
            if open_at is None:                  # a re-halt after a restart
                open_at, open_why = ts, detail   # is the same dark period
        elif kind == "HALT_LIFTED" and open_at is not None:
            out.append((open_at, ts, open_why))
            open_at, open_why = None, ""
    if open_at is not None:
        out.append((open_at, None, open_why))
    return out, shadows


def main():
    paths = sorted(glob.glob("bot/data/*/paper.db"))
    if not paths:
        raise SystemExit("no ledgers under bot/data/*/paper.db")
    now = time.time()
    print(f"{'ledger':<22} {'dark':>9} {'of life':>8} {'windows':>8}  periods")
    print("-" * 78)
    grand_dark = grand_win = 0
    for p in paths:
        name = os.path.basename(os.path.dirname(p))
        try:
            db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            per, shadows = spans(db)
            first = db.execute("SELECT MIN(ts) FROM events").fetchone()[0]
        except sqlite3.Error as e:
            print(f"{name:<22} unreadable ({str(e)[:40]})")
            continue
        if first is None:
            continue
        life = max(now - first, 1.0)
        T = FAMILY_SECS[family_of(name)]
        dark = sum((e or now) - s for s, e, _ in per)
        lost = int(dark // T)
        grand_dark += dark
        grand_win += lost
        mark = "" if not any(e is None for _, e, _ in per) else "  <- STILL DARK"
        if per or shadows:
            print(f"{name:<22} {dark/3600:>8.2f}h {100*dark/life:>7.1f}% "
                  f"{lost:>8}  {len(per)} halt(s), "
                  f"{len(shadows)} shadow{mark}")
            for s, e, why in per:
                span = (e or now) - s
                print(f"  {'':<20} {time.strftime('%m-%d %H:%M', time.gmtime(s))}"
                      f" -> {time.strftime('%m-%d %H:%M', time.gmtime(e)) if e else 'now':<11}"
                      f" {span/3600:>5.2f}h  {why[:44]}")
            for s, why in shadows:
                print(f"  {'':<20} {time.strftime('%m-%d %H:%M', time.gmtime(s))}"
                      f"    SHADOW (kept trading) {why[:36]}")
        else:
            print(f"{name:<22} {'-':>9} {'-':>8} {'-':>8}  clean")
    print("-" * 78)
    print(f"{'TOTAL':<22} {grand_dark/3600:>8.2f}h {'':>8} {grand_win:>8} "
          f"windows never evaluated\n")
    if grand_win:
        print("THESE WINDOWS ARE NOT RECOVERABLE FROM THE LEDGER — the bot never")
        print("traded them. They ARE priceable from the tape archive, and they")
        print("are exactly the intervals where doing so matters, because a halt")
        print("only ever fires on a bad day. Until that is done, treat every")
        print("live win rate above as an UPPER bound and every drawdown as a")
        print("LOWER bound.")
    else:
        print("No halts recorded: the ledgers above are uncensored.")


if __name__ == "__main__":
    main()
