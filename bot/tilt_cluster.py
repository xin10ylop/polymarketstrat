"""Are these bets independent? Run lengths, autocorrelation, and cross-coin.

  venv/bin/python -m bot.tilt_cluster
  COIN=eth OTHER=btc GATE=1.0 venv/bin/python -m bot.tilt_cluster

WHY THIS EXISTS, AND IT IS NOT A DOUBT ABOUT THE EDGE. The first five live
positions came back and every one of them picked DOWN — btc at -3.67, -2.17
and -1.68bp, eth at -2.59 and -3.74bp. Worse, the two coins agreed window for
window: both won the 13:40 window and both lost the 13:55 one, for +$122/+$122
and -$122/-$132.

That is not a coincidence to be waved at, it is the mechanism doing exactly
what it says. The tilt is spot minus a trailing mean, so in a sustained move
the tilt carries the sign of the move and consecutive windows pick the same
side. And btc and eth move together over five minutes, so the two bots are
not two bets, they are one bet in two places.

TWO THINGS FOLLOW, and both are about SIZE and CONFIDENCE, not about whether
the edge exists:

  1. Every confidence interval quoted on this strategy so far — the 60.4%
     settle rate, the '95% lower bound +5.68c' — assumed independent windows.
     If the win indicator is autocorrelated, the effective sample is smaller
     than the row count and those bounds are too tight. This computes n_eff
     from the measured lag-1 autocorrelation and re-derives the interval.

  2. Running btc and eth is not diversification if they pick the same side
     and win together. It is a double clip on one bet, which is a sizing
     decision that should be made deliberately rather than by accident.

SELF-SETTLED, NOT VIA GAMMA. The winner is TWAP(close) >= TWAP(open), both
computable from the archived grid, which bot/twap_verify.py scored against
official outcomes at 100%. That keeps this instant and rate-limit-free over
1,200 windows instead of a few thousand gamma calls; the price is that a
window whose grid coverage is thin at either end is skipped rather than
guessed.

Read-only. Never trades, touches no bot state, writes nothing.
"""
import os
import sqlite3

from bot.twap_verify import COIN, DB_DIR, FAMILY, NSEC, WINDOW, load_grid

LEAD = int(os.environ.get("LEAD", "3"))
GATE = float(os.environ.get("GATE", "0.5"))
COVER = float(os.environ.get("COVER", "0.75"))
OTHER = os.environ.get("OTHER", "eth" if COIN == "btc" else "btc").lower()


def wilson(k, n):
    if not n:
        return (0.0, 1.0)
    z, p = 1.96, k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, c - h), min(1.0, c + h))


def load(coin):
    path = os.path.join(DB_DIR, f"{coin}_1s.db")
    if not os.path.exists(path):
        return {}
    return dict(sqlite3.connect(path).execute("SELECT ts, v FROM px"))


def calls(g):
    """{window_open: (pick, won, tilt_bp)} for every window we could trade."""
    if not g:
        return {}
    lo, hi = min(g), max(g)
    first = ((int(lo) + NSEC) // WINDOW + 1) * WINDOW
    out = {}
    for t in range(first, int(hi) - WINDOW + 1, WINDOW):
        if t - NSEC < lo or t + WINDOW > hi:
            continue
        # --- the call, from only what exists at T-lead (carry-forward)
        carry, total, present = None, 0.0, 0
        for back in range(1, 121):
            if (t - NSEC - back) in g:
                carry = g[t - NSEC - back]
                break
        for s in range(t - NSEC, t - LEAD):
            v = g.get(s)
            if v is not None:
                carry, present = v, present + 1
            if carry is None:
                break
            total += carry
        if carry is None or present < (NSEC - LEAD) * COVER:
            continue
        spot = None
        for s in range(t - LEAD, t - LEAD - 4, -1):
            if s in g:
                spot = g[s]
                break
        if spot is None:
            continue
        k = (total + LEAD * spot) / NSEC
        if k <= 0:
            continue
        tilt = (spot - k) / k * 1e4
        if abs(tilt) < GATE:
            continue
        # --- self-settle: TWAP(close) >= TWAP(open), both ends fully covered
        def twap(end):
            v = [g[s] for s in range(end - NSEC, end) if s in g]
            return (sum(v) / len(v), len(v) / NSEC) if v else (None, 0.0)
        o, co = twap(t)
        c, cc = twap(t + WINDOW)
        if o is None or c is None or min(co, cc) < 0.9:
            continue
        pick = "up" if tilt >= 0 else "down"
        out[t] = (pick, ("up" if c >= o else "down") == pick, tilt)
    return out


def runs(seq):
    """Run-length counts of a boolean/str sequence."""
    r, out, prev = 0, {}, object()
    for x in seq:
        if x == prev:
            r += 1
        else:
            if r:
                out[r] = out.get(r, 0) + 1
            r, prev = 1, x
    if r:
        out[r] = out.get(r, 0) + 1
    return out


def autocorr(seq):
    """Lag-1 autocorrelation of a 0/1 sequence."""
    n = len(seq)
    if n < 3:
        return 0.0
    m = sum(seq) / n
    num = sum((seq[i] - m) * (seq[i + 1] - m) for i in range(n - 1))
    den = sum((x - m) ** 2 for x in seq)
    return num / den if den else 0.0


def main():
    a = calls(load_grid())
    b = calls(load(OTHER))
    if not a:
        raise SystemExit("no tradeable windows in the archive yet")
    ts = sorted(a)
    picks = [a[t][0] for t in ts]
    wins = [1 if a[t][1] else 0 for t in ts]
    n, k = len(wins), sum(wins)
    print(f"{COIN} {FAMILY}: {n} tradeable windows at |tilt| >= {GATE}bp, "
          f"self-settled, lead {LEAD}s\n")

    print("1. DOES IT PICK ONE SIDE?")
    up = picks.count("up")
    print(f"   up {up} ({100*up/n:.1f}%)   down {n-up} ({100*(n-up)/n:.1f}%)")
    print(f"   win rate {100*k/n:.1f}%  ({k}/{n})")

    print("\n2. DO CONSECUTIVE WINDOWS PICK THE SAME SIDE?")
    rp = runs(picks)
    tot = sum(rp.values())
    print(f"   {'run':>4} {'count':>7} {'share':>7} {'if independent':>15}")
    for L in sorted(rp)[:8]:
        print(f"   {L:>4} {rp[L]:>7} {100*rp[L]/tot:>6.1f}% "
              f"{100*0.5**L:>14.1f}%")
    longest = max(rp) if rp else 0
    print(f"   longest run of one side: {longest} windows "
          f"({longest*WINDOW/60:.0f} minutes on the same bet)")

    print("\n3. WHAT THAT DOES TO THE CONFIDENCE INTERVAL")
    r = autocorr(wins)
    n_eff = n * (1 - r) / (1 + r) if r > -1 else n
    lo1, hi1 = wilson(k, n)
    lo2, hi2 = wilson(round(k * n_eff / n), round(n_eff))
    print(f"   lag-1 autocorrelation of the WIN indicator : {r:+.4f}")
    print(f"   rows {n}  ->  effective sample {n_eff:.0f}")
    print(f"   win rate 95% CI assuming independence : "
          f"[{100*lo1:.1f}, {100*hi1:.1f}]%")
    print(f"   win rate 95% CI at the effective size : "
          f"[{100*lo2:.1f}, {100*hi2:.1f}]%")
    print(f"   break-even needs 51-53% depending on entry, so the number that")
    print(f"   matters is whether the LOWER bound clears it: "
          f"{100*lo2:.1f}% at the honest sample size.")

    if b:
        print(f"\n4. ARE {COIN.upper()} AND {OTHER.upper()} THE SAME BET?")
        both = sorted(set(a) & set(b))
        if len(both) < 10:
            print(f"   only {len(both)} shared windows — not enough yet")
        else:
            same_pick = sum(1 for t in both if a[t][0] == b[t][0])
            same_win = sum(1 for t in both if a[t][1] == b[t][1])
            both_win = sum(1 for t in both if a[t][1] and b[t][1])
            both_lose = sum(1 for t in both if not a[t][1] and not b[t][1])
            m = len(both)
            print(f"   {m} windows where both coins would trade")
            print(f"   same side picked      : {same_pick:>5} ({100*same_pick/m:.1f}%)")
            print(f"   same outcome          : {same_win:>5} ({100*same_win/m:.1f}%)")
            print(f"   won together          : {both_win:>5} ({100*both_win/m:.1f}%)")
            print(f"   lost together         : {both_lose:>5} ({100*both_lose/m:.1f}%)")
            print("   At 50% these are independent bets and running both")
            print("   halves the variance per dollar. Well above 50% and the")
            print("   second bot is a bigger clip on the first bot's bet,")
            print("   which is a sizing decision, not diversification.")

    print("\nNONE OF THIS SAYS THE EDGE IS ABSENT. It says the bets arrive in")
    print("clumps, so a good hour and a bad hour both overstate their case,")
    print("and two coins are not two independent chances at the same edge.")


if __name__ == "__main__":
    main()
