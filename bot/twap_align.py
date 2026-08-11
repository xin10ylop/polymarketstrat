"""Is our TWAP reconstruction MISALIGNED with the one the market settles on?

  venv/bin/python -m bot.twap_align
  DATA=bot/data/preopen-eth COIN=eth venv/bin/python -m bot.twap_align

WHAT PROVOKED THIS. Three flagged mismatches, and every one of them the same
direction: we said DOWN by a hair, the exchange said UP (btc -0.310bp, eth
-0.303bp, eth -0.421bp). Two sit essentially ON the 0.3bp tie threshold, and
one window mismatched on BTC AND ETH AT THE SAME INSTANT, which no per-coin
price error can produce. Reconstruction noise is symmetric; this is not.

The settlement rule is "TWAP at close >= TWAP at open", so TIES BREAK UP. A
small systematic NEGATIVE bias in our reconstruction pushes near-ties to
"down" for us and "up" for them — precisely the observed signature.

WHAT WOULD CAUSE SUCH A BIAS. Our TWAP is the mean of 1-second grid samples
over [t-n, t). Chainlink's published stream may average a different set of
seconds: a boundary offset by a second or two, a different window length, or
a stream timestamped at publication rather than at the end of its own window.
In a trending market any of those produces a small, SIGNED, systematic
difference of exactly this size.

WHAT THIS DOES. Rather than widen the tie band until the alarm stops — which
would bury the bug rather than fix it — it scans boundary offsets and window
lengths against the OFFICIAL outcomes and asks whether some alignment
explains the disagreements. A misalignment shows up as a clear accuracy peak
away from the current setting. Noise shows up as a flat surface.

GUARDED AGAINST FITTING. Scanning offsets against outcomes is exactly the
shape of exercise that invents an edge: this project has already retracted a
vol-scaled gate twice for it. So every cell is chosen on the FIRST half of
the windows and reported on the SECOND, and the baseline is carried through
both halves for comparison. A winner that does not survive the split is
reported as noise, however good its in-sample number.

Also reports the two things the mismatch count alone cannot show: the
DIRECTION of every error, and why 37% of windows get no call at all.

Read-only.
"""
import os
import sqlite3

from bot.config import CFG
from bot.twap_verify import COIN, DB_DIR, NSEC, WINDOW

DATA = os.environ.get("DATA", "bot/data/preopen-btc")
MINCOV = CFG.oracle_twap_min_coverage
TIE = CFG.oracle_tie_bps
SHIFTS = range(-4, 5)
# only windows inside this margin can be flipped by a boundary shift
NEAR = float(os.environ.get("NEAR_BP", 2.0))
LENGTHS = (NSEC - 2, NSEC - 1, NSEC, NSEC + 1, NSEC + 2)


def mean_at(grid, t, n):
    """(mean, coverage) over [t-n, t) — the production convention, no carry."""
    vals = [grid[s] for s in range(int(t) - n, int(t)) if s in grid]
    if not vals:
        return None, 0.0
    return sum(vals) / len(vals), len(vals) / float(n)


def call(grid, wts, shift, n):
    """(winner, bp) under a boundary offset, or (None, None) on thin cover."""
    k, ck = mean_at(grid, wts + shift, n)
    c, cc = mean_at(grid, wts + WINDOW + shift, n)
    if k is None or c is None or k <= 0 or min(ck, cc) < MINCOV:
        return None, None
    return ("up" if c >= k else "down"), (c - k) / k * 1e4


def score(grid, rows, shift, n):
    ok = bad = mute = 0
    for wts, winner in rows:
        w, _ = call(grid, wts, shift, n)
        if w is None:
            mute += 1
        elif w == winner:
            ok += 1
        else:
            bad += 1
    return ok, bad, mute


def main():
    lpath = os.path.join(DATA, "paper.db")
    gpath = os.path.join(DB_DIR, f"{COIN}_1s.db")
    for p in (lpath, gpath):
        if not os.path.exists(p):
            raise SystemExit(f"missing {p}")
    grid = dict(sqlite3.connect(f"file:{gpath}?mode=ro", uri=True)
                .execute("SELECT ts, v FROM px"))
    lo, hi = min(grid), max(grid)
    rows = [(w, x) for (w, x) in sqlite3.connect(
        f"file:{lpath}?mode=ro", uri=True).execute(
        "SELECT wts, winner FROM settlements WHERE winner IS NOT NULL "
        "ORDER BY wts")
        if lo <= w - NSEC and w + WINDOW <= hi]
    if len(rows) < 40:
        raise SystemExit(f"only {len(rows)} settled windows inside the archive")

    print(f"{DATA}  coin={COIN}  window={WINDOW}s  twap={NSEC}s  "
          f"min_cov={MINCOV}  tie<{TIE}bp")
    print(f"{len(rows)} settled windows the archive can price\n")

    # ---- 1. IS THE ERROR SIGNED? ------------------------------------------
    # The whole case rests on this. Symmetric errors are noise near a tie and
    # nothing can be done about them. A one-sided error is a bias, and a bias
    # has a cause.
    wrong, mags, ok_n = [], [], 0
    thin = edge_split = 0
    for wts, winner in rows:
        w, bp = call(grid, wts, 0, NSEC)
        if w is None:
            thin += 1
            continue
        if w == winner:
            ok_n += 1
        else:
            wrong.append((wts, w, winner, bp))
        mags.append(abs(bp))
    dn_up = sum(1 for _, w, x, _ in wrong if w == "down" and x == "up")
    up_dn = sum(1 for _, w, x, _ in wrong if w == "up" and x == "down")
    print("=== 1. DIRECTION OF THE ERRORS (the question that decides it) ===")
    print(f"  called, correct            : {ok_n}")
    print(f"  called, WRONG              : {len(wrong)}")
    print(f"    we said DOWN, exchange UP: {dn_up}")
    print(f"    we said UP, exchange DOWN: {up_dn}")
    print(f"  refused (coverage < {MINCOV}) : {thin}")
    if wrong:
        ws = sorted(abs(b) for _, _, _, b in wrong)
        print(f"  |bp| of the wrong calls    : median {ws[len(ws)//2]:.3f}, "
              f"max {ws[-1]:.3f}")
        big = [w for w in wrong if abs(w[3]) > 1.0]
        print(f"  wrong on a window decided by MORE than 1bp: {len(big)}")
        print("  (a reconstruction that is merely noisy near a tie cannot be")
        print("   wrong on a decided window; those would be a real defect)")
        for wts, w, x, bp in sorted(wrong, key=lambda z: -abs(z[3]))[:6]:
            print(f"     w{wts}  ours={w:<4} exch={x:<4} {bp:+.3f}bp")
    if dn_up + up_dn:
        skew = dn_up / float(dn_up + up_dn)
        print(f"\n  one-sidedness: {100*skew:.0f}% of errors are down/up.")
        print("  50% is noise. Near 100% is a NEGATIVE BIAS in our close-open,")
        print("  which is what an alignment error looks like.")

    # ---- 2. DOES AN ALIGNMENT EXPLAIN IT? ---------------------------------
    # ONLY NEAR-TIES CAN DISCRIMINATE. A window decided by 5bp is called
    # correctly by every alignment in the scan, so including it adds a point
    # to every cell equally and dilutes the comparison toward a dead heat.
    # Shifting the boundary a second or two moves the mean by hundredths of a
    # basis point; it can only flip a window already inside that margin —
    # which is precisely the population the three mismatches came from. So
    # the scan is run on the windows where an alignment error is capable of
    # showing up at all.
    near = [(w, x) for (w, x) in rows
            if (lambda r: r[1] is not None and abs(r[1]) < NEAR)(
                call(grid, w, 0, NSEC))]
    print(f"\nwindows decided by under {NEAR}bp (where alignment can matter "
          f"at all): {len(near)} of {len(rows)}")
    if len(near) < 20:
        print("Too few to scan. The alignment question cannot be answered")
        print("until more near-tie windows accumulate; nothing below has")
        print("enough power to act on.")
    scan_rows = near if len(near) >= 20 else rows
    half = len(scan_rows) // 2
    a, b = scan_rows[:half], scan_rows[half:]
    base_a, base_b = score(grid, a, 0, NSEC), score(grid, b, 0, NSEC)

    def acc(t):
        return 100.0 * t[0] / (t[0] + t[1]) if t[0] + t[1] else 0.0

    print(f"\n=== 2. ALIGNMENT SCAN, {len(SHIFTS)}x{len(LENGTHS)} cells, on {len(scan_rows)} windows ===")
    print(f"baseline (shift 0, n={NSEC}):  "
          f"first half {acc(base_a):.1f}%  second half {acc(base_b):.1f}%")
    cells = []
    for n in LENGTHS:
        for s in SHIFTS:
            t = score(grid, a, s, n)
            if t[0] + t[1] >= 20:
                cells.append((acc(t), s, n, t))
    # TIE-BREAK TOWARD THE CURRENT SETTING. Whole rows of this surface can
    # sit at the same accuracy — a shift only matters when the price moved
    # across it — and picking arbitrarily among them manufactures a
    # "discovery" out of a coin flip. Among cells within 0.1 point of the
    # best, prefer the one nearest the alignment we already use, so a
    # genuine convention difference has to actually BEAT the incumbent
    # rather than merely tie it and win on sort order.
    cells.sort(key=lambda z: (-z[0], abs(z[1]), abs(z[2] - NSEC)))
    top = cells[0][0]
    tied = [c for c in cells if c[0] >= top - 0.1]
    if len(tied) > 1:
        print(f"({len(tied)} cells tie within 0.1pt in sample — reporting the "
              f"one closest to the current alignment)")
    print(f"\n{'shift':>6} {'n':>4} {'in-sample':>10} {'n_called':>9} "
          f"{'OUT-OF-SAMPLE':>14} {'n_called':>9}")
    for a_acc, s, n, t in cells[:6]:
        ob = score(grid, b, s, n)
        print(f"{s:>+6} {n:>4} {a_acc:>9.1f}% {t[0]+t[1]:>9} "
              f"{acc(ob):>13.1f}% {ob[0]+ob[1]:>9}"
              f"{'   <- current' if (s == 0 and n == NSEC) else ''}")

    best = cells[0]
    ob = score(grid, b, best[1], best[2])
    print()
    if best[1] == 0 and best[2] == NSEC:
        print("THE CURRENT ALIGNMENT ALREADY WINS IN SAMPLE. There is no")
        print("offset or window length that reads the settlement feed better,")
        print("so the residual disagreements are not a misalignment.")
    elif acc(base_b) >= acc(ob):
        # A ceiling case: nothing can beat a baseline that is already right
        # on every window it calls, and reporting "fitted" there would be
        # the wrong words for the right answer.
        print(f"NOTHING BEATS THE CURRENT ALIGNMENT OUT OF SAMPLE "
              f"({acc(base_b):.1f}% vs {acc(ob):.1f}%).")
        print("The residual disagreements are not a misalignment.")
    elif acc(ob) > acc(base_b) + 0.5:
        print(f"A DIFFERENT ALIGNMENT WINS, AND IT SURVIVES THE SPLIT: "
              f"shift {best[1]:+d}s, n={best[2]}")
        print(f"  out of sample {acc(ob):.1f}% against the baseline's "
              f"{acc(base_b):.1f}%.")
        print("  That is a real convention difference, not a fitted one, and")
        print("  it is worth changing oracle.twap_at over. Re-run on the other")
        print("  coin before doing so: a true convention is shared, a fitted")
        print("  one is not.")
    else:
        print(f"THE BEST IN-SAMPLE CELL (shift {best[1]:+d}s, n={best[2]}, "
              f"{best[0]:.1f}%) DOES NOT")
        print(f"SURVIVE OUT OF SAMPLE ({acc(ob):.1f}% vs baseline "
              f"{acc(base_b):.1f}%). That is a fitted")
        print("cell, not a convention. Do not change the oracle on it.")

    # ---- 3. WHAT TIE BAND IS THE MEASUREMENT ACTUALLY ENTITLED TO? --------
    # If the estimator is right and the alignment is right, what is left is
    # the irreducible residual of approximating a published TWAP STREAM with
    # a uniform mean of 1-second samples. The tripwire must sit ABOVE that
    # residual or it fires on our own arithmetic — which is what has been
    # happening. But a band that is too wide blinds the tripwire instead
    # (audit D10 found 2.0bp switched it off on 34-71% of windows), so the
    # band is a measurement, not a preference. Both costs, priced together.
    called = [(abs(bp), w == x) for (w, x, bp) in
              ((c[0], winner, c[1]) for wts, winner in rows
               for c in [call(grid, wts, 0, NSEC)] if c[0] is not None)]
    n_called = len(called)
    print(f"\n=== 3. TIE BAND: SENSITIVITY vs FALSE ALARMS, {n_called} calls ===")
    print(f"{'band':>6} {'blind':>7} {'blind%':>8} {'errors left':>12} "
          f"{'accuracy above band':>20}")
    choice = None
    for band in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.5, 2.0):
        blind = sum(1 for m, _ in called if m < band)
        above = [okk for m, okk in called if m >= band]
        left = sum(1 for okk in above if not okk)
        a2 = 100.0 * sum(above) / len(above) if above else 0.0
        mark = ""
        if left == 0 and choice is None:
            choice, mark = band, "   <- first band with NO false alarms"
        print(f"{band:>6.1f} {blind:>7} {100*blind/n_called:>7.1f}% "
              f"{left:>12} {a2:>19.1f}%{mark}"
              + ("   <- current" if abs(band - TIE) < 1e-9 else ""))
    print()
    if choice is not None:
        print(f"EVERY DISAGREEMENT THIS ARCHIVE CONTAINS IS INSIDE {choice}bp.")
        print(f"At that band the tripwire is blind to "
              f"{100*sum(1 for m,_ in called if m < choice)/n_called:.0f}% of "
              f"windows and reads the rest at 100%.")
        print("A band below it does not make the tripwire more sensitive to")
        print("real defects — there are none up there to find — it only makes")
        print("it fire on our own reconstruction residual, which is what")
        print("halted the fleet. Set ORACLE_TIE_BPS from this column, and")
        print("only after the same table on the other coin agrees.")
    else:
        print("NO BAND CLEARS THE ERRORS. Some disagreement survives at 2bp,")
        print("which a sampling residual cannot explain — that is a real")
        print("defect and the tie band is not the answer to it.")


if __name__ == "__main__":
    main()
