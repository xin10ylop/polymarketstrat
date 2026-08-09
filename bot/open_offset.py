"""The market now OPENS off-strike. Measure it.

  venv/bin/python -m bot.open_offset
  COIN=eth FAMILY=15m venv/bin/python -m bot.open_offset

THE POINT, IN ONE PARAGRAPH.

Before 2026-08-07 the strike was spot at the open: at T the price WAS the
strike, by construction, and the market was a true coin flip. Since the rule
change the strike is a TRAILING AVERAGE — the 30s mean ending at T (60s for
15m). A trailing mean lags. So at the instant the window opens, spot already
sits some distance above or below the number it will be judged against, and
the closing average is centred on spot, not on the strike. The market does
not open at 50/50. It opens tilted, and the tilt is knowable at T+0 from the
grid alone, with no forecast of any kind.

This did not exist before. It is not the old edge relocated — under
spot/spot settlement the offset is identically zero. The rule change
manufactured it.

WHY IT IS WORTH THE TROUBLE. Every study since the cutover has aimed at the
last few seconds of the window, where the book is empty and the winning side
costs 0.98. This lives at T+0, where both sides are quoted hundreds deep at
0.49/0.51 — the price region that produced 46% of all historical profit, and
the region where the drought does not exist.

WHAT THIS FILE DOES. Grid plus official outcomes, nothing else — no book, so
nothing here is a P&L claim. It measures how often the tilted side actually
wins, bucketed by how tilted it was, and prints the price you would have to
buy at to break even. It also runs a shuffle control: the same computation
with each window's offset paired to a NEIGHBOUR's outcome, which must come
out at chance. If the control shows signal, the harness is broken and the
real column means nothing.

Read-only.
"""
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor

from bot.twap_verify import COIN, FAMILY, NSEC, WINDOW, load_grid, official

# seconds after the window OPENS. 0 is the structural one: the tilt that
# exists before anybody has traded a thing.
PROBES = [int(x) for x in os.environ.get("PROBES", "0,15,30,60,120").split(",")]
EDGES = [0.0, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 1e9]        # |offset| in bp
MIN_COVER = 0.9


def fee(p):
    return 0.07 * p * (1 - p)


def wilson(k, n):
    if not n:
        return (0.0, 1.0)
    z, p = 1.96, k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, c - h), min(1.0, c + h))


def main():
    g = load_grid()
    lo, hi = min(g), max(g)
    wtss = [w for w in range(lo - lo % WINDOW, hi + WINDOW, WINDOW)
            if w - NSEC >= lo and w + WINDOW <= hi]
    with ThreadPoolExecutor(16) as ex:
        won = {w: r for w, r in ex.map(official, wtss) if r}
    print(f"{COIN} {FAMILY}: grid {(hi-lo)/3600:.1f}h, {len(wtss)} complete "
          f"windows, {len(won)} with an official outcome\n")
    if len(won) < 30:
        print("too few settled windows to say anything — record longer")
        return

    def mean(a, b):
        v = [g[s] for s in range(a, b) if s in g]
        return (sum(v) / len(v), len(v) / (b - a)) if v else (None, 0.0)

    def spot(t):
        for kk in (t, t - 1, t - 2):
            if kk in g:
                return g[kk]
        return None

    # per-second log vol, straight off the grid: the yardstick for judging
    # whether the measured tilt is the size the diffusion says it should be
    xs = sorted(g)
    d = [math.log(g[b] / g[a]) for a, b in zip(xs, xs[1:]) if b - a == 1]
    sig = (sum(x * x for x in d) / len(d)) ** 0.5 if d else 0.0
    print(f"grid vol: {sig*1e4:.3f} bp/s  ->  a 5m window moves "
          f"{sig*math.sqrt(WINDOW)*1e4:.1f} bp (1 sd)\n")

    order = sorted(won)
    for P in PROBES:
        obs = []
        for i, w in enumerate(order):
            k, ck = mean(w - NSEC, w)
            if k is None or ck < MIN_COVER:
                continue
            s = spot(w + P)
            if s is None:
                continue
            off = (s - k) / k * 1e4
            pick = "up" if off >= 0 else "down"
            # far shuffle, not the neighbour: adjacent windows share a trend,
            # so a neighbour control can show real signal and prove nothing
            shuf = won[order[(i + len(order) // 2) % len(order)]]
            obs.append((abs(off), pick == won[w], pick == shuf))
        if not obs:
            print(f"T+{P}s: no covered windows")
            continue
        rem = WINDOW - P
        print(f"--- probe T+{P}s ({rem}s to close) --- {len(obs)} windows, "
              f"tilt sd {(sum(o[0]**2 for o in obs)/len(obs))**0.5:.2f}bp")
        print(f"{'|tilt| bp':>12} {'n':>5} {'win%':>6} {'95% CI':>13} "
              f"{'model':>6} {'break-even buy':>15} {'shuffle':>8}")
        for a, b in zip(EDGES, EDGES[1:]):
            sel = [o for o in obs if a <= o[0] < b]
            if len(sel) < 5:
                continue
            n = len(sel)
            kk = sum(o[1] for o in sel)
            clo, chi = wilson(kk, n)
            mid = sum(o[0] for o in sel) / n
            # what a pure diffusion says the tilt is worth, as a yardstick
            z = (mid * 1e-4) / (sig * math.sqrt(rem)) if sig and rem else 0.0
            model = 0.5 * (1 + math.erf(z / math.sqrt(2)))
            # highest price at which the LOWER bound still breaks even
            be = clo
            for _ in range(40):
                be -= (be + fee(be) - clo) / (1 + 0.07 * (1 - 2 * be))
            lab = f"{a:.1f}-{b:.1f}" if b < 1e8 else f"{a:.1f}+"
            print(f"{lab:>12} {n:>5} {100*kk/n:>5.0f}% "
                  f"[{100*clo:>4.0f},{100*chi:>4.0f}]% {100*model:>5.0f}% "
                  f"{be:>15.3f} {100*sum(o[2] for o in sel)/n:>7.0f}%")
        allk = sum(o[1] for o in obs)
        allc = sum(o[2] for o in obs)
        clo, chi = wilson(allk, len(obs))
        print(f"{'ALL':>12} {len(obs):>5} {100*allk/len(obs):>5.0f}% "
              f"[{100*clo:>4.0f},{100*chi:>4.0f}]% {'':>6} {'':>15} "
              f"{100*allc/len(obs):>7.0f}%")
        print()

    print("'model' = what a random walk says the tilt is worth. Beating it")
    print("badly in either direction means the tilt is not what moves the")
    print("outcome and the story is wrong, however good the win rate looks.")
    print("'break-even buy' = the most you could pay, after the 0.07p(1-p)")
    print("taker fee, and still not lose AT THE 95% LOWER BOUND. Compare it")
    print("to what the book charges at T+0. If it is below ~0.52 there is")
    print("nothing here; the open is quoted 0.49/0.51.")
    print("'shuffle' pairs each window's tilt with its neighbour's outcome.")
    print("It must sit at 50%. If it does not, ignore every other number.")


if __name__ == "__main__":
    main()
