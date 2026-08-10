"""Why does the pre-open bot refuse windows? Separate FEED HOLES from FEED LAG.

  venv/bin/python -m bot.preopen_diag
  COIN=eth venv/bin/python -m bot.preopen_diag
  HOURS=48 LEAD=3 venv/bin/python -m bot.preopen_diag

THE SYMPTOM. The first live session skipped 'no_grid' on 2 of 4 windows. That
refusal is not a small thing: at ~288 windows a day, a 30-50% no_grid rate is
half the strategy's trade count thrown away before any signal is even tested.

no_grid fires when either
  (a) no Chainlink print exists within 3s of T-lead, or
  (b) fewer than 90% of the elapsed strike seconds [T-30, T-lead) are present.

There are exactly two ways (b) can happen and they need OPPOSITE responses:

  HOLES  the 1s grid genuinely skips seconds. Then the coverage gate is doing
         its job, imputing across the hole would be guessing, and the honest
         answer is that lead=3 is too tight for this feed.

  LAG    the grid is complete but the newest seconds have not ARRIVED yet.
         The bot evaluates at wall-clock T-3.6..T-3.0, and a sample stamped
         T-4 reaches us a beat later. Those seconds are not missing, they are
         merely in the future of our socket — and the strike formula ALREADY
         imputes the not-yet-elapsed tail from the last print. Refusing here
         applies a hole rule to something that is not a hole.

This tool tells them apart from data already on disk, because the archive
recorded by bot.twap_record has the true grid with no arrival timing: any
second the archive holds but the live bot lacked was a LAG, by definition.

WHAT IT PRINTS
  1. Grid completeness over [T-30, T-lead) across the whole archive — the
     refusal rate attributable to holes alone.
  2. The same rate simulated at assumed arrival lags of 0..5s. The live
     no_grid rate should land on one of those rows, which identifies the lag
     we actually run at.
  3. The refusal rate under a HORIZON-AWARE coverage rule: score coverage
     only over seconds that had arrived, and impute everything past the
     arrival horizon from the last print, exactly as the unelapsed tail is
     already imputed. This is the candidate fix, and the column to read is
     whether it still refuses the genuinely holed windows.

Read-only. Never trades, touches no bot state, writes nothing.
"""
import os
import statistics as stat

from bot.twap_verify import COIN, FAMILY, NSEC, WINDOW, load_grid

LEAD = float(os.environ.get("LEAD", "3"))
COVER = float(os.environ.get("COVER", "0.9"))
HOURS = float(os.environ.get("HOURS", "0"))          # 0 = whole archive
LAGS = [int(x) for x in os.environ.get("LAGS", "0,1,2,3,4,5").split(",")]
TOL = 3          # price_at(..., tolerance=3) in the strategy


def pct(k, n):
    return 100.0 * k / n if n else 0.0


def main():
    g = load_grid()
    if not g:
        raise SystemExit("no grid archive — is bot.twap_record running?")
    lo, hi = min(g), max(g)
    if HOURS:
        lo = max(lo, hi - int(HOURS * 3600))
    # every window OPEN in the archive whose full strike window is covered
    first = ((int(lo) + NSEC) // WINDOW + 1) * WINDOW
    opens = [t for t in range(first, int(hi) + 1, WINDOW) if t - NSEC >= lo]
    if not opens:
        raise SystemExit("archive too short for a full window yet")
    span_h = (opens[-1] - opens[0] + WINDOW) / 3600.0
    edge = int(LEAD)
    n_el = NSEC - edge                      # elapsed strike seconds at T-lead
    need = n_el * COVER

    print(f"{COIN} {FAMILY}: {len(opens)} window opens over {span_h:.1f}h of "
          f"archive, lead {LEAD:g}s, coverage floor {COVER:.2f}")
    print(f"strike = mean over [T-{NSEC}, T); at T-{edge} that is {n_el} "
          f"elapsed seconds, so the gate needs {need:.1f} of them present\n")

    # ---------------------------------------------------------------- holes
    present, gaps, right_gap = [], 0, []
    for t in opens:
        p = sum(1 for s in range(t - NSEC, t - edge) if s in g)
        present.append(p)
        if p < n_el:
            gaps += 1
        newest = max((s for s in range(t - NSEC, t - edge) if s in g),
                     default=None)
        right_gap.append(None if newest is None else (t - edge - 1) - newest)

    full = sum(1 for p in present if p == n_el)
    print("1. IS THE GRID ITSELF COMPLETE?")
    print(f"   windows with all {n_el} seconds present : {full:>6} "
          f"({pct(full, len(opens)):.1f}%)")
    print(f"   windows missing at least one second     : {gaps:>6} "
          f"({pct(gaps, len(opens)):.1f}%)")
    print(f"   median seconds present                  : "
          f"{stat.median(present):>6.0f} / {n_el}")
    hole_refuse = sum(1 for p in present if p < need)
    print(f"   REFUSED on holes alone                  : {hole_refuse:>6} "
          f"({pct(hole_refuse, len(opens)):.1f}%)")

    # ------------------------------------------------------------ lag sweep
    print("\n2. REFUSAL RATE IF SAMPLES ARRIVE L SECONDS LATE")
    print("   (the live no_grid rate identifies which row we run at)")
    print(f"   {'lag':>4} {'seen/elapsed':>13} {'cover':>7} {'no_spot':>8} "
          f"{'refused':>9} {'rate':>7}")
    for L in LAGS:
        seen, no_spot, refused = [], 0, 0
        for t in opens:
            horizon = t - edge - L          # newest second the socket holds
            p = sum(1 for s in range(t - NSEC, min(t - edge, horizon + 1))
                    if s in g)
            seen.append(p)
            # the strategy also needs a print within TOL seconds of T-lead,
            # searching BACKWARD only — so it may reach past the horizon
            spot = any((s in g) for s in range(t - edge - TOL,
                                               min(t - edge, horizon) + 1))
            if not spot:
                no_spot += 1
            if not spot or p < need:
                refused += 1
        m = stat.median(seen)
        print(f"   {L:>4} {f'{m:.0f}/{n_el}':>13} {m/n_el:>7.2f} "
              f"{no_spot:>8} {refused:>9} {pct(refused, len(opens)):>6.1f}%")

    # -------------------------------------------------------- candidate fix
    print("\n3. HORIZON-AWARE COVERAGE (the candidate fix)")
    print("   Score coverage over ARRIVED seconds only; impute the rest from")
    print("   the last print, exactly as the unelapsed tail already is.")
    print(f"   {'lag':>4} {'refused':>9} {'rate':>7} {'vs strict':>10} "
          f"{'max imputed':>12}")
    for L in LAGS:
        refused, strict, imputed = 0, 0, []
        for t in opens:
            horizon = t - edge - L
            arrived = [s for s in range(t - NSEC, t - edge) if s <= horizon]
            p = sum(1 for s in arrived if s in g)
            spot = any((s in g) for s in range(t - edge - TOL,
                                               min(t - edge, horizon) + 1))
            if not spot or p < need:
                strict += 1
            # holes are still holes: coverage is judged against the seconds
            # that COULD have arrived, not against all elapsed seconds
            if not spot or not arrived or p < len(arrived) * COVER:
                refused += 1
            else:
                imputed.append(NSEC - len(arrived))
        print(f"   {L:>4} {refused:>9} {pct(refused, len(opens)):>6.1f}% "
              f"{pct(strict, len(opens)):>9.1f}% "
              f"{max(imputed) if imputed else 0:>12}")

    # ------------------------------------------------------ 4. floor sweep
    # grid_holes showed the picked side is unchanged from the complete-grid
    # call at 21-24 of 27 seconds (84/84 btc, 47/47 eth) — the exact region
    # the 0.9 floor refuses. So the floor is a free parameter and this is
    # what each setting costs, at the lag we actually run at.
    print("\n4. WHAT EACH COVERAGE FLOOR REFUSES")
    print("   'blackout' = no print at all in the range; those must always")
    print("   refuse and no floor can recover them.")
    print(f"   {'floor':>6} {'needs':>7}" +
          "".join(f"{f'lag {L}':>8}" for L in LAGS) + f"{'blackout':>10}")
    for fl in [float(x) for x in
               os.environ.get("FLOORS", "0.9,0.8,0.75,0.7,0.6").split(",")]:
        cells = []
        for L in LAGS:
            refused = 0
            for t in opens:
                horizon = t - edge - L
                p = sum(1 for s in range(t - NSEC, min(t - edge, horizon + 1))
                        if s in g)
                spot = any((s in g) for s in range(t - edge - TOL,
                                                   min(t - edge, horizon) + 1))
                if not spot or p < n_el * fl:
                    refused += 1
            cells.append(pct(refused, len(opens)))
        black = sum(1 for t in opens
                    if not any((s in g) for s in range(t - NSEC, t - edge)))
        print(f"   {fl:>6.2f} {n_el*fl:>7.1f}"
              + "".join(f"{c:>7.1f}%" for c in cells)
              + f"{pct(black, len(opens)):>9.1f}%")

    print("\nREAD IT LIKE THIS. If row 1 says the grid is essentially complete")
    print("and section 2 shows the refusal rate climbing with lag, then the")
    print("live no_grid is LAG and section 3 is the fix — it refuses only the")
    print("windows with real holes. If row 1 already refuses at the live rate,")
    print("the feed has holes, the gate is correct, and the lead must move")
    print("instead. 'max imputed' is the largest number of the 30 strike")
    print("seconds the fix would ever fill from one print; if that number is")
    print("large the fix is imputing too much and must not ship.")


if __name__ == "__main__":
    main()
