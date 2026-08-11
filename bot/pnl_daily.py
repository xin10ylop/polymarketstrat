"""Per-day P&L with the PATH, not just the total.

  venv/bin/python -m bot.pnl_daily
  DAYS=5 venv/bin/python -m bot.pnl_daily

WHY THE PATH AND NOT THE TOTAL. The STATUS line carries two different P&L
numbers and it is easy to read one as the other:

  pnl_today=       filtered to ts >= UTC midnight. RESETS AT 00:00 UTC.
  fills={'pnl':}   no time filter at all. LIFETIME, since the ledger began.

Neither shows the shape. On 2026-08-10 I reported btc 5m at +$260, +$311 and
+$163 at three check-ins and called it flat; the actual path had peaked at
+$743.34 and given back $580.18 — 78% of the peak — and none of the three
readings could have shown that. A cumulative number quoted at intervals hides
exactly the thing that decides whether a strategy is sizeable.

So every row here carries the day's P&L, the running total, the running peak
and the drawdown from it. The drawdown column is the one to read.

A CAVEAT THAT APPLIES TO EVERY NUMBER BELOW. These ledgers were censored by
the daily-loss breaker until 2026-08-11: on bad days the bot stopped trading
partway down, so drawdowns here are FLOORS and win rates are CEILINGS. Days
carrying a halt are marked. See LIVE_RUNBOOK 2026-08-11.

Read-only.
"""
import glob
import os
import sqlite3
import time

DAYS = int(os.environ.get("DAYS", "14"))
DAY = 86400


def dark_days(db):
    """UTC days on which this ledger recorded a halt or a shadow trip."""
    out = {}
    try:
        rows = db.execute("SELECT ts, kind FROM events WHERE kind IN "
                          "('HALT','SHADOW_HALT')").fetchall()
    except sqlite3.Error:
        return out
    for ts, kind in rows:
        out.setdefault(int(ts // DAY), set()).add(
            "halt" if kind == "HALT" else "shadow")
    return out


def main():
    paths = sorted(glob.glob("bot/data/preopen-*/paper.db"))
    if not paths:
        raise SystemExit("no pre-open ledgers under bot/data/preopen-*/paper.db")
    today = int(time.time() // DAY)
    for path in paths:
        name = os.path.basename(os.path.dirname(path))
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = db.execute(
                "SELECT CAST(ts/86400 AS INT) d, COUNT(*), "
                "COALESCE(SUM(pnl),0), COALESCE(SUM(fee),0), "
                "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) "
                "FROM fills WHERE pnl IS NOT NULL GROUP BY d ORDER BY d"
            ).fetchall()
        except sqlite3.Error as e:
            print(f"{name}: unreadable ({str(e)[:40]})\n")
            continue
        if not rows:
            print(f"{name}: no settled fills yet\n")
            continue
        marks = dark_days(db)
        # PEAK AND DRAWDOWN OVER THE WHOLE HISTORY, not just the printed tail:
        # a drawdown measured from inside a window is not a drawdown.
        cum = peak = 0.0
        hist = []
        for d, n, pnl, fee, wins in rows:
            cum += pnl
            peak = max(peak, cum)
            # the RUNNING peak, not the final one: a peak column that
            # shows the eventual maximum on an early row is reading the
            # future, and makes that row's drawdown unreadable
            hist.append((d, n, pnl, fee, wins, cum, cum - peak, peak))
        worst = min(h[6] for h in hist)
        print(f"=== {name} ===")
        print(f"{'day (UTC)':<12} {'fills':>6} {'win%':>6} {'day P&L':>10} "
              f"{'cumulative':>11} {'peak':>10} {'drawdown':>10}")
        for d, n, pnl, fee, wins, c, dd, pk in hist[-DAYS:]:
            tag = ""
            if d in marks:
                tag = ("  <- HALTED (censored)" if "halt" in marks[d]
                       else "  <- shadow trip, kept trading")
            day = time.strftime("%m-%d", time.gmtime(d * DAY))
            if d == today:
                day += "*"
            wr = f"{100.0*wins/n:.0f}%" if n else "-"
            print(f"{day:<12} {n:>6} {wr:>6} {pnl:>+10.2f} {c:>+11.2f} "
                  f"{pk:>+10.2f} {dd:>+10.2f}{tag}")
        print(f"{'':<12} {'':>6} {'':>6} {'':>10} {'':>11} "
              f"{'worst DD':>10} {worst:>+10.2f}")
        if peak > 0:
            print(f"lifetime {hist[-1][5]:+.2f}   peak {peak:+.2f}   "
                  f"worst drawdown {worst:+.2f} "
                  f"({100*abs(worst)/peak:.0f}% of peak)")
        else:
            print(f"lifetime {hist[-1][5]:+.2f}   never above water")
        print("* = today, still open\n")

    print("pnl_today= in the STATUS line is the '*' row only and resets at")
    print("00:00 UTC. fills={'pnl':} in the same line is the lifetime column.")
    print("Days marked HALTED are censored: the bot stopped partway down, so")
    print("their drawdown is a floor and their win rate a ceiling.")


if __name__ == "__main__":
    main()
