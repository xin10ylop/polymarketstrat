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
        rows = db.execute("SELECT ts, price, size, pnl FROM fills "
                          "WHERE pnl IS NOT NULL ORDER BY ts").fetchall()
        mism = db.execute(
            "SELECT COALESCE(SUM(mismatch),0) FROM settlements").fetchone()[0]
    except sqlite3.Error:
        return None
    if not rows:
        return {"n": 0, "pnl": 0.0, "mism": mism}
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
    if s["hi"] < s["be"]:
        return ("LOSING, and that is real — not bad luck",
                "more time will not rescue this one")
    if s["lo"] > s["be"]:
        return ("WINNING, and we can now trust it", "proven")
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
    for path in sorted(glob.glob("bot/data/preopen-*/paper.db")):
        key = os.path.basename(os.path.dirname(path))
        s = look(path)
        if s is None:
            continue
        name = NAMES.get(key, key)
        state = unit_state(key)
        total += s["pnl"]
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
    print("\nWHY THE 15-MINUTE BOTS LOOK FROZEN. They get one window every")
    print("15 minutes where the 5-minute bots get one every 5, and both only")
    print("trade about a fifth of what they see — so a 15-minute bot makes")
    print("roughly 19 trades a day against 58. It is also normal for a total")
    print("to sit still for an hour: a trade only counts once its window has")
    print("closed AND settled, which is 5 or 15 minutes later plus a minute.")
    print("\nWHY 'could still be luck' KEEPS APPEARING. Flip a fair coin 160")
    print("times and you will often see 57% heads. The bots have not yet made")
    print("enough trades for a good result to be distinguishable from that.")
    print("The only cure is more trades, which is why the dates above matter")
    print("more than today's dollar figure.")


if __name__ == "__main__":
    main()
