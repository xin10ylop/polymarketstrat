"""The fleet in plain English. One screen, no statistics vocabulary.

  venv/bin/python -m bot.plain

WHY THIS EXISTS. The operator of this system is not a statistician and reads
these reports from a phone. pnl_daily, halt_audit and entry_ceiling each
print a dense table, and together they answer the question three times in a
language that has to be decoded first. On 2026-08-12 the honest feedback was
"i am not understanding any of the results", which is a defect in the
reporting, not in the reader.

So this says, for each bot, only:
    how much money, is it working, can we trust it yet, and when will we know.

Everything else stays in the detailed tools for when a specific question
comes up. Read-only.

THE ONE IDEA THE READER NEEDS, and it is stated in the output every time: a
50% win rate is NOT break-even here. The bot buys a share at about 52 cents
and pays a fee, so it needs roughly 54 wins in 100 just to get its money
back. Every "is it working" judgement below is against that number, never
against 50%.
"""
import glob
import math
import os
import sqlite3
import time

from bot.halt_audit import unit_state
from bot.pnl_daily import breakeven, wilson

NAMES = {"preopen-btc": "BTC 5-minute", "preopen-btc15": "BTC 15-minute",
         "preopen-eth": "ETH 5-minute", "preopen-eth15": "ETH 15-minute"}


def look(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        raw = db.execute("SELECT ts, wts, price, size, pnl FROM fills "
                         "WHERE pnl IS NOT NULL ORDER BY ts").fetchall()
        mism = db.execute(
            "SELECT COALESCE(SUM(mismatch),0) FROM settlements").fetchone()[0]
    except sqlite3.Error:
        return None
    if not raw:
        return {"n": 0, "pnl": 0.0, "mism": mism}
    # ONE DECISION = ONE TRADE. A single buy sweeps several price levels and
    # the executor writes a fill ROW PER LEVEL (see audit F4 in ledger.py) —
    # 1.8 rows per decision on btc, up to 4 on eth's thin book. Every row
    # from one window settles together, so counting rows as trades inflated
    # n and made every verdict more confident than the data supports; on
    # 2026-08-12 it declared eth "proven losing" on 85 rows that were only
    # ~24 decisions, and the verdict flipped back the next morning. Rows are
    # aggregated to the WINDOW before anything is counted.
    pos = {}
    for ts, w, px_, sz_, pnl_ in raw:
        a = pos.setdefault(w, [ts, 0.0, 0.0, 0.0])
        a[0] = max(a[0], ts)
        a[1] += px_ * sz_
        a[2] += sz_
        a[3] += pnl_
    rows = sorted(([t, c / z if z else 0.0, z, p] for t, c, z, p in
                   pos.values()), key=lambda r: r[0])
    n = len(rows)
    wins = sum(1 for r in rows if r[3] > 0)
    sz = sum(r[2] for r in rows) or 1.0
    px = sum(r[1] * r[2] for r in rows) / sz
    be = breakeven(px)
    lo, hi = wilson(wins, n)
    eq = pk = worst = 0.0
    for r in rows:
        eq += r[3]
        pk = max(pk, eq)
        worst = min(worst, eq - pk)
    days = max(1.0, (rows[-1][0] - rows[0][0]) / 86400.0)
    # "IT IS NOT MOVING" IS USUALLY THE 15m FAMILY BEING SLOW BY DESIGN: it
    # sees 96 windows a day against the 5m family's 288, and enters about a
    # fifth of them. Showing the RATE and the last trade time answers that
    # at a glance, where a lifetime total never can.
    now = time.time()
    day = [r for r in rows if r[0] > now - 86400]
    return {"n": n, "wins": wins, "pnl": eq, "rate": wins / n, "be": be,
            "lo": lo, "hi": hi, "worst": worst, "per_day": n / days,
            "mism": mism, "n24": len(day),
            "pnl24": sum(r[3] for r in day), "last": rows[-1][0]}


def verdict(s):
    """(headline, when we will know) in words, never in statistics."""
    if s["n"] < 15:
        return ("too few trades to say anything at all", "")
    # A VERDICT MUST SURVIVE A FEW DAYS BEFORE IT IS TREATED AS SETTLED. On
    # 08-12 this printed "LOSING, and that is real — more time will not
    # rescue this one" about eth, and by the next morning the verdict had
    # flipped back to unproven. A 95% line gets crossed falsely, especially
    # when it is checked every day, so the words must not promise more than
    # the test does.
    if s["hi"] < s["be"]:
        return ("LOSING — too far below the bar to be bad luck",
                "if it still says this in a few days, treat it as settled")
    if s["lo"] > s["be"]:
        return ("WINNING — unlikely to be luck",
                "if it still says this in a few days, treat it as settled")
    edge = s["rate"] - s["be"]
    if edge <= 0:
        return ("probably losing, but not proven yet",
                "watch it; the trend is the wrong way")
    need = int(3.8416 * s["rate"] * (1 - s["rate"]) / (edge * edge)) + 1
    days = (need - s["n"]) / max(0.1, s["per_day"])
    when = time.strftime("%d %b", time.gmtime(time.time() + days * 86400))
    return ("winning so far, but this could still be luck",
            f"about {days:.0f} more days (~{when}) before we can tell")


def main():
    print("=" * 68)
    print("  A 50% win rate is NOT break-even. The bot buys at about 52c and")
    print("  pays a fee, so it needs roughly 54 wins in 100 just to get its")
    print("  money back. Everything below is judged against that, not 50%.")
    print("=" * 68)
    total, trouble = 0.0, []
    fast = slow = 0.0
    big = 0
    for path in sorted(glob.glob("bot/data/preopen-*/paper.db")):
        key = os.path.basename(os.path.dirname(path))
        s = look(path)
        if s is None:
            continue
        name = NAMES.get(key, key)
        state = unit_state(key)
        total += s["pnl"]
        big = max(big, s["n"])
        if "15" in key:
            slow = max(slow, s["n24"])
        else:
            fast = max(fast, s["n24"])
        print(f"\n{name}   {s['pnl']:+,.0f} dollars")
        # "unknown" means the service manager could not be asked, NOT that
        # the bot is down. Reporting a failed check as a failure would cry
        # wolf every time this runs somewhere without systemd.
        if state == "stopped":
            print("   NOT RUNNING")
            trouble.append(f"{name} is not running")
        if s["n"] == 0:
            print("   no completed trades yet")
            continue
        head, when = verdict(s)
        print(f"   {s['wins']} wins out of {s['n']} trades "
              f"({100*s['rate']:.0f}%), needs {100*s['be']:.0f}% to break even")
        print(f"   -> {head}")
        if when:
            print(f"      {when}")
        print(f"   worst losing run: {s['worst']:+,.0f} dollars from its best "
              f"point")
        ago = (time.time() - s["last"]) / 60.0
        print(f"   last 24 hours: {s['n24']} trades, {s['pnl24']:+,.0f} "
              f"dollars   (newest trade {ago:.0f} min ago)")
        if s["mism"]:
            print(f"   !! {s['mism']} settlement disagreement(s) — HALTED")
            trouble.append(f"{name} has a settlement disagreement")

    print("\n" + "=" * 68)
    print(f"  ALL FOUR BOTS TOGETHER: {total:+,.0f} dollars")
    print("  This is pretend money. Nothing real has been traded.")
    if trouble:
        print("\n  NEEDS ATTENTION:")
        for t in trouble:
            print(f"    - {t}")
    else:
        print("  Nothing needs attention. The bots are running normally.")
    print("=" * 68)
    # THESE TWO PARAGRAPHS QUOTE NUMBERS, so they are computed rather than
    # written down. The hardcoded versions said "19 trades a day against 58"
    # and "flip a coin 160 times" — both were true of FILL ROWS and became
    # wrong the moment the counting was fixed to decisions. Prose that
    # states a figure has to read it from the same place the table does.
    if fast and slow:
        print("\nWHY THE 15-MINUTE BOTS LOOK SLOW. They get one window every")
        print("15 minutes where the 5-minute bots get one every 5, and both")
        print("trade only a fraction of what they see. Over the last 24 hours")
        print(f"that came to {fast:.0f} trades for a 5-minute bot against "
              f"{slow:.0f} for a 15-minute one.")
    print("\nIt is normal for a total to sit still for an hour: a trade only")
    print("counts once its window has closed AND settled, which is 5 or 15")
    print("minutes after it opened, plus a minute for the exchange.")
    # floor the illustration: quoting a two-trade sample back at the reader
    # as evidence about coin flips is worse than saying nothing
    print(f"\nWHY 'could still be luck' KEEPS APPEARING. Flip a fair coin "
          f"{max(big, 100)} times")
    print("and you will often see a run that looks like an edge. The bots have")
    print("not yet made enough trades for a good result to be distinguishable")
    print("from that. The only cure is more trades, which is why the dates")
    print("above matter more than today's dollar figure.")


if __name__ == "__main__":
    main()
