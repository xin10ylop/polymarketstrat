"""Can Binance stand in for the oracle grid? Calibrate it, or stop using it.

  venv/bin/python -m bot.proxy_calib
  COIN=eth venv/bin/python -m bot.proxy_calib

WHY THIS EXISTS. Twice now a result measured on Binance 1s trades has died
on the real Chainlink grid, and both times for the same reason: Binance
last-trade prints carry bid-ask bounce that the oracle grid does not.
    btc   Binance 1s vol 0.384 bp/s   Chainlink grid 0.108 bp/s
    btc   Binance 5m-bar vol 7.0bp    Chainlink grid 2.8bp
The opening tilt is ~1bp. Against that, Binance is mostly noise. The
vol-scaled ranking was worse still — it divided a noisy signal by a noisy
scaler and ranked on the ratio.

This matters beyond one dead result. The oracle grid only goes back as far
as bot/twap_record.py has been running, so every question needing weeks of
history has to use Binance or wait weeks. If a SMOOTHED Binance tracks the
grid closely enough, the archive becomes usable and the waiting stops.

WHAT IT DOES. Over the span the grid already covers, fetches Binance 1s
closes, smooths them with a trailing mean of m seconds for a range of m,
and asks the only question that matters: does the SMOOTHED proxy's opening
tilt agree with the grid's? Reports correlation, the regression slope
(attenuation: 1.0 means unbiased, 0.5 means half the signal is noise), and
how often the two disagree about which side is tilted — because that sign
is what a strategy would trade.

READ THE SIGN-DISAGREEMENT COLUMN, not the correlation. A proxy that
correlates 0.9 but picks the wrong side 1 window in 6 is useless for a
trade whose whole edge is 10-20 points of win rate.

Read-only. Never trades.
"""
import json
import math
import os
import statistics as st
import time
import urllib.request

from bot.twap_verify import COIN, HDRS, NSEC, WINDOW, load_grid

SMOOTH = [int(x) for x in os.environ.get("SMOOTH", "1,2,3,5,10,15,30").split(",")]
SYM = os.environ.get("SYM", {"btc": "BTCUSDT", "eth": "ETHUSDT",
                             "sol": "SOLUSDT", "doge": "DOGEUSDT"}.get(COIN))
HOST = os.environ.get("BINANCE_HOST", "data-api.binance.vision")


def fetch(a, b):
    """1s closes from Binance over [a, b). {} if unreachable."""
    out = {}
    for s in range(a, b, 1000):
        e = min(s + 1000, b)
        url = (f"https://{HOST}/api/v3/klines?symbol={SYM}&interval=1s"
               f"&startTime={s*1000}&endTime={e*1000}&limit=1000")
        for attempt in range(4):
            try:
                r = json.load(urllib.request.urlopen(
                    urllib.request.Request(url, headers=HDRS), timeout=30))
                for k in r:
                    out[k[0] // 1000] = float(k[4])
                break
            except Exception:  # noqa: BLE001
                if attempt == 3:
                    return out
                time.sleep(0.6 * (2 ** attempt))
    return out


def main():
    g = load_grid()
    lo, hi = min(g), max(g)
    print(f"grid: {COIN} {(hi-lo)/3600:.1f}h; fetching {SYM} 1s from {HOST} "
          f"for the same span (this takes a minute)", flush=True)
    b = fetch(lo, hi + 1)
    if not b:
        raise SystemExit(f"could not reach {HOST} — nothing to calibrate")
    both = sorted(set(g) & set(b))
    print(f"overlap: {len(both)} seconds\n")
    if len(both) < 5000:
        raise SystemExit("overlap too small")

    def vol(series):
        xs = sorted(series)
        d = [math.log(series[y] / series[x])
             for x, y in zip(xs, xs[1:]) if y - x == 1]
        return (sum(v * v for v in d) / len(d)) ** 0.5 * 1e4 if d else 0.0

    print(f"1s vol: grid {vol(g):.3f} bp/s | binance raw {vol(b):.3f} bp/s "
          f"({vol(b)/max(vol(g),1e-9):.1f}x)\n")

    def tilt_of(series, smooth):
        """{wts: tilt in bp} using a trailing `smooth`-second spot."""
        out = {}
        for w in range(lo - lo % WINDOW + WINDOW, hi - WINDOW, WINDOW):
            kv = [series[s] for s in range(w - NSEC, w) if s in series]
            sv = [series[s] for s in range(w - smooth + 1, w + 1) if s in series]
            if len(kv) < NSEC * 0.9 or not sv:
                continue
            k = sum(kv) / len(kv)
            out[w] = ((sum(sv) / len(sv)) - k) / k * 1e4
        return out

    ref = tilt_of(g, 1)
    print("does a SMOOTHED binance tilt agree with the grid's tilt?")
    print(f"{'smooth':>7} {'n':>6} {'corr':>7} {'slope':>7} {'sd ratio':>9} "
          f"{'sign disagrees':>15} {'on |tilt|>1bp':>14}")
    best = None
    for m in SMOOTH:
        p = tilt_of(b, m)
        keys = sorted(set(ref) & set(p))
        if len(keys) < 200:
            continue
        x = [p[k] for k in keys]
        y = [ref[k] for k in keys]
        mx, my = st.mean(x), st.mean(y)
        cov = sum((a - mx) * (c - my) for a, c in zip(x, y)) / len(x)
        sx, sy = st.pstdev(x), st.pstdev(y)
        corr = cov / (sx * sy) if sx * sy else 0.0
        slope = cov / st.pvariance(x) if st.pvariance(x) else 0.0
        bad = sum(1 for a, c in zip(x, y) if (a >= 0) != (c >= 0)) / len(x)
        strong = [(a, c) for a, c in zip(x, y) if abs(c) >= 1.0]
        badS = (sum(1 for a, c in strong if (a >= 0) != (c >= 0)) / len(strong)
                if strong else float("nan"))
        print(f"{m:>6}s {len(keys):>6} {corr:>7.3f} {slope:>7.3f} "
              f"{sx/sy if sy else 0:>9.2f} {100*bad:>14.1f}% {100*badS:>13.1f}%")
        if best is None or badS < best[1]:
            best = (m, badS, corr)

    print(f"\n'slope' is attenuation: 1.0 = unbiased, 0.5 = half the measured")
    print("tilt is noise. 'sd ratio' > 1 means the proxy invents movement the")
    print("oracle never had. The last column is the one that decides: on the")
    print("windows the strategy would actually trade, how often does the")
    print("proxy point the WRONG WAY?")
    if best:
        m, badS, corr = best
        print(f"\nbest: {m}s smoothing, wrong side on {100*badS:.1f}% of "
              f"|tilt|>1bp windows, corr {corr:.3f}")
        if badS <= 0.05:
            print("  USABLE for research on the Binance archive at that")
            print("  smoothing — but re-confirm every result on the grid.")
        else:
            print("  NOT USABLE. A proxy that picks the wrong side this often")
            print("  cannot test a signal whose edge is 10-20 points. Research")
            print("  on this market waits for the grid; there is no shortcut.")


if __name__ == "__main__":
    main()
