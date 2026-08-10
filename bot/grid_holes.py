"""The Chainlink 1s grid has holes. What are they, and what do they cost?

  venv/bin/python -m bot.grid_holes
  COIN=eth GATE=1.0 venv/bin/python -m bot.grid_holes

WHY. bot.preopen_diag settled the first question: the pre-open bot's no_grid
refusals are NOT mostly arrival lag. The grid itself is incomplete — 62.6% of
btc windows are missing at least one of the 27 seconds available at T-3, and
23.1% miss enough to trip the 0.9 coverage floor. Lag adds to that (the live
rate sits near 41%, between the lag-2 and lag-3 rows) but the floor under it
is holes, and no amount of horizon-awareness recovers those.

So the refusal is only correct if a holed window actually corrupts the call.
That has never been measured. The 98.9% sign-agreement result was computed on
whatever the archive held, complete or not — it never asked what the holes did
to it. This asks.

THE THREE QUESTIONS, in the order that decides what to change:

  1. WHAT IS A HOLE? Run lengths and position. An isolated missing second is
     a very different object from a 6-second blackout, and the coverage floor
     cannot tell them apart — it counts.

  2. IS A HOLE A NO-CHANGE REPORT? Chainlink streams publish on deviation or
     heartbeat. If a second is absent because the price did not move, then the
     right imputation is to CARRY THE LAST PRICE, and the code's current rule
     — rescale the present seconds' mean over the whole window — is biased,
     because it fills the gap with the window average instead of with the
     price that was actually standing. Testable: when second s is missing, how
     often is g[s-1] == g[s+1] exactly?

  3. WHAT DO HOLES COST THE CALL? On windows whose strike window is COMPLETE,
     compute the tilt the bot would have seen. Then punch real hole patterns
     — sampled from the actually-incomplete windows, so burst structure and
     position are preserved rather than invented — into those same windows and
     recompute under both imputation rules. The number that matters is whether
     the SIDE flips, because the side is the whole decision.

Output is a table of sign agreement against the complete-grid call, bucketed
by how many seconds survived. That prices the coverage floor directly: if a
20-of-27 window still calls the same side 99% of the time, the floor at 24.3
is refusing trades for nothing. If agreement falls off a cliff, the floor is
earning its keep and the lead has to move instead.

Read-only. Never trades, touches no bot state, writes nothing.
"""
import os
import random
from collections import Counter

from bot.twap_verify import COIN, FAMILY, NSEC, WINDOW, load_grid

LEAD = int(os.environ.get("LEAD", "3"))
GATE = float(os.environ.get("GATE", "0.5"))       # bp; the live btc gate
SEED = int(os.environ.get("SEED", "11"))
REPS = int(os.environ.get("REPS", "5"))            # hole patterns per window
TOL = 3                                            # price_at tolerance


def pct(k, n):
    return 100.0 * k / n if n else float("nan")


def main():
    g = load_grid()
    lo, hi = min(g), max(g)
    first = ((int(lo) + NSEC) // WINDOW + 1) * WINDOW
    opens = [t for t in range(first, int(hi) + 1, WINDOW) if t - NSEC >= lo]
    if not opens:
        raise SystemExit("archive too short")
    n_el = NSEC - LEAD                # elapsed strike seconds at T-lead
    print(f"{COIN} {FAMILY}: {len(opens)} windows, strike [T-{NSEC}, T), "
          f"decision at T-{LEAD} on {n_el} elapsed seconds, gate {GATE}bp\n")

    # ------------------------------------------------- 1. what is a hole?
    runs, masks, present_hist = Counter(), [], Counter()
    for t in opens:
        miss = [i for i in range(n_el) if (t - NSEC + i) not in g]
        present_hist[n_el - len(miss)] += 1
        if not miss:
            continue
        masks.append(tuple(miss))
        r, prev = 1, miss[0]
        for m in miss[1:]:
            if m == prev + 1:
                r += 1
            else:
                runs[r] += 1
                r = 1
            prev = m
        runs[r] += 1

    print("1. WHAT A HOLE LOOKS LIKE")
    tot_runs = sum(runs.values())
    for L in sorted(runs):
        if runs[L]:
            print(f"   runs of {L:>2} consecutive missing second(s): "
                  f"{runs[L]:>6} ({pct(runs[L], tot_runs):>5.1f}% of gaps)")
    print(f"   seconds present per window (of {n_el}):")
    for k in sorted(present_hist, reverse=True):
        print(f"      {k:>3}: {present_hist[k]:>5} windows "
              f"({pct(present_hist[k], len(opens)):>5.1f}%)")

    # --------------------------------- 2. is a hole a no-change report?
    same = diff = 0
    for t in opens:
        for i in range(1, n_el - 1):
            s = t - NSEC + i
            if s in g:
                continue
            a, b = g.get(s - 1), g.get(s + 1)
            if a is None or b is None:
                continue
            if a == b:
                same += 1
            else:
                diff += 1
    base_same = base_diff = 0
    for t in opens:
        for i in range(1, n_el - 1):
            s = t - NSEC + i
            if s not in g:
                continue
            a, b = g.get(s - 1), g.get(s + 1)
            if a is None or b is None:
                continue
            if a == b:
                base_same += 1
            else:
                base_diff += 1
    print("\n2. IS A MISSING SECOND A NO-CHANGE REPORT?")
    print(f"   across an isolated hole, price identical : {same:>6} of "
          f"{same+diff:>6} ({pct(same, same+diff):.1f}%)")
    print(f"   across a PRESENT second, same comparison : {base_same:>6} of "
          f"{base_same+base_diff:>6} ({pct(base_same, base_same+base_diff):.1f}%)")
    print("   If the first rate is far higher, holes are deduplicated")
    print("   no-change reports and carrying the last price is exactly right.")

    # ------------------------------------- 3. what do holes cost the call?
    def spot(t):
        for k in range(t, t - TOL - 1, -1):
            if k in g:
                return g[k]

    def tilt_from(vals, s):
        """vals: list of (second, price_or_None) for the elapsed range."""
        k = (sum(v for _, v in vals) + LEAD * s) / NSEC
        return (s - k) / k * 1e4 if k > 0 else None

    rnd = random.Random(SEED)
    complete = [t for t in opens
                if all((t - NSEC + i) in g for i in range(n_el))]
    print(f"\n3. WHAT HOLES COST THE CALL  ({len(complete)} complete windows, "
          f"{len(masks)} real hole patterns to punch into them)")
    if not complete or not masks:
        print("   not enough complete windows or hole patterns yet")
        return
    def punch(secs, keep, s):
        """(rescale_tilt, carry_tilt) for one holed version of a window."""
        kept = set(keep)
        m = sum(g[x] for x in keep) / len(keep)
        # A: rescale the present seconds' mean over the elapsed range —
        #    what bot/strategies/preopen.py does today
        a = tilt_from([(x, m) for x in secs], s)
        # B: carry the last known price across the hole
        fill, b_vals = None, []
        for x in secs:
            if x in kept:
                fill = g[x]
            elif fill is None:
                for back in range(1, 60):
                    if (x - back) in g:
                        fill = g[x - back]
                        break
            b_vals.append((x, fill if fill is not None else m))
        return a, tilt_from(b_vals, s)

    buckets = {}
    for t in complete:
        s = spot(t - LEAD)
        if s is None:
            continue
        secs = [t - NSEC + i for i in range(n_el)]
        truth = tilt_from([(x, g[x]) for x in secs], s)
        if truth is None or abs(truth) < GATE:
            continue
        # REPS distinct real masks per window. Each (window, mask) pair is a
        # separate counterfactual, but pairs sharing a window are NOT
        # independent — read the row counts as resolution, not as a sample
        # size to put a confidence interval on.
        for _ in range(REPS):
            mask = set(rnd.choice(masks))
            keep = [x for i, x in enumerate(secs) if i not in mask]
            if not keep:
                continue
            a, b = punch(secs, keep, s)
            row = buckets.setdefault(len(keep), [0, 0, 0, 0.0, 0.0])
            row[0] += 1
            row[1] += (a is not None and (a >= 0) == (truth >= 0))
            row[2] += (b is not None and (b >= 0) == (truth >= 0))
            row[3] += abs(a - truth) if a is not None else 0.0
            row[4] += abs(b - truth) if b is not None else 0.0

    print(f"   {'kept':>5} {'n':>6} {'rescale ok':>11} {'carry ok':>10} "
          f"{'rescale err':>12} {'carry err':>11}")
    agg = [0, 0, 0, 0.0, 0.0]
    for k in sorted(buckets, reverse=True):
        n, ra, rb, ea, eb = buckets[k]
        for i, v in enumerate((n, ra, rb, ea, eb)):
            agg[i] += v
        print(f"   {k:>5} {n:>6} {pct(ra, n):>10.1f}% {pct(rb, n):>9.1f}% "
              f"{ea/n:>11.3f}bp {eb/n:>10.3f}bp")
    n, ra, rb, ea, eb = agg
    if n:
        print(f"   {'ALL':>5} {n:>6} {pct(ra, n):>10.1f}% {pct(rb, n):>9.1f}% "
              f"{ea/n:>11.3f}bp {eb/n:>10.3f}bp")

    print("\nREAD THE 'kept' COLUMN AGAINST THE FLOOR. The live gate refuses")
    print(f"anything under {n_el * 0.9:.1f} of {n_el} seconds. If agreement is")
    print("still near 100% below that line, the floor is refusing trades for")
    print("nothing and should come down. If it falls away, the floor is right")
    print("and the lead has to move instead of the threshold.")
    print("The two 'err' columns are the average absolute tilt error in bp.")
    print("Whichever is smaller is the imputation the strike should use — and")
    print("that one applies to EVERY window, not only the refused ones.")


if __name__ == "__main__":
    main()
