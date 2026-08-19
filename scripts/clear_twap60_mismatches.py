"""Clear the RULE2 (30s->60s) mismatch flags -- after re-proving EVERY one.

  venv/bin/python scripts/clear_twap60_mismatches.py            # dry run
  venv/bin/python scripts/clear_twap60_mismatches.py --apply

WHAT HAPPENED. At 2026-08-14 00:00 UTC the venue moved the 5m family from
the 30s to the 60s Chainlink TWAP stream (gamma resolutionSource; the
twapLookbackSeconds field no longer exists). Our oracle kept computing the
30s rule; on the ~1.3% of windows where the two averages straddle the
strike differently, the reconciler recorded a winner mismatch, and the
sticky correctness halt stopped both 5m units -- exactly its job. The
flags are real recordings of a real disagreement, but the disagreement was
the VENUE's rule change, not an oracle defect.

WHAT THIS SCRIPT DOES. For every settlements row with mismatch=1 in a 5m
unit's ledger inside [RULE2, VERIFIED_BY2), it recomputes the window under
the NEW rule from the recorder's own 1s grid: strike = carry-forward mean
over [open-60, open), settle = the same over [close-60, close), winner =
up on settle >= strike (ties resolve Up, unchanged). Each window is then
classed:

  AGREE      60s winner matches the exchange, decided beyond the tie band
  NEAR-TIE   |margin| inside ORACLE_TIE_BPS -- the 60s read has no strong
             opinion, which also invalidates the 30s-based flag
  DISAGREE   the 60s winner ALSO contradicts the exchange: NOT explained
             by the rule change -- a live defect
  NO-GRID    the recorder cannot cover both boundary means at >=90%

--apply clears flags ONLY when there are zero DISAGREE and zero NO-GRID
windows in that unit; otherwise it refuses and changes nothing -- paste
the output. Windows outside [RULE2, VERIFIED_BY2) are never touched, and
anything flagged after VERIFIED_BY2 is a NEW incident, not this one.

ALLOW_UNVERIFIED_WTS -- the surgical override for a permanent blackout.
2026-08-19: one btc window (1786750200) sits in a total recorder outage
(strike minute 0/60 seconds); nothing anywhere holds that minute's
oracle values, so it can never be verified. With 36/37 windows verified
and zero DISAGREE, its flag -- produced by comparing the WRONG rule --
protects nothing. Naming a window here accepts it as unverifiable:
  ALLOW_UNVERIFIED_WTS=1786750200 ... --apply
Only a NO-GRID window can be accepted; a DISAGREE always refuses, listed
or not. The acceptance is printed and written into the clearing event.
"""
import glob
import os
import sqlite3
import sys
import time

RULE2 = 1786665600            # 2026-08-14 00:00 UTC
VERIFIED_BY2 = 1787184000     # 2026-08-20 00:00 UTC
N = 60                        # the new rule's lookback
TIE_BPS = float(os.environ.get("TIE_BPS", "0.6"))
MIN_COVER = float(os.environ.get("MIN_COVER", "0.9"))
PAPER_GLOB = os.environ.get("PAPER_GLOB", "bot/data/preopen-*/paper.db")
TWAP_DIR = os.environ.get("TWAP_DIR", "bot/data/twapcal")
APPLY = "--apply" in sys.argv
ALLOW = {int(x) for x in
         os.environ.get("ALLOW_UNVERIFIED_WTS", "").split(",") if x.strip()}


def mean60(g, a, b):
    """Carry-forward mean over [a, b) and its coverage fraction."""
    carry, total, present = None, 0.0, 0
    for back in range(1, 121):
        if (a - back) in g:
            carry = g[a - back]
            break
    for s in range(a, b):
        v = g.get(s)
        if v is not None:
            carry, present = v, present + 1
        if carry is None:
            return None, 0.0
        total += carry
    return total / (b - a), present / (b - a)


def unit(path):
    name = os.path.basename(os.path.dirname(path))
    coin = "eth" if "eth" in name else "btc"
    if "15" in name:
        print(f"\n{name}: 15m family -- the rule never changed here; "
              f"any mismatch is NOT this incident. Skipped.")
        return True
    gp = os.path.join(TWAP_DIR, f"{coin}_1s.db")
    if not os.path.exists(gp):
        print(f"\n{name}: missing grid {gp} -- cannot verify, REFUSED.")
        return False
    g = dict(sqlite3.connect(f"file:{gp}?mode=ro", uri=True)
             .execute("SELECT ts, v FROM px"))
    db = sqlite3.connect(path)
    rows = db.execute(
        "SELECT wts, winner FROM settlements WHERE mismatch=1 "
        "AND wts >= ? AND wts < ? ORDER BY wts",
        (RULE2, VERIFIED_BY2)).fetchall()
    out_of_scope = db.execute(
        "SELECT COUNT(*) FROM settlements WHERE mismatch=1 "
        "AND (wts < ? OR wts >= ?)", (RULE2, VERIFIED_BY2)).fetchone()[0]
    print(f"\n{name}: {len(rows)} flagged windows in scope"
          + (f"  (!! {out_of_scope} flagged OUTSIDE the rule-change era -- "
             f"left untouched, investigate separately)" if out_of_scope
             else ""))
    if not rows:
        return True
    win_len = 300
    counts = {"AGREE": 0, "NEAR-TIE": 0, "DISAGREE": 0, "NO-GRID": 0,
              "ACCEPTED": 0}
    accepted = []
    print(f"{'window':>12} {'utc':>16} {'exchange':>9} {'60s says':>9} "
          f"{'margin bp':>10} {'class':>9}")
    for wts, winner in rows:
        k, ck = mean60(g, wts - N, wts)
        st, cs = mean60(g, wts + win_len - N, wts + win_len)
        if k is None or st is None or ck < MIN_COVER or cs < MIN_COVER:
            cls, w60, marg = "NO-GRID", "?", float("nan")
            if wts in ALLOW:
                cls = "ACCEPTED"
                accepted.append(wts)
        else:
            marg = (st - k) / k * 1e4
            w60 = "up" if st >= k else "down"
            if abs(marg) < TIE_BPS:
                cls = "NEAR-TIE"
            elif w60 == winner:
                cls = "AGREE"
            else:
                cls = "DISAGREE"
        counts[cls] += 1
        print(f"{wts:>12} "
              f"{time.strftime('%m-%d %H:%M', time.gmtime(wts)):>16} "
              f"{winner:>9} {w60:>9} {marg:>+10.3f} {cls:>9}")
    print(f"  {counts}")
    if counts["DISAGREE"] or counts["NO-GRID"]:
        print(f"  REFUSED: {counts['DISAGREE']} window(s) the new rule does "
              f"NOT explain and {counts['NO-GRID']} unverifiable -- nothing "
              f"changed. Paste this output before doing anything else.")
        return False
    if APPLY:
        db.execute(
            "UPDATE settlements SET mismatch=0 WHERE mismatch=1 "
            "AND wts >= ? AND wts < ?", (RULE2, VERIFIED_BY2))
        db.execute(
            "INSERT INTO events VALUES(?, 'rule2_migration', ?)",
            (time.time(), f"cleared {len(rows)} RULE2 mismatch flags "
             f"({counts['AGREE']} agree, {counts['NEAR-TIE']} near-tie"
             + (f", {counts['ACCEPTED']} accepted-unverifiable "
                f"{accepted} (recorder blackout)" if accepted else "")
             + f") after 60s re-verification against the recorded grid "
             f"at min_cover {MIN_COVER}"))
        db.commit()
        print(f"  APPLIED: {len(rows)} flags cleared; the sticky halt "
              f"releases on the next unit restart.")
    else:
        print("  DRY RUN (would clear all of the above; re-run with "
              "--apply).")
    return True


def main():
    print(f"RULE2 mismatch migration, era [{RULE2}, {VERIFIED_BY2}) = "
          f"[2026-08-14 00:00, 2026-08-20 00:00) UTC, tie band "
          f"{TIE_BPS}bp, {'APPLY' if APPLY else 'DRY RUN'}")
    paths = sorted(glob.glob(PAPER_GLOB))
    if not paths:
        raise SystemExit(f"no ledgers match {PAPER_GLOB}")
    ok = True
    for p in paths:
        ok = unit(p) and ok
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
