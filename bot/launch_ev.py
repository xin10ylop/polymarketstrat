"""THE launch number: per-decision hold-to-settle EV at the real paid price.

  venv/bin/python -m bot.launch_ev
  ERA=1786406400 venv/bin/python -m bot.launch_ev      # override the era start

WHY THIS TOOL AND NOT ANY OF THE OTHERS. The audit of 2026-08-13 traced the
project's headline EV from +13.3c (tape backtest, 08-10) to +3.55c (book
archive) and found the decay is almost entirely the SETTLE RATE, driven by
population differences none of those tools can escape:

  - the tape backtest keeps only windows with a pre-open taker BUY on our
    side — conditioning on informed flow already agreeing with the tilt —
    and prices entry at the median of those prints, not what anyone pays;
  - the book-archive join gates on the ARCHIVE's tilt at a fixed T-3 while
    the live bot gates on its own, staler view at whatever lead it achieved
    — a winner's-curse selection the archive cannot reproduce.

The live ledger has neither problem: every decision in it was selected by
the REAL signal at the REAL time and paid the REAL swept price, with the
real fee netted out. Its only weakness is n — which is why this reports the
autocorrelation-discounted interval and refuses to pool units.

WHAT IT COMPUTES, per unit, uncensored era only (default from 2026-08-11
12:20 UTC — after the shadow daily stop deployed, the dropped-window bug
died, and the tie band was set from measurement; before that the ledgers
are censored):

  EV_c   = 100 * mean over decisions of (pnl / shares)   <- cents per share,
           at the real paid price, net of real fees, by construction
  band   = t-interval on the per-decision series, n_eff discounted for
           autocorrelation (bot.scalp_backtest.band)
  cross-check: win rate vs break-even at the size-weighted paid price
           (must agree in sign with EV_c; a disagreement means a data bug)

GO / NO-GO reads ONE line per unit: the band's lower bound. Positive on the
uncensored era = qualified. Anything else = keep collecting. Read-only.
"""
import glob
import os
import sqlite3
import time

from bot.pnl_daily import breakeven, wilson
from bot.scalp_backtest import band

# Clean-era starts PER UNIT (2026-08-20). One global era stopped being
# honest when the units' histories diverged:
#   btc 5m   RULE2 (2026-08-14 00:00) — by luck it has zero fills between
#            the rule change and its 08-19 restart, so RULE2 selects
#            exactly the post-restart, new-rule population.
#   eth 5m   2026-08-20 07:39 UTC, when its 0.5-gate deploy went live
#            (first sub-1bp entry event, epoch 1787211597, minus a few
#            seconds so that first decision's own fills are included).
#            Earlier "new-rule" fills mix 3h of WRONG-RULE signal (it
#            traded 00:04-03:19 on 08-14 before its halt bit) and a
#            1.0-gate stretch — different populations, not this bot.
#   15m      2026-08-11 12:20, the original uncensored era: never
#            halted, and RULE2 never touched the 15m family.
# ERA= still forces every unit onto one era (for cross-checks).
ERAS = {"preopen-btc": 1786665600, "preopen-eth": 1787211590,
        "preopen-btc15": 1786450800, "preopen-eth15": 1786450800}
ERA = int(os.environ["ERA"]) if os.environ.get("ERA") else None
DEFAULT_ERA = 1786450800


def main():
    if ERA is not None:
        print(f"FORCED single era for all units: "
              f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(ERA))}\n")
    else:
        print("per-unit clean eras (ERA= forces one era on all units):")
        for u, e in sorted(ERAS.items()):
            print(f"  {u:<16} "
                  f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(e))}")
        print()
    print(f"{'unit':<16} {'n':>4} {'/day':>5} {'EV c/sh':>8} "
          f"{'95% band':>18} {'win%':>6} {'b/e%':>6} {'agree':>6}")
    for path in sorted(glob.glob("bot/data/preopen-*/paper.db")):
        name = os.path.basename(os.path.dirname(path))
        era = ERA if ERA is not None else ERAS.get(name, DEFAULT_ERA)
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = db.execute(
                "SELECT f.wts, SUM(f.price*f.size), SUM(f.size), SUM(f.pnl), "
                "MAX(f.ts) FROM fills f JOIN settlements s ON f.wts = s.wts "
                "WHERE f.pnl IS NOT NULL AND f.strategy='preopen' "
                "AND s.winner IS NOT NULL AND s.mismatch=0 "
                "GROUP BY f.wts HAVING SUM(f.size) >= 5 AND MAX(f.ts) >= ? "
                "ORDER BY MAX(f.ts)", (era,)).fetchall()
        except sqlite3.Error as e:
            print(f"{name:<16} unreadable ({str(e)[:40]})")
            continue
        if len(rows) < 4:
            print(f"{name:<16} {len(rows):>4}   too few uncensored decisions")
            continue
        per_share_c = [100.0 * pnl / sz for _, cost, sz, pnl, _ in rows]
        m, lo, hi, n_eff = band(per_share_c)
        wins = sum(1 for _, _, _, pnl, _ in rows if pnl > 0)
        tot_cost = sum(r[1] for r in rows)
        tot_sz = sum(r[2] for r in rows)
        px = tot_cost / tot_sz
        be = breakeven(px)
        wr = wins / len(rows)
        # the two estimates come from the same decisions; disagreement in
        # sign means a data bug, not a market opinion
        agree = "ok" if (m >= 0) == (wr >= be) else "CHECK"
        days = max(0.05, (rows[-1][4] - rows[0][4]) / 86400.0)
        print(f"{name:<16} {len(rows):>4} {len(rows)/days:>5.0f} "
              f"{m:>+8.2f} [{lo:>+7.2f},{hi:>+7.2f}] "
              f"{100*wr:>5.1f}% {100*be:>5.2f}% {agree:>6}")
        wl, wh = wilson(wins, len(rows))
        verdict = ("QUALIFIED — the lower bound is positive"
                   if lo > 0 else
                   "not qualified yet — the band includes zero"
                   if hi > 0 else
                   "NEGATIVE — the whole band is below zero")
        print(f"{'':<16} n_eff {n_eff:.0f}, wilson [{100*wl:.1f}, "
              f"{100*wh:.1f}] vs b/e {100*be:.2f}  ->  {verdict}")
    print("\nThe LOWER BOUND is the go/no-go number. It is per unit — pooling")
    print("units would let btc's sample launder eth's losses. Fees and the")
    print("real swept entry price are inside pnl already; nothing here is a")
    print("model. The censored era (halts, dropped windows, enforced daily")
    print("stop) is excluded because its losses are truncated and its win")
    print("rates are ceilings — see LIVE_RUNBOOK 2026-08-11.")


if __name__ == "__main__":
    main()
