"""Read the simultaneous two-venue book samples. Is there a real gap?

  venv/bin/python -m bot.pair_report
  MAX_DT=1.5 venv/bin/python -m bot.pair_report

WHAT THIS ANSWERS THAT HISTORY COULD NOT. Priced off 1-minute candles the
pair looked like +1.54c/pair (t=1.90), and +0.73c (t=0.55) once the worst
quote inside each minute was used — with 76% of the "opportunities"
vanishing under that honest treatment. Inside a single minute Kalshi's ask
moved 0.52 -> 0.31, so quotes up to 60s apart cannot distinguish a real
cross-venue gap from two prices sampled at different moments.

bot/pair_record.py samples both books back to back and stores the
round-trip time of each sample, so here the simultaneity is a measured
quantity rather than an assumption. MAX_DT drops any sample whose two legs
were further apart than that.

THE TRADE. Both venues ask the same question — is the 60s average at the
close at least the 60s average at the open — on the same clock, settled on
Chainlink (Polymarket) and CF Benchmarks BRTI (Kalshi).
    package A = buy UP on Polymarket + buy NO on Kalshi
    package B = buy DOWN on Polymarket + buy YES on Kalshi
Either pays exactly 1.00 whenever the two feeds agree, which they do 96.9%
of the time, and the disagreements are symmetric so they cancel in
expectation. So the edge is simply 1.00 minus what the package costs.

TWO THINGS TO READ, IN ORDER.
  1. How often is a package priced under 1.00 with both fees included, and
     by how much? That needs no outcomes and is available immediately.
  2. Realised profit, averaged WITHIN each window before averaging across
     windows — 40 samples of one window are not 40 independent bets, and
     treating them as such is how the candle version got to t=1.90.
Watch the payoff mix too: the history run produced 64 zeros against 7 twos
while outcome disagreements were 6 and 3, meaning the cheapest packages
cluster in the windows that break. A live rule has to REFUSE the cheapest
packages, not hunt them.

Read-only.
"""
import json
import math
import os
import sqlite3
import statistics as st
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

PAIR_DIR = os.environ.get("PAIR_DIR", "bot/data/paircal")
MAX_DT = float(os.environ.get("MAX_DT", "2.0"))     # seconds between legs
CLIP = float(os.environ.get("CLIP", "250"))
PM_SLUG = os.environ.get("PM_SLUG", "btc-updown-15m-")
K_SERIES = os.environ.get("K_SERIES", "KXBTC15M")
WINDOW = 900
HDRS = {"User-Agent": "Mozilla/5.0"}
KB = "https://api.elections.kalshi.com/trade-api/v2"


def fee(p):
    return 0.07 * p * (1 - p)


def get(url, tries=4):
    for attempt in range(tries):
        try:
            return json.load(urllib.request.urlopen(
                urllib.request.Request(url, headers=HDRS), timeout=30))
        except Exception:  # noqa: BLE001
            if attempt == tries - 1:
                return None
            time.sleep(0.5 * (2 ** attempt))


def pm_outcome(wts):
    a = get(f"https://gamma-api.polymarket.com/markets?slug={PM_SLUG}{wts}&closed=true")
    if not a:
        return wts, None
    m = a[0]
    outs, pr = m["outcomes"], m["outcomePrices"]
    if isinstance(outs, str):
        outs = json.loads(outs)
    if isinstance(pr, str):
        pr = json.loads(pr)
    hit = [o.lower() for o, p in zip(outs, pr) if float(p) == 1.0]
    return wts, (hit[0] if len(hit) == 1 else None)


def kalshi_outcomes():
    """{close_ts: 'yes'|'no'} for settled markets in the series."""
    out, cur = {}, ""
    for _ in range(8):
        r = get(f"{KB}/markets?series_ticker={K_SERIES}&status=settled&limit=200"
                + (f"&cursor={cur}" if cur else ""))
        ms = (r or {}).get("markets") or []
        for m in ms:
            if m.get("result") in ("yes", "no") and m.get("close_time"):
                try:
                    t = time.mktime(time.strptime(
                        m["close_time"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
                except Exception:  # noqa: BLE001
                    continue
                out[int(t)] = m["result"]
        cur = (r or {}).get("cursor") or ""
        if not ms or not cur:
            break
    return out


def main():
    path = os.path.join(PAIR_DIR, "pair_15m.db")
    if not os.path.exists(path):
        raise SystemExit(f"no pair recording at {path} — start polybot-pairrec")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    rows = list(db.execute(
        "SELECT wts, ts, left_s, pm_up_ask, pm_up_sz, pm_dn_ask, pm_dn_sz, "
        "k_yes_ask, k_yes_sz, k_no_ask, k_no_sz, pm_err, k_err, dt FROM pair"))
    if not rows:
        raise SystemExit("recording is empty")
    wins = sorted({r[0] for r in rows})
    span = (max(r[1] for r in rows) - min(r[1] for r in rows)) / 3600.0
    dts = sorted(r[13] for r in rows if r[13] is not None)
    good = [r for r in rows if not r[11] and not r[12]
            and None not in (r[3], r[5], r[7], r[9]) and (r[13] or 9) <= MAX_DT]
    print(f"{len(rows)} samples over {len(wins)} windows, {span:.1f}h")
    print(f"  fetch failures: polymarket {sum(r[11] for r in rows)}, "
          f"kalshi {sum(r[12] for r in rows)}")
    if dts:
        print(f"  leg separation: median {dts[len(dts)//2]:.2f}s, "
              f"p90 {dts[9*len(dts)//10]:.2f}s  (candle data was up to 60s)")
    print(f"  usable at MAX_DT={MAX_DT}s: {len(good)}\n")
    if not good:
        print("nothing usable yet — let it record longer")
        return

    # ---------------------------------------------- 1. the gap, no outcomes
    best = []
    for r in good:
        (wts, ts, left, pu, pus, pd, pds, ky, kys, kn, kns, _, _, _) = r
        A = pu + kn + fee(pu) + fee(kn)
        B = pd + ky + fee(pd) + fee(ky)
        if A <= B:
            best.append((wts, left, "A", A, min(pus or 0, kns or 0)))
        else:
            best.append((wts, left, "B", B, min(pds or 0, kys or 0)))
    costs = sorted(b[3] for b in best)
    under = [b for b in best if b[3] < 1.0]
    print("1. THE GAP — cheapest package per sample, both fees included")
    print(f"   median cost {costs[len(costs)//2]:.4f} | "
          f"p10 {costs[len(costs)//10]:.4f} | min {costs[0]:.4f}")
    print(f"   priced under 1.00: {len(under)} of {len(good)} samples "
          f"({100*len(under)/len(good):.1f}%), in "
          f"{len({b[0] for b in under})} of {len(wins)} windows")
    if under:
        avg_gap = sum(1.0 - b[3] for b in under) / len(under)
        sz = sorted(b[4] for b in under)
        print(f"   mean gap when present: {100*avg_gap:.2f}c/pair | "
              f"median size at the touch {sz[len(sz)//2]:.0f} "
              f"(the binding leg)")

    # ------------------------------------------- 2. realised, with outcomes
    kal = kalshi_outcomes()
    need = [w for w in wins if w + WINDOW < time.time() - 300]
    with ThreadPoolExecutor(12) as ex:
        pm = dict(ex.map(pm_outcome, need))
    perwin, pays = {}, {}
    for wts, left, tag, cost, sz in best:
        if cost >= 1.0:
            continue
        p, k = pm.get(wts), kal.get(wts + WINDOW)
        if not p or not k:
            continue
        if tag == "A":
            pay = (1 if p == "up" else 0) + (1 if k == "no" else 0)
        else:
            pay = (1 if p == "down" else 0) + (1 if k == "yes" else 0)
        perwin.setdefault(wts, []).append(pay - cost)
        pays[pay] = pays.get(pay, 0) + 1
    print(f"\n2. REALISED — {sum(len(v) for v in perwin.values())} priced "
          f"opportunities across {len(perwin)} settled windows")
    if len(perwin) < 8:
        print("   too few settled windows yet; the gap table above is the")
        print("   part that is already meaningful. Re-run in a few hours.")
        return
    wp = [sum(v) / len(v) for v in perwin.values()]
    m, sd = st.mean(wp), st.pstdev(wp)
    se = sd / math.sqrt(len(wp))
    print(f"   payoff mix: {dict(sorted(pays.items()))}   "
          f"(1 = feeds agreed, 0 and 2 = they split)")
    print(f"   profit per pair, averaged within window then across windows:")
    print(f"     mean {100*m:+.2f}c  sd {100*sd:.1f}c  se {100*se:.2f}c  "
          f"t = {m/se if se else 0:+.2f}  (n={len(wp)} windows)")
    lo, hi = m - 1.96 * se, m + 1.96 * se
    print(f"     95% CI [{100*lo:+.2f}c, {100*hi:+.2f}c] per pair")
    if lo > 0:
        # ONE position per window, not one per sample. Forty samples of the
        # same window are the same trade re-observed; counting them as forty
        # bets inflates both the size and the significance.
        wph = len(perwin) / max(span, 1e-9)
        print(f"\n   POSITIVE AT THE LOWER BOUND. Sized at one package per "
              f"window — {wph:.1f} tradeable windows/h at {CLIP:.0f} contracts "
              f"— that is ${lo*CLIP*wph*24:.0f}/day at the floor.")
        print("   Size is still capped by the binding leg above, and this "
              "needs a\n   second, disjoint day before it means anything.")
    else:
        print("\n   NOT significant. Same verdict as the candle data, now"
              " without the timing excuse.")
    zeros = pays.get(0, 0)
    twos = pays.get(2, 0)
    if zeros + twos:
        print(f"\n   ADVERSE SELECTION CHECK: {zeros} zeros vs {twos} twos. "
              f"Feed disagreements\n   run 3.1% and symmetric, so a big "
              f"imbalance here means the cheap\n   packages are landing in "
              f"the windows that break — refuse them, don't chase.")


if __name__ == "__main__":
    main()
