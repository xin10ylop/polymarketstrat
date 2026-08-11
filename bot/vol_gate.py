"""Fixed gate vs volatility-scaled gate, judged at the REAL pre-open entry.

  venv/bin/python -m bot.vol_gate
  COIN=eth venv/bin/python -m bot.vol_gate
  COIN=btc FAMILY=15m venv/bin/python -m bot.vol_gate

THE QUESTION. The gate is a fixed number of basis points. But a basis point
of tilt is not worth the same in every market: bot/vol_tilt.py measures the
true value of a bp at +0.2646 in calm btc windows against +0.0831 in wild ones
— THREE TIMES more — while the book moves its quote only +0.1189 and +0.0491,
paying 45% and 59% of value. If that holds, a fixed gate is too strict when
the market is quiet and too loose when it is not, and ranking by |tilt|/vol
should beat ranking by |tilt|.

WHY THIS TOOL EXISTS RATHER THAN vol_tilt's ANSWER. vol_tilt prices at the
FIRST QUOTE AFTER THE OPEN — its avg px is 0.61-0.68, because by then the
book has already repriced. Our bot buys BEFORE the open at ~0.52. Break-even
at 0.65 is 66%; at 0.52 it is 54%. So vol_tilt's EV, need% and $/day columns
describe a different trade entirely, and splicing its win rates onto our entry
price would be the exact error that has cost this project four retractions.
Everything here is priced from the same pre-open tape scalp_backtest uses.

HOW THE COMPARISON IS KEPT FAIR. Two rankings cannot be compared at their own
preferred thresholds — a stricter one always looks better per trade. So both
are held to the SAME TRADE COUNT: take the top N windows by |tilt|, take the
top N by |tilt|/vol, and score both on the same days with the same entries.
Any difference is then the ranking, not the selectivity.

WHAT WOULD MAKE IT REAL. The scaled column beating the fixed column at
matched N, in both halves of the clock, with a lower bound that clears. Two
of three is a hint. One of three is noise, and this project has already
retracted a vol-scaling result once — on 8-of-8 slices that turned out to be
proxy noise divided by itself.

Read-only. Never trades.
"""
import math
import os
import statistics as st

from bot.scalp_backtest import (band, fee, load_tape, tilt_at, wilson)
from bot.twap_verify import COIN, FAMILY, NSEC, WINDOW, load_grid

BASE = float(os.environ.get("BASE", "0.3"))     # pool both rankings draw from
VOL_BARS = int(os.environ.get("VOL_BARS", "42"))
ENTRY_WIN = int(os.environ.get("ENTRY_WIN", "60"))
CLIP = float(os.environ.get("CLIP", "250"))
RULE = 1786060800                                # 2026-08-07 rule change


def local_vol(g, w):
    """Realised vol per window, from the price at the last VOL_BARS window
    boundaries. Same construction as vol_tilt so the two are comparable."""
    px = []
    for i in range(VOL_BARS + 1):
        t = w - WINDOW * i
        v = next((g[s] for s in (t, t - 1, t - 2) if s in g), None)
        if v is None or v <= 0:
            return None
        px.append(v)
    r = [math.log(px[i] / px[i + 1]) for i in range(VOL_BARS)]
    s = st.pstdev(r)
    return s if s > 0 else None


def main():
    g = load_grid()
    lo, hi = min(g), max(g)
    lo = max(lo, RULE)
    first = ((int(lo) + NSEC) // WINDOW + 1) * WINDOW + WINDOW * (VOL_BARS + 1)
    cand = {}
    for w in range(first, int(hi) - WINDOW, WINDOW):
        t = tilt_at(g, w)
        if not t or abs(t[0]) < BASE:
            continue
        v = local_vol(g, w)
        if v is None:
            continue
        cand[w] = (t[0], t[1], v)
    if len(cand) < 40:
        raise SystemExit(f"only {len(cand)} windows above the {BASE}bp pool "
                         "gate — grid too short to compare rankings")
    print(f"{COIN} {FAMILY}: {len(cand)} windows in the pool (|tilt| >= "
          f"{BASE}bp, post-rule-change), vol over {VOL_BARS} prior windows\n")

    tape = load_tape(sorted(cand))
    rows = []
    for w, (tilt, pick, vol) in cand.items():
        if w not in tape:
            continue
        winner, prints = tape[w]
        pr = [(t, s, p if pick == "up" else 1.0 - p)
              for (t, s, p, _z) in prints]
        if pick == "down":
            pr = [(t, "SELL" if s == "BUY" else "BUY", p) for (t, s, p) in pr]
        pre = [p for (t, s, p) in pr if -ENTRY_WIN <= t < 0 and s == "BUY"]
        if not pre:
            continue
        entry = st.median(pre)
        if not (0.02 < entry < 0.98):
            continue
        rows.append(dict(w=w, bp=abs(tilt), z=abs(tilt) / vol, vol=vol,
                         entry=entry, won=(winner == pick)))
    if len(rows) < 40:
        raise SystemExit(f"only {len(rows)} joined tape+grid")
    rows.sort(key=lambda r: r["w"])
    span_h = (rows[-1]["w"] - rows[0]["w"]) / 3600.0
    print(f"{len(rows)} windows with tape + grid + outcome over {span_h:.1f}h\n")

    def pnl(r):
        return 100.0 * ((1.0 if r["won"] else 0.0) - r["entry"] - fee(r["entry"]))

    def score(sel):
        p = [pnl(r) for r in sel]
        m, lo_, hi_, ne = band(p)
        k = sum(r["won"] for r in sel)
        e = sum(r["entry"] for r in sel) / len(sel)
        per_day = (m / 100.0) * CLIP * len(sel) * 24.0 / span_h if span_h else 0
        return (k / len(sel), e, m, lo_, per_day)

    # --------------------------------------------- matched-N head to head
    print("HEAD TO HEAD AT THE SAME TRADE COUNT")
    print("  (top N by each ranking, same windows available to both)")
    print(f"{'N':>5} {'ranking':>10} {'settles':>9} {'entry':>7} {'EV c/sh':>9} "
          f"{'95% lo':>9} {'$/day':>8} {'overlap':>9}")
    for frac in (0.15, 0.25, 0.40, 0.60, 1.0):
        n = max(12, int(len(rows) * frac))
        if n > len(rows):
            continue
        by_bp = sorted(rows, key=lambda r: -r["bp"])[:n]
        by_z = sorted(rows, key=lambda r: -r["z"])[:n]
        ov = len({r["w"] for r in by_bp} & {r["w"] for r in by_z})
        for lbl, sel in (("fixed bp", by_bp), ("tilt/vol", by_z)):
            wr, e, m, lo_, pd = score(sel)
            print(f"{n:>5} {lbl:>10} {100*wr:>8.1f}% {e:>7.4f} {m:>+8.2f}c "
                  f"{lo_:>+8.2f}c {pd:>8.0f}"
                  + (f" {ov:>8}" if lbl == "tilt/vol" else ""))
        print()

    # ------------------------------------------------ where it comes from
    print("WHERE ANY DIFFERENCE COMES FROM — same bp gate, split by regime")
    print(f"{'regime':>8} {'n':>5} {'med vol':>9} {'med bp':>8} {'settles':>9} "
          f"{'entry':>7} {'EV c/sh':>9} {'95% lo':>9}")
    byv = sorted(rows, key=lambda r: r["vol"])
    third = len(byv) // 3
    for lbl, sel in (("calm", byv[:third]), ("middle", byv[third:2 * third]),
                     ("wild", byv[2 * third:])):
        if len(sel) < 8:
            continue
        wr, e, m, lo_, _ = score(sel)
        print(f"{lbl:>8} {len(sel):>5} {1e4*st.median(r['vol'] for r in sel):>8.2f}b "
              f"{st.median(r['bp'] for r in sel):>8.2f} {100*wr:>8.1f}% "
              f"{e:>7.4f} {m:>+8.2f}c {lo_:>+8.2f}c")

    # ------------------------------------------------------ out of sample
    half = len(rows) // 2
    a, b = rows[:half], rows[half:]
    print(f"\nOUT OF SAMPLE — first {len(a)} windows vs last {len(b)}")
    print(f"{'ranking':>10} {'N':>5} {'first half':>12} {'second half':>13} "
          f"{'verdict':>10}")
    for frac in (0.25, 0.40):
        na, nb = max(8, int(len(a) * frac)), max(8, int(len(b) * frac))
        out = {}
        for lbl, key in (("fixed bp", "bp"), ("tilt/vol", "z")):
            sa = sorted(a, key=lambda r: -r[key])[:na]
            sb = sorted(b, key=lambda r: -r[key])[:nb]
            out[lbl] = (score(sa)[2], score(sb)[2])
        for lbl in ("fixed bp", "tilt/vol"):
            fa, fb = out[lbl]
            v = ""
            if lbl == "tilt/vol":
                v = "HELD" if fb > out["fixed bp"][1] else "lost"
            print(f"{lbl:>10} {na:>5} {fa:>+11.2f}c {fb:>+12.2f}c {v:>10}")
        print()

    print("READ THE OUT-OF-SAMPLE BLOCK FIRST. Ranking by tilt/vol has been")
    print("retracted on this project once already — it won 8 of 8 slices on")
    print("Binance data and 3 of 8 on the real grid, because both terms of the")
    print("ratio came from the same noisy proxy. Here both come from the")
    print("Chainlink grid the market actually settles on, but that only removes")
    print("the known defect; it does not make a small sample large.")
    print("A win in ONE half is noise. The claim needs both halves and a")
    print("lower bound that clears zero at a trade count worth having.")


if __name__ == "__main__":
    main()
