"""Do the deep levels of the sweep earn their price? The sweep-cap replay.

  venv/bin/python -m bot.sweep_cap
  ERA=1786060800 venv/bin/python -m bot.sweep_cap     # rule-era cross-check

WHY. A preopen entry SWEEPS the ask ladder up to PREOPEN_MAX_PX: the
cheapest level of a decision is the touch at that moment, and every level
above it pays more for the same claim. slippage.py measured that sweep at
1.5-2.4c on a ~3-10c edge. This tool asks the only question that decides a
cap: do the shares bought N ticks above the touch settle well enough to
pay for themselves, net of their own fee, at their own price?

The ledger already holds the answer: one fill row PER LEVEL SWEPT (audit
F4), each with its real price, size, fee and settled pnl. No model. All
aggregation is per DECISION (window), never per row -- rows of a window
settle together and are not independent evidence.

  MARGINAL DEPTH  pnl per share by depth above the touch, with decision-
                  ordered time halves. A depth that loses in both halves
                  is a pure donation. Buckets are CENTS, not ticks: the
                  first run found the ladder quotes on whole cents (zero
                  fills 1-5 ticks up on every unit; the tail starts 6+
                  ticks / ~1c above the touch).
  CAP REPLAY      what each cap (touch, +1c, +2c) would have done on the
                  same decisions: shares kept, $ deployed, EV c/share,
                  the per-DECISION mean with its autocorrelation-
                  discounted band (what launch_ev would report for a bot
                  built with this cap), and $/day. Capping wins in
                  ABSOLUTE terms only where the cut tail was negative --
                  a positive tail still adds dollars even at worse
                  per-share EV, and $/day is the objective.

THE PRE-REGISTERED BAR. A cap goes into config only if the cut tail loses
in BOTH halves AND the same depth loses on the other coin of the family.
Per-share EV improving is NOT enough. Within-decision depth comparisons
are largely robust to the old era's day-censoring (a halt truncates every
depth of a day equally), so the rule-era cross-check command above is the
larger-n confirmation, not a different question. Read-only.
"""
import glob
import os
import sqlite3
import time

from bot.scalp_backtest import band

TICK = 0.001
ERA = int(os.environ.get("ERA", "1786450800"))
MIN_DEC = int(os.environ.get("MIN_DEC", "10"))     # per half, per tail
# The ladder quotes on whole CENTS in practice: the first run of this tool
# found zero fills 1-5 ticks above the touch across every unit and era --
# every deeper level sat 6+ ticks up. Buckets and caps are cent-denominated
# for that reason; depth is still computed in ticks internally.
CAPS = ((0, "touch"), (10, "+1c"), (20, "+2c"), (999, "uncap"))
DATA_GLOB = os.environ.get("DATA_GLOB", "bot/data/preopen-*/paper.db")
BUCKETS = ((0, 0, "touch"), (1, 10, "+0-1c"), (11, 20, "+1-2c"),
           (21, 999, "+2c+"))


def agg(decs, lo, hi):
    """Per-decision series over rows with lo<=depth<=hi; halves by decision
    order. Returns (n_dec, shares, avg_px, settle%, c/share, h1, h2)."""
    sub = []
    for _, rows in decs:
        r = [x for x in rows if lo <= x[0] <= hi]
        if r:
            sub.append(r)
    if not sub:
        return None

    def cs(chunk):
        sz = sum(x[2] for r in chunk for x in r)
        pnl = sum(x[5] for r in chunk for x in r)
        return (100 * pnl / sz, sz) if sz else (0.0, 0)
    half = len(sub) // 2
    sz = sum(x[2] for r in sub for x in r)
    cost = sum(x[1] * x[2] for r in sub for x in r)
    won = sum(x[2] for r in sub for x in r if x[4] >= 0.5)
    return {"n": len(sub), "sz": sz, "px": cost / sz, "win": won / sz,
            "c": cs(sub)[0], "h1": cs(sub[:half]), "h2": cs(sub[half:])}


def unit(path):
    name = os.path.basename(os.path.dirname(path))
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        raw = db.execute(
            "SELECT f.wts, f.ts, f.price, f.size, f.fee, f.settle, f.pnl "
            "FROM fills f JOIN settlements s ON f.wts = s.wts "
            "WHERE f.pnl IS NOT NULL AND f.strategy='preopen' "
            "AND s.winner IS NOT NULL AND s.mismatch=0 AND f.ts >= ? "
            "ORDER BY f.wts, f.price", (ERA,)).fetchall()
    except sqlite3.Error as e:
        print(f"\n{name}: unreadable ({str(e)[:40]})")
        return
    bywts = {}
    for w, ts, px, sz, fee, st, pnl in raw:
        bywts.setdefault(w, []).append((ts, px, sz, fee, st, pnl))
    decs = []          # [(wts, [(depth, px, sz, fee, settle, pnl), ...])]
    for w in sorted(bywts):
        rows = bywts[w]
        if sum(x[2] for x in rows) < 5:
            continue
        touch = min(x[1] for x in rows)
        decs.append((w, [(round((px - touch) / TICK), px, sz, fee, st, pnl)
                         for _, px, sz, fee, st, pnl in rows]))
    if len(decs) < 8:
        print(f"\n{name}: {len(decs)} decisions -- too few")
        return
    tss = [x[0] for w, _ in decs for x in bywts[w]]
    days = max(0.05, (max(tss) - min(tss)) / 86400)
    print(f"\n{name}: {len(decs)} decisions, "
          f"{sum(len(r) for _, r in decs)} fill rows over {days:.1f} days")

    print(f"{'depth':>7} {'n_dec':>6} {'shares':>7} {'avg px':>7} "
          f"{'win%':>6} {'c/share':>8} {'h1 c':>7} {'h2 c':>7}")
    for lo, hi, lab in BUCKETS:
        a = agg(decs, lo, hi)
        if not a:
            continue
        print(f"{lab:>7} {a['n']:>6} {a['sz']:>7.0f} {a['px']:>7.4f} "
              f"{100*a['win']:>5.1f}% {a['c']:>+8.2f} "
              f"{a['h1'][0]:>+7.2f} {a['h2'][0]:>+7.2f}")

    # per-cap, the per-DECISION series carries the launch statistic: its
    # autocorrelation-discounted band is what launch_ev would report if the
    # bot had been built with this cap
    print(f"{'cap':>7} {'sh/dec':>7} {'$/dec':>7} {'EV c/sh':>8} "
          f"{'per-dec c [95% band]':>24} {'$/day':>8} {'vs uncap':>9}")
    unc = sum(x[5] for _, r in decs for x in r)
    for cap, lab in CAPS:
        series, sz, pnl, cost = [], 0.0, 0.0, 0.0
        for _, r in decs:
            kept = [x for x in r if x[0] <= cap]
            ks = sum(x[2] for x in kept)
            kp = sum(x[5] for x in kept)
            series.append(100 * kp / ks)
            sz, pnl = sz + ks, pnl + kp
            cost += sum(x[1] * x[2] for x in kept)
        m, blo, bhi, _ = band(series)
        delta = "" if cap == 999 else f" {(pnl - unc) / days:>+9.2f}"
        print(f"{lab:>7} {sz/len(decs):>7.1f} {cost/len(decs):>7.2f} "
              f"{100*pnl/sz:>+8.2f} "
              f"{m:>+7.2f} [{blo:>+6.2f},{bhi:>+6.2f}] "
              f"{pnl/days:>+8.2f}{delta}")

    # verdict: the shallowest depth whose whole tail loses in both halves
    for d, cap_lab in ((1, "touch-only"), (11, "touch+1c"),
                       (21, "touch+2c")):
        a = agg(decs, d, 999)
        if not a:
            continue
        (c1, _), (c2, _) = a["h1"], a["h2"]
        n1, n2 = a["n"] // 2, a["n"] - a["n"] // 2
        if min(n1, n2) < MIN_DEC:
            print(f"  verdict: tail {d}+ ticks has only {a['n']} decisions "
                  f"-- too few to judge; keep collecting.")
            break
        if c1 < 0 and c2 < 0:
            print(f"  verdict: every share {d}+ ticks above the touch LOSES "
                  f"in both halves ({c1:+.2f}c, {c2:+.2f}c on {a['n']} "
                  f"decisions) -- {cap_lab} entry is actionable as a "
                  f"PROPOSAL if the other coin of the family agrees.")
            break
    else:
        print("  verdict: no depth tail loses in both halves -- the deep "
              "levels pay for themselves; no cap.")


def main():
    print(f"sweep-cap replay, era >= "
          f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(ERA))} "
          f"(ERA= to override; ERA=1786060800 = rule era, larger n)")
    paths = sorted(glob.glob(DATA_GLOB))
    if not paths:
        raise SystemExit(f"no ledgers match {DATA_GLOB}")
    for p in paths:
        unit(p)


if __name__ == "__main__":
    main()
