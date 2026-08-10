"""AHL's one transferable rule: divide the signal by volatility.

  venv/bin/python -m bot.vol_tilt
  COIN=eth venv/bin/python -m bot.vol_tilt

WHERE THIS CAME FROM. A ManAHL-style multi-horizon momentum score —
sum of sign(close - close[n]) over four lookbacks, scaled by 1/vol — was
tested on this market and the SIGNAL part is worthless here: 16 lookback
sets across two coins, split-sample, best pooled t = 1.66, nothing above
52% in both halves. It cannot work, and the reason is arithmetic rather
than bad luck. AHL's whole edge is about 0.12 points of win rate per 5
minutes; the taker fee at 0.50 is 1.75 points. You would need fourteen
times their edge to pay the toll. They hold for weeks and pay it a few
times a month; a 5m binary pays it 288 times a day.

But the OTHER half of their rule — position = signal / vol — is not a
forecast, it is a statement about what a signal is worth, and that part
transfers exactly. The opening tilt is a distance in basis points. What it
is worth is that distance measured in standard deviations. Those are the
same thing only if vol is constant, and it isn't.

MEASURED, 6,000 windows per coin, and this is what motivated the tool:
ranking windows by |tilt|/vol beat ranking by |tilt| in 8 of 8 matched
slices across both coins. Same tilt in basis points, split by regime:
    btc  calm third 66.3%   middle 60.5%   wild third 56.0%
    eth  calm third 61.9%   middle 58.4%   wild third 56.2%
And against real prices the book's error is mechanical: it moves its quote
+0.0975 per bp of tilt in calm windows when the outcome actually moves
+0.2342 — it pays 42% of value. In wild windows it pays 67%. The book
anchors on a roughly fixed basis-points-to-probability mapping and
under-adjusts when the market is quiet.

This scores that on the REAL Chainlink grid rather than the Binance
stand-in those numbers came from. Read-only.
"""
import json
import math
import os
import statistics as st
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from bot.twap_verify import COIN, HDRS, NSEC, WINDOW, load_grid

MAXAGE = int(os.environ.get("MAXAGE", "45"))
SPREAD = float(os.environ.get("SPREAD", "0.005"))
CLIP = float(os.environ.get("CLIP", "250"))
VOL_BARS = int(os.environ.get("VOL_BARS", "42"))
SLICES = [0.05, 0.10, 0.20, 0.35]
SLUG = f"{COIN}-updown-{'15m' if WINDOW == 900 else '5m'}-"


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


def get(url, tries=4):
    for attempt in range(tries):
        try:
            return json.load(urllib.request.urlopen(
                urllib.request.Request(url, headers=HDRS), timeout=30))
        except Exception:  # noqa: BLE001
            if attempt == tries - 1:
                return None
            time.sleep(0.5 * (2 ** attempt))


def pull(wts):
    a = get(f"https://gamma-api.polymarket.com/markets?slug={SLUG}{wts}&closed=true")
    if not a:
        return wts, None, []
    m = a[0]
    toks, outs, pr = m["clobTokenIds"], m["outcomes"], m["outcomePrices"]
    if isinstance(toks, str):
        toks = json.loads(toks)
    if isinstance(outs, str):
        outs = json.loads(outs)
    if isinstance(pr, str):
        pr = json.loads(pr)
    by = {o.lower(): t for o, t in zip(outs, toks)}
    hit = [o.lower() for o, p in zip(outs, pr) if float(p) == 1.0]
    win = hit[0] if len(hit) == 1 else None
    if "up" not in by or win is None:
        return wts, win, []
    h = get(f"https://clob.polymarket.com/prices-history?market={by['up']}"
            f"&startTs={wts - 60}&endTs={wts + WINDOW}&fidelity=1")
    return wts, win, [(p["t"], p["p"]) for p in ((h or {}).get("history") or [])]


def main():
    g = load_grid()
    lo, hi = min(g), max(g)

    def mean(a, b):
        v = [g[s] for s in range(a, b) if s in g]
        return (sum(v) / len(v), len(v) / (b - a)) if v else (None, 0.0)

    def spot(t):
        for kk in (t, t - 1, t - 2, t - 3):
            if kk in g:
                return g[kk]

    start = lo - lo % WINDOW + WINDOW * (VOL_BARS + 1)
    wtss = [w for w in range(start, hi - WINDOW, WINDOW)]
    if not wtss:
        raise SystemExit(f"grid too short — needs {VOL_BARS + 1} windows of "
                         f"history before the first scored one")
    with ThreadPoolExecutor(12) as ex:
        got = list(ex.map(pull, wtss))

    rows = []
    for w, win, pts in got:
        if not win:
            continue
        k, ck = mean(w - NSEC, w)
        p = spot(w)
        if k is None or p is None or ck < 0.9:
            continue
        bars = [spot(w - WINDOW * i) for i in range(VOL_BARS + 1)]
        if any(b is None for b in bars):
            continue
        sig = st.pstdev([math.log(bars[i] / bars[i + 1])
                         for i in range(VOL_BARS)])
        if sig <= 0:
            continue
        fresh = [q for q in pts if w < q[0] <= w + MAXAGE]
        if not fresh:
            continue
        _, mid = min(fresh)
        tilt = (p - k) / k
        pick = "up" if tilt >= 0 else "down"
        px = (mid if pick == "up" else 1 - mid) + SPREAD
        rows.append(dict(z=abs(tilt / sig), bp=abs(tilt) * 1e4,
                         won=pick == win, px=px, sig=sig * 1e4,
                         tilt=tilt * 1e4, mid=mid, up=1 if win == "up" else 0))

    n = len(rows)
    days = (hi - lo) / 86400.0
    print(f"{COIN}: {n} windows with tilt + vol + quote + outcome over "
          f"{days:.2f}d | ask = mid + {SPREAD:.3f}")
    if n < 60:
        print("too few joined windows — let the grid grow and re-run")
        return
    print(f"median {WINDOW//60}m-bar vol "
          f"{sorted(r['sig'] for r in rows)[n//2]:.1f}bp\n")

    for name, key in (("RANKED BY RAW |tilt|", "bp"),
                      ("RANKED BY |tilt| / vol   <- AHL's rule", "z")):
        print(name)
        print(f"{'slice':>7} {'n':>5} {'win%':>6} {'95% CI':>13} {'avg px':>7} "
              f"{'need%':>7} {'EV c/sh':>8} {'EV lo':>8} {'$/day':>8}")
        for frac in SLICES:
            m = max(12, int(n * frac))
            sel = sorted(rows, key=lambda r: -r[key])[:m]
            k = sum(r["won"] for r in sel)
            clo, chi = wilson(k, m)
            px = sum(r["px"] for r in sel) / m
            ev = k / m - px - fee(px)
            print(f"{100*frac:>6.0f}% {m:>5} {100*k/m:>5.1f}% "
                  f"[{100*clo:>4.1f},{100*chi:>4.1f}]% {px:>7.3f} "
                  f"{100*(px+fee(px)):>6.1f}% {100*ev:>7.2f}c "
                  f"{100*(clo-px-fee(px)):>7.2f}c {ev*CLIP*m/days:>8.0f}")
        print()

    # the mechanism: does the book move enough per bp when the market is calm?
    med = sorted(r["sig"] for r in rows)[n // 2]
    print("THE MECHANISM — price the book moves per bp vs what a bp is worth")
    print(f"{'regime':>8} {'n':>5} {'book slope':>12} {'true slope':>12} "
          f"{'book pays':>10}")
    for lab, part in (("calm", [r for r in rows if r["sig"] < med]),
                      ("wild", [r for r in rows if r["sig"] >= med])):
        if len(part) < 20:
            continue
        x = [r["tilt"] for r in part]
        mx, vx = st.mean(x), st.pvariance(x)

        def slope(y, mx=mx, vx=vx, x=x):
            my = st.mean(y)
            return (sum((a - mx) * (b - my) for a, b in zip(x, y))
                    / len(x) / vx) if vx else 0.0
        bs = slope([r["mid"] - 0.5 for r in part])
        ts = slope([float(r["up"]) for r in part])
        print(f"{lab:>8} {len(part):>5} {bs:>+12.4f} {ts:>+12.4f} "
              f"{100*bs/ts if ts else 0:>9.0f}%")
    print("\nIf 'book pays' is well under 100% in the calm row and the")
    print("vol-scaled table beats the raw one, the book is anchoring on")
    print("basis points and under-adjusting for regime — which is a")
    print("structural error, not a lucky slice.")
    print("EV lo is still the column that decides. n is small at 5%.")


if __name__ == "__main__":
    main()
