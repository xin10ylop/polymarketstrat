"""Why did our winner disagree with the exchange's? Bug, or our own imputation?

  venv/bin/python -m bot.mismatch_audit
  DATA=bot/data/preopen-eth COIN=eth venv/bin/python -m bot.mismatch_audit
  DATA=bot/data/preopen-btc15 FAMILY=15m venv/bin/python -m bot.mismatch_audit

WHAT A MISMATCH MEANS AND WHY IT HALTS EVERYTHING. The reconciler recomputes
each settled window's winner from our own Chainlink grid and compares it with
gamma's official outcome. A disagreement means our read of who won is wrong,
which would make every backtest number in this project a fiction — so it is a
STICKY halt in every mode, correctly.

THE TRIPWIRE IS ALREADY GUARDED, which is what makes a recorded mismatch
serious rather than routine. main.py suppresses windows decided by less than
ORACLE_TIE_BPS, and oracle.twap_winner refuses when the two boundary
conventions disagree or coverage is thin. So a flagged window was DECIDED,
not a photo-finish.

THE HYPOTHESIS THIS TESTS. The settlement path calls oracle.twap_at, which
averages the seconds that are PRESENT:

    vals = [samples[s] for s in range(lo, hi) if s in samples]
    return sum(vals) / len(vals)

That is the rescaling estimator. oracle.twap_carry — which the ENTRY path
already uses — instead carries the last print into each hole, and was measured
4x more accurate on btc and 5.5x on eth, with rescaling drifting up to 0.377bp
in its worst bucket. ORACLE_TIE_BPS is 0.3. So rescaling's error can EXCEED
the band that is supposed to keep marginal windows from being flagged, and a
window decided by, say, 0.35bp could be miscalled by our own imputation,
clear the tie filter, and register as a mismatch that no real disagreement
caused. The bot would then be trading on one estimator and auditing itself
with a worse one.

If that is what happened, carry-forward agrees with the exchange on these
windows and the fix is to settle with the same estimator we trade with. If
carry-forward ALSO disagrees, the hypothesis is dead and the problem is real.

The verdict is not taken from the flagged windows alone — three windows
cannot separate two estimators. Every settled window with an official outcome
is scored under both, which is the only comparison with any power.

Read-only: opens the ledger and the grid archive, writes nothing, and does
not clear any halt.
"""
import os
import sqlite3

from bot.config import CFG
from bot.feeds.oracle import Oracle
from bot.twap_verify import COIN, DB_DIR, NSEC, WINDOW

DATA = os.environ.get("DATA", "bot/data/preopen-btc")
MINCOV = CFG.oracle_twap_min_coverage
TIE = CFG.oracle_tie_bps


def naive(o, open_s, close_s):
    """Exactly what the reconciler computes today, via twap_winner's rule."""
    out, worst = {}, 1.0
    for edge in ("left", "right"):
        c, cov_c = o.twap_at(close_s, NSEC, edge)
        k, cov_k = o.twap_at(open_s, NSEC, edge)
        worst = min(worst, cov_c or 0.0, cov_k or 0.0)
        if c is None or k is None or min(cov_c, cov_k) < MINCOV:
            return None, None, None, worst
        out[edge] = ("up" if c >= k else "down", (c - k) / k * 1e4, c, k)
    if out["left"][0] != out["right"][0]:
        return None, None, None, worst        # the edges disagree: no opinion
    w, bp, c, k = out["left"]
    # the REAL coverage, not a placeholder: a rescaled mean that had to skip
    # seconds is exactly the case under investigation, so hiding it behind
    # 1.00 would conceal the evidence this tool exists to find
    return w, bp, (c, k), worst


def carried(o, open_s, close_s):
    """The same rule under the estimator the ENTRY path already trades on."""
    c, cp, ce = o.twap_carry(close_s, NSEC, close_s)
    k, kp, ke = o.twap_carry(open_s, NSEC, open_s)
    if c is None or k is None or k <= 0:
        return None, None, None, 0.0
    cov = min(cp / float(NSEC), kp / float(NSEC))
    if cov < MINCOV:
        return None, None, None, cov
    return ("up" if c >= k else "down"), (c - k) / k * 1e4, (c, k), cov


def main():
    lpath = os.path.join(DATA, "paper.db")
    gpath = os.path.join(DB_DIR, f"{COIN}_1s.db")
    for p in (lpath, gpath):
        if not os.path.exists(p):
            raise SystemExit(f"missing {p}")
    led = sqlite3.connect(f"file:{lpath}?mode=ro", uri=True)
    rows = list(led.execute(
        "SELECT wts, winner, oracle_winner, mismatch FROM settlements "
        "WHERE winner IS NOT NULL ORDER BY wts"))
    if not rows:
        raise SystemExit("no settled windows in this ledger yet")

    o = Oracle(CFG)
    o.samples = dict(sqlite3.connect(f"file:{gpath}?mode=ro", uri=True)
                     .execute("SELECT ts, v FROM px"))
    o.last_sample_s = max(o.samples) if o.samples else 0
    lo, hi = (min(o.samples), max(o.samples)) if o.samples else (0, 0)

    print(f"{DATA}  coin={COIN} window={WINDOW}s twap={NSEC}s  "
          f"tie<{TIE}bp  min_cov={MINCOV}")
    print(f"grid archive: {len(o.samples)} seconds\n")

    flagged = [r for r in rows if r[3]]
    print(f"=== THE {len(flagged)} FLAGGED WINDOW(S) ===")
    if not flagged:
        print("  none in this ledger\n")
    for wts, winner, ow, _ in flagged:
        if not (lo <= wts and wts + WINDOW <= hi):
            print(f"  w{wts}  exchange={winner} ours={ow}  "
                  f"-> OUTSIDE THE ARCHIVE, cannot re-derive")
            continue
        nw, nbp, _, ncov = naive(o, wts, wts + WINDOW)
        cw, cbp, _, ccov = carried(o, wts, wts + WINDOW)
        print(f"  w{wts}  exchange={winner}  bot said={ow}")
        print(f"     rescaled (what settled it): {str(nw):>4}  "
              f"{'n/a' if nbp is None else f'{nbp:+.3f}bp':>10}  cov {ncov:.2f}"
              f"   {'AGREES' if nw == winner else 'disagrees'}")
        print(f"     carried  (what we trade)  : {str(cw):>4}  "
              f"{'n/a' if cbp is None else f'{cbp:+.3f}bp':>10}  cov {ccov:.2f}"
              f"   {'AGREES' if cw == winner else 'disagrees'}")
        if nbp is not None and cbp is not None:
            print(f"     the two estimators differ by {abs(nbp - cbp):.3f}bp "
                  f"on a window decided by {abs(cbp):.3f}bp")
    print()

    # THE PART WITH ANY STATISTICAL POWER. Three flagged windows cannot
    # separate two estimators; every settled window can.
    tally = {"rescaled": [0, 0, 0], "carried": [0, 0, 0]}   # ok, wrong, mute
    gaps = []
    for wts, winner, _, _ in rows:
        if not (lo <= wts and wts + WINDOW <= hi):
            continue
        nw, nbp, _, _ = naive(o, wts, wts + WINDOW)
        cw, cbp, _, _ = carried(o, wts, wts + WINDOW)
        for key, w in (("rescaled", nw), ("carried", cw)):
            if w is None:
                tally[key][2] += 1
            elif w == winner:
                tally[key][0] += 1
            else:
                tally[key][1] += 1
        if nbp is not None and cbp is not None:
            gaps.append(abs(nbp - cbp))
    n = sum(tally["carried"])
    print(f"=== BOTH ESTIMATORS OVER ALL {n} SETTLED WINDOWS IN THE ARCHIVE ===")
    print(f"{'estimator':<12} {'agrees':>7} {'WRONG':>7} {'no call':>8} "
          f"{'accuracy':>9}")
    for key in ("rescaled", "carried"):
        ok, bad, mute = tally[key]
        acc = f"{100*ok/(ok+bad):.1f}%" if ok + bad else "n/a"
        print(f"{key:<12} {ok:>7} {bad:>7} {mute:>8} {acc:>9}")
    if gaps:
        gaps.sort()
        print(f"\ndisagreement between the two, over {len(gaps)} windows: "
              f"median {gaps[len(gaps)//2]:.3f}bp, "
              f"p95 {gaps[int(.95*len(gaps))]:.3f}bp, max {max(gaps):.3f}bp")
        over = sum(1 for g in gaps if g > TIE)
        print(f"windows where they differ by MORE than the {TIE}bp tie band: "
              f"{over}/{len(gaps)} ({100*over/len(gaps):.1f}%)")
        print("Any window in that group can be flagged as a mismatch by the")
        print("choice of estimator alone, with no real disagreement involved.")
    print()
    ok_n, bad_n, _ = tally["rescaled"]
    ok_c, bad_c, _ = tally["carried"]
    if bad_c < bad_n:
        print("CARRY-FORWARD SETTLES MORE WINDOWS CORRECTLY. The bot trades on")
        print("the carried estimator and audits itself with the rescaled one;")
        print("that asymmetry is the defect, and settling with the estimator")
        print("we trade with is the fix.")
    elif bad_c > bad_n:
        print("CARRY-FORWARD IS WORSE HERE. The hypothesis is dead — do not")
        print("change the settlement path on it.")
    elif bad_c == bad_n == 0:
        print("NEITHER ESTIMATOR IS WRONG ON ANY WINDOW THE ARCHIVE CAN PRICE.")
        print("The flagged windows are then not a maths defect at all: the")
        print("LIVE bot's socket saw a thinner grid than this archive holds,")
        print("so the cause is coverage at the moment of settlement, not the")
        print("rule. Carrying still helps, because it is what tolerates holes.")
    else:
        print("THE TWO ARE TIED ON ACCURACY. Prefer the carried estimator")
        print("anyway for consistency with the entry path, but this run is")
        print("not evidence for it.")


if __name__ == "__main__":
    main()
