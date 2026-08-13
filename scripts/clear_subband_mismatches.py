"""Acknowledge mismatches that are inside the MEASURED reconstruction residual.

  venv/bin/python scripts/clear_subband_mismatches.py            # dry run
  venv/bin/python scripts/clear_subband_mismatches.py --apply

WHY THESE ROWS ARE EXPLAINED HISTORY RATHER THAN AN OPEN DEFECT. The mismatch
halt is sticky by design — RiskManager re-halts while the ledger holds any
mismatch row, so a restart alone cannot resume trading, and that is correct.
Normally a mismatch means our read of who won disagrees with the exchange,
i.e. our bug.

These do not. Measured on 809 settled windows across btc and eth
(bot/mismatch_audit.py, bot/twap_align.py):

  - the ESTIMATOR is not the cause: rescaled and carried never differ by more
    than 0.247bp and carry is marginally worse
  - the ALIGNMENT is not the cause: no boundary offset in -4..+4s and no
    window length in N-2..N+2 beats the current one out of sample, on either
    coin independently
  - ZERO errors occur on windows decided by more than 1bp. Every one of the 9
    disagreements lies within 0.421bp of a tie

What remains is the irreducible residual of approximating a published TWAP
STREAM with a uniform mean of 1-second samples. ORACLE_TIE_BPS was 0.3, BELOW
that residual, so the tripwire was firing on our own arithmetic. The band is
now set from the measurement.

SAFETY, and it is the whole point of this file. Only rows whose recomputed
margin is INSIDE the band can be acknowledged. A mismatch outside it cannot
be explained by the residual, is a genuine disagreement, and is refused —
loudly, with a non-zero exit, so that a chained restart does not run. Rows the
archive cannot re-price are also refused, because "cannot check" is not
"fine". The recorded winner and oracle_winner are never touched; only the
`mismatch` flag is cleared, and every change writes an audit event carrying
the measured margin.
"""
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import CFG                                    # noqa: E402

# THE BAND IS PER FAMILY, NOT PER SHELL (audit 2026-08-13). One BAND from
# the calling shell's FAMILY (default 5m -> 0.6) was applied to a list that
# MIXES 5m and 15m ledgers: a 15m row decided by 0.45bp — outside the 15m
# band of 0.3 — "would clear" unless the operator remembered FAMILY=15m.
# The band now travels with each ledger; BAND_BP overrides ALL of them and
# exists for tests only.
_OVERRIDE = os.environ.get("BAND_BP")
_FAM_BAND = {300: 0.6, 900: 0.3}
LEDGERS = [
    ("preopen-btc", "bot/data/preopen-btc", "btc", 300, 30),
    ("preopen-eth", "bot/data/preopen-eth", "eth", 300, 30),
    ("preopen-btc15", "bot/data/preopen-btc15", "btc", 900, 60),
    ("preopen-eth15", "bot/data/preopen-eth15", "eth", 900, 60),
]
GRID = "bot/data/twapcal"
MINCOV = CFG.oracle_twap_min_coverage


def margin_bp(grid, wts, window, n):
    """|close TWAP - open TWAP| in bp, or None if the archive cannot say."""
    def mean(t):
        v = [grid[s] for s in range(t - n, t) if s in grid]
        return (sum(v) / len(v), len(v) / float(n)) if v else (None, 0.0)
    k, ck = mean(wts)
    c, cc = mean(wts + window)
    if k is None or c is None or k <= 0 or min(ck, cc) < MINCOV:
        return None
    return (c - k) / k * 1e4


def main(apply_it):
    # REPO_ROOT exists so the refusal path can be tested against fixtures.
    # Code whose entire job is to REFUSE must be exercised doing it, and a
    # hardcoded root makes that impossible without touching the live ledgers.
    root = os.environ.get(
        "REPO_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    print(f"{'APPLYING' if apply_it else 'DRY RUN'} — acknowledging mismatches "
          f"inside each family's measured residual band "
          f"(5m: {_FAM_BAND[300]}bp, 15m: {_FAM_BAND[900]}bp"
          f"{', OVERRIDDEN to ' + _OVERRIDE + 'bp' if _OVERRIDE else ''})\n")
    grids, cleared, refused = {}, 0, []
    for name, rel, coin, window, n in LEDGERS:
        BAND = float(_OVERRIDE) if _OVERRIDE else _FAM_BAND[window]
        path = os.path.join(root, rel, "paper.db")
        if not os.path.exists(path):
            continue
        db = sqlite3.connect(path)
        try:
            rows = db.execute(
                "SELECT wts, winner, oracle_winner FROM settlements "
                "WHERE mismatch=1 ORDER BY wts").fetchall()
        except sqlite3.Error as e:
            print(f"{name}: unreadable ({str(e)[:50]})")
            continue
        if not rows:
            print(f"{name}: clean")
            continue
        if coin not in grids:
            gp = os.path.join(root, GRID, f"{coin}_1s.db")
            grids[coin] = dict(sqlite3.connect(f"file:{gp}?mode=ro", uri=True)
                               .execute("SELECT ts, v FROM px")) \
                if os.path.exists(gp) else {}
        grid = grids[coin]
        for wts, winner, ow in rows:
            bp = margin_bp(grid, wts, window, n)
            if bp is None:
                refused.append((name, wts, winner, ow,
                                "the archive cannot re-price this window"))
                print(f"  {name} w{wts}: exch={winner} ours={ow}  "
                      f"NOT RE-PRICEABLE — refused")
                continue
            if abs(bp) >= BAND:
                refused.append((name, wts, winner, ow,
                                f"decided by {abs(bp):.3f}bp, outside {BAND}bp"))
                print(f"  {name} w{wts}: exch={winner} ours={ow}  "
                      f"{bp:+.3f}bp  OUTSIDE THE BAND — refused")
                continue
            print(f"  {name} w{wts}: exch={winner} ours={ow}  "
                  f"{bp:+.3f}bp  inside {BAND}bp -> "
                  f"{'clearing' if apply_it else 'would clear'}")
            if apply_it:
                db.execute("UPDATE settlements SET mismatch=0 WHERE wts=?",
                           (wts,))
                db.execute(
                    "INSERT INTO events VALUES(?,?,?)",
                    (time.time(), "mismatch_cleared",
                     f"w{wts} exch={winner} ours={ow} margin={bp:+.4f}bp "
                     f"< band {BAND}bp; measured reconstruction residual, "
                     f"not a disagreement (see LIVE_RUNBOOK 2026-08-11)"))
            cleared += 1
        if apply_it:
            db.commit()

    print(f"\n{cleared} row(s) {'cleared' if apply_it else 'would be cleared'}, "
          f"{len(refused)} refused")
    if refused:
        print("\nREFUSED — THESE ARE NOT EXPLAINED BY THE RESIDUAL:")
        for name, wts, winner, ow, why in refused:
            print(f"  {name} w{wts}  exch={winner} ours={ow}  {why}")
        print("\nA disagreement outside the measured band is a genuine defect.")
        print("Do NOT widen the band to make it go away — that is how a")
        print("tripwire gets trusted after it has stopped working. Diagnose")
        print("it first: bot/mismatch_audit.py and bot/twap_align.py.")
        return 1
    if not apply_it and cleared:
        print("\nRe-run with --apply to clear, then restart the affected units.")
    return 0


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv))
