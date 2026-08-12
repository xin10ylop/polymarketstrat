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
import math
import os
import sqlite3
import time

DAYS = int(os.environ.get("DAYS", "14"))
DAY = 86400
Z = 1.96


def wilson(k, n):
    """95% interval on a win rate. Wilson, not normal-approximation: at these
    sample sizes the naive interval runs past 100% and understates width."""
    if not n:
        return 0.0, 0.0
    p = k / n
    d = 1 + Z * Z / n
    c = (p + Z * Z / (2 * n)) / d
    h = Z / d * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n))
    return max(0.0, c - h), min(1.0, c + h)


def breakeven(px):
    """Win rate needed to break even buying at px. Fee is 0.07*p*(1-p), so a
    0.5187 entry needs 53.62% — the number every win rate here is measured
    against, and it is NOT 50%."""
    return px + 0.07 * px * (1 - px)


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
            raw = db.execute(
                "SELECT ts, wts, price, size, fee, pnl FROM fills "
                "WHERE pnl IS NOT NULL ORDER BY ts").fetchall()
        except sqlite3.Error as e:
            print(f"{name}: unreadable ({str(e)[:40]})\n")
            continue
        if not raw:
            print(f"{name}: no settled fills yet\n")
            continue
        # ONE DECISION = ONE TRADE. The executor writes a fill row PER PRICE
        # LEVEL swept (audit F4 in ledger.py) — 1.8 rows per decision on btc,
        # up to 4 on eth's thin book — and every row from one window settles
        # together. Counting rows as trades inflated n on every win rate this
        # tool printed, tightening the intervals by up to 2x; the "proven
        # losing" eth verdict of 08-12 was issued on 85 rows that were ~24
        # decisions, and flipped back the next morning. Aggregate to the
        # WINDOW before counting anything.
        posmap = {}
        for ts, w, px_, sz_, fee_, pnl_ in raw:
            a = posmap.setdefault(w, [ts, 0.0, 0.0, 0.0, 0.0])
            a[0] = max(a[0], ts)
            a[1] += px_ * sz_
            a[2] += sz_
            a[3] += fee_
            a[4] += pnl_
        positions = sorted(posmap.values())     # by settle ts
        byday = {}
        for ts, cost, sz_, fee_, pnl_ in positions:
            d = int(ts // DAY)
            r = byday.setdefault(d, [0, 0.0, 0.0, 0])
            r[0] += 1
            r[1] += pnl_
            r[2] += fee_
            r[3] += 1 if pnl_ > 0 else 0
        rows = [(d, r[0], r[1], r[2], r[3]) for d, r in sorted(byday.items())]
        # DRAWDOWN MUST COME FROM THE FILL-LEVEL CURVE, NOT THE DAILY ONE.
        # The first version reported day-END equity only, and on btc 5m that
        # turned a real -$580.18 drawdown from a +$743.34 peak into
        # "-0.46 (0% of peak)" — because the day CLOSED at +471.84 having run
        # up to +743 and back inside it. A daily series cannot see a round
        # trip that starts and finishes inside one day, and drawdown is the
        # whole reason this tool exists.
        curve = [(ts, pnl_) for ts, cost, sz_, fee_, pnl_ in positions]
        eq = pk_f = 0.0
        worst_f, worst_at, pk_at = 0.0, None, None
        for ts, pnl in curve:
            eq += pnl
            if eq > pk_f:
                pk_f, pk_at = eq, ts
            if eq - pk_f < worst_f:
                worst_f, worst_at = eq - pk_f, ts
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
        print(f"{'day (UTC)':<12} {'trades':>6} {'win%':>6} {'day P&L':>10} "
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
              f"{'day-end DD':>10} {worst:>+10.2f}")
        print(f"lifetime {hist[-1][5]:+.2f}   peak (day-end) {peak:+.2f}")
        # THE NUMBER TO SIZE AGAINST is the intraday one. A day-end series
        # says btc 5m never drew down; the fill-level curve says it gave back
        # 78% of its peak. Both are printed so the gap between them is
        # visible rather than something you have to know to look for.
        if pk_f > 0:
            when = (time.strftime("%m-%d %H:%M", time.gmtime(worst_at))
                    if worst_at else "-")
            print(f"INTRADAY (trade by trade, {len(curve)} trades): "
                  f"peak {pk_f:+.2f} at "
                  f"{time.strftime('%m-%d %H:%M', time.gmtime(pk_at))}, "
                  f"worst drawdown {worst_f:+.2f} at {when} "
                  f"({100*abs(worst_f)/pk_f:.0f}% of peak)")
            if abs(worst_f) > abs(worst) * 2 and abs(worst_f) > 1:
                print(f"  ^ the daily row above shows {worst:+.2f}. The round")
                print("    trip happened INSIDE a day, so only this line sees")
                print("    it. Size against this one.")
        else:
            print(f"INTRADAY: never above water "
                  f"(worst {min([0.0] + [w for w in [worst_f]]):+.2f})")
        # ---- IS THIS DISTINGUISHABLE FROM ZERO? ---------------------------
        # The only question a P&L total cannot answer. A +$471 result on 130
        # fills sounds like an edge and is 0.5 standard deviations from
        # nothing; the interval says so and the total never will.
        wins = sum(1 for _, p in curve if p > 0)
        n = len(curve)
        tot_cost = sum(c for _, c, *_ in positions)
        tot_sz = sum(z for _, _, z, *_ in positions)
        px = (tot_cost / tot_sz) if tot_sz else 0.5
        be = breakeven(px)
        lo, hi = wilson(wins, n)
        wr = wins / n if n else 0.0
        print(f"EDGE: {wins}/{n} = {100*wr:.1f}%   95% CI "
              f"[{100*lo:.1f}, {100*hi:.1f}]   entry {px:.4f} -> "
              f"break-even {100*be:.2f}%")
        if lo > be:
            print("  clears break-even at 95%.")
        elif hi < be:
            print("  BELOW break-even at 95% — this is a losing configuration,")
            print("  not an unlucky one.")
        else:
            edge = wr - be
            if edge > 0:
                # n for the interval to exclude break-even at the OBSERVED
                # edge. If the edge is real, this is how long the wait is.
                need = int(Z * Z * wr * (1 - wr) / (edge * edge)) + 1
                print(f"  straddles break-even: cannot tell an edge from "
                      f"nothing yet.")
                print(f"  At this observed edge ({100*edge:+.2f}pp) it would "
                      f"take about {need} fills")
                print(f"  to separate them — {need - n} more, roughly "
                      f"{(need - n) / max(1, n / max(1, len(hist))):.0f} "
                      f"days at the current rate.")
            else:
                print("  straddles break-even, and the point estimate is on")
                print("  the WRONG side of it. More data may rescue it; do not")
                print("  assume it will.")
        print("* = today, still open\n")

    print("pnl_today= in the STATUS line is the '*' row only and resets at")
    print("00:00 UTC. fills={'pnl':} in the same line is the lifetime column.")
    print("Days marked HALTED are censored: the bot stopped partway down, so")
    print("their drawdown is a floor and their win rate a ceiling.")


if __name__ == "__main__":
    main()
