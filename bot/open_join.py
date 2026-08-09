"""Did the book already price the opening tilt? Tilt x quote x outcome.

  venv/bin/python -m bot.open_join
  COIN=eth venv/bin/python -m bot.open_join
  DAYS=3 venv/bin/python -m bot.open_join

bot/open_offset.py asks whether the tilt PREDICTS. This asks the only
question that follows: whether anyone will still sell it to you cheap.

Three sources per window: the 1s Chainlink grid for the tilt at T+0, the
CLOB price-history endpoint for what the book was quoting just after the
open, and gamma for who won. No book recorder needed — price-history is
public and retrospective, which is why this can answer in minutes what the
recorders would take a week to answer.

TWO NUMBERS CARRY THE VERDICT.
  1. slope of (quote - 0.50) on tilt. Zero means the book is ignoring the
     tilt and the trade is live. It was +0.0024/bp BEFORE the rule change,
     when the tilt was genuinely worthless, which is what a blind book
     looks like.
  2. EV after the taker fee, with its 95% lower bound. At a quote near 0.50
     the fee is at its arithmetic MAXIMUM — 0.07*p*(1-p) is 1.75c/share at
     0.50 against 0.14c at 0.98 — so an edge under ~2 points of win rate
     does not survive contact with it.

Read-only. Never trades, touches no bot state.
"""
import json
import math
import os
import statistics as st
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from bot.twap_verify import COIN, HDRS, NSEC, WINDOW, load_grid

DAYS = float(os.environ.get("DAYS", "0"))          # 0 = whatever the grid holds
MAXAGE = int(os.environ.get("MAXAGE", "45"))       # quote must be this fresh
SPREAD = float(os.environ.get("SPREAD", "0.005"))  # mid -> ask penalty
CLIP = float(os.environ.get("CLIP", "250"))
EDGES = [0.0, 0.5, 1.0, 2.0, 3.0, 1e9]
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
    """(wts, winner, [(t, mid)]) — outcome plus quotes around the open."""
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
    if DAYS:
        lo = max(lo, int(time.time()) - int(DAYS * 86400))
    wtss = [w for w in range(lo - lo % WINDOW + WINDOW, hi - WINDOW, WINDOW)]
    if not wtss:
        raise SystemExit("grid too short")

    def mean(a, b):
        v = [g[s] for s in range(a, b) if s in g]
        return (sum(v) / len(v), len(v) / (b - a)) if v else (None, 0.0)

    def spot(t):
        for kk in (t, t - 1, t - 2):
            if kk in g:
                return g[kk]

    with ThreadPoolExecutor(12) as ex:
        got = list(ex.map(pull, wtss))

    rows = []
    for w, win, pts in got:
        if not win:
            continue
        k, ck = mean(w - NSEC, w)
        s = spot(w)
        if k is None or s is None or ck < 0.9:
            continue
        fresh = [p for p in pts if w < p[0] <= w + MAXAGE]
        if not fresh:
            continue
        t, mid = min(fresh)
        tilt = (s - k) / k * 1e4
        pick = "up" if tilt >= 0 else "down"
        px = (mid if pick == "up" else 1 - mid) + SPREAD
        rows.append((abs(tilt), pick == win, px, t - w, mid, tilt))

    span_d = (hi - lo) / 86400.0
    print(f"{COIN} {SLUG.split('-')[2]}: {len(rows)} windows with tilt + quote "
          f"+ outcome over {span_d:.2f}d | quote within {MAXAGE}s of the open "
          f"| ask = mid + {SPREAD:.3f}\n")
    if len(rows) < 30:
        print("too few joined windows to say anything")
        return

    print(f"{'|tilt| bp':>11} {'n':>5} {'win%':>6} {'95% CI':>13} {'avg px':>7} "
          f"{'need%':>7} {'EV c/sh':>8} {'EV lo':>8} {'$/day':>9}")
    for a, b in zip(EDGES, EDGES[1:]):
        sel = [r for r in rows if a <= r[0] < b]
        if len(sel) < 15:
            continue
        n = len(sel)
        k = sum(r[1] for r in sel)
        clo, chi = wilson(k, n)
        px = sum(r[2] for r in sel) / n
        ev = k / n - px - fee(px)
        lab = f"{a:.1f}-{b:.1f}" if b < 1e8 else f"{a:.1f}+"
        print(f"{lab:>11} {n:>5} {100*k/n:>5.1f}% [{100*clo:>4.1f},{100*chi:>4.1f}]% "
              f"{px:>7.3f} {100*(px+fee(px)):>6.1f}% {100*ev:>7.2f}c "
              f"{100*(clo-px-fee(px)):>7.2f}c {ev*CLIP*n/span_d:>9.0f}")
    n = len(rows)
    k = sum(r[1] for r in rows)
    clo, chi = wilson(k, n)
    px = sum(r[2] for r in rows) / n
    ev = k / n - px - fee(px)
    print(f"{'ALL':>11} {n:>5} {100*k/n:>5.1f}% [{100*clo:>4.1f},{100*chi:>4.1f}]% "
          f"{px:>7.3f} {100*(px+fee(px)):>6.1f}% {100*ev:>7.2f}c "
          f"{100*(clo-px-fee(px)):>7.2f}c {ev*CLIP*n/span_d:>9.0f}")

    xs = [r[5] for r in rows]
    ys = [r[4] - 0.5 for r in rows]
    mx, my = st.mean(xs), st.mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / len(xs)
    sx, sy = st.pstdev(xs), st.pstdev(ys)
    slope = cov / st.pvariance(xs) if st.pvariance(xs) else 0.0
    xs2 = sorted(g)
    dd = [math.log(g[b] / g[a]) for a, b in zip(xs2, xs2[1:]) if b - a == 1]
    sig = (sum(x * x for x in dd) / len(dd)) ** 0.5 if dd else 0.0
    full = (0.5 * (1 + math.erf((1e-4 / (sig * math.sqrt(WINDOW)))
                                / math.sqrt(2))) - 0.5) if sig else 0.0
    ages = sorted(r[3] for r in rows)
    print(f"\nIS THE TILT PRICED?  corr = {cov/(sx*sy) if sx*sy else 0:+.3f}, "
          f"slope = {slope:+.4f} of price per bp")
    print(f"  a random walk would call a bp worth {full:+.4f}; the book is "
          f"paying {100*slope/full if full else 0:.0f}% of that.")
    print("  A gap here is NOT an edge by itself — the model is only a")
    print("  yardstick. Read it against the realised win rates above: the")
    print("  tilt is worth what it actually won, not what the model says.")
    print("  (An earlier run of this on Binance 1s trades concluded the tilt")
    print("  mean-reverts and the book was fully paying. That was wrong —")
    print("  Binance last-trade noise is ~3.6x the Chainlink grid's 1s vol,")
    print("  so most of the measured tilt was bid-ask bounce, which reverts")
    print("  by construction. On this feed the reversion is much weaker.)")
    print(f"quote age: median {ages[len(ages)//2]}s, "
          f"p90 {ages[9*len(ages)//10]}s")
    print("\nfee note: 0.07*p*(1-p) peaks at 1.75c/share at p=0.50 and is")
    print("0.14c at 0.98. Trading near the open means paying the worst fee")
    print("on the board, so the edge has to clear ~2 points of win rate.")


if __name__ == "__main__":
    main()
