"""Score reconstructed-TWAP resolution rules against OFFICIAL market outcomes.

  venv/bin/python -m bot.twap_verify                 # btc, 5m family
  COIN=eth FAMILY=15m venv/bin/python -m bot.twap_verify

Reads the 1s Chainlink grid captured by bot/twap_record.py, rebuilds each
candidate rule for every fully-covered window, fetches the official winner
from gamma, and reports agreement.

Since the recorded grid is the SAME price series the resolver uses, a correct
reconstruction should agree ~100% (any residue should be zero-margin ties).
That is the gate for changing bot/feeds/oracle.py: do NOT migrate the oracle
on a rule that scores below ~99% here, and do not restart the direction bots
until one does.

Candidates (N = 30s for the 5m family, 60s for 15m, per the market
descriptions):
  OLD        spot close vs spot open                (pre-2026-08-07 rule)
  TWAP/TWAP  N-sec mean at close vs N-sec mean at open
  TWAP/spot  N-sec mean at close vs spot at open
each under two boundary conventions: [t-N, t) and (t-N, t].
"""
import json
import os
import sqlite3
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

COIN = os.environ.get("COIN", "btc").lower()
FAMILY = os.environ.get("FAMILY", "5m")
DB_DIR = os.environ.get("TWAP_DIR", "bot/data/twapcal")
WINDOW = 900 if FAMILY == "15m" else 300
# RULE2 (2026-08-14 00:00 UTC): the venue moved the 5m family from the 30s
# to the 60s TWAP stream. The 15m family was 60s all along. NSEC is the
# CURRENT-rule lookback; any tool touching windows that may predate RULE2
# must ask nsec_at(wts) per window instead of using the scalar.
RULE2 = 1786665600
NSEC = int(os.environ.get("NSEC", "60"))


def nsec_at(wts):
    """The strike/settle TWAP lookback in force for this window."""
    return 30 if FAMILY == "5m" and wts < RULE2 else NSEC
SLUG = f"{COIN}-updown-{FAMILY}-"
MIN_COVER = 0.95
HDRS = {"User-Agent": "Mozilla/5.0"}


def load_grid():
    path = os.path.join(DB_DIR, f"{COIN}_1s.db")
    if not os.path.exists(path):
        raise SystemExit(f"no recording at {path} — run bot.twap_record first")
    db = sqlite3.connect(path)
    g = dict(db.execute("SELECT ts, v FROM px"))
    if not g:
        raise SystemExit("recording is empty")
    return g


def official(wts, _tries=4):
    """(wts, 'up'|'down'|None). None means UNRESOLVED OR UNREACHABLE.

    Retries with backoff: gamma rate-limits after a few hundred requests, and
    a silent swallow there reads as "no markets exist", which is how an eth
    run once reported 0 settled windows against a full grid.
    """
    for attempt in range(_tries):
        try:
            req = urllib.request.Request(
                f"https://gamma-api.polymarket.com/markets?slug={SLUG}{wts}&closed=true",
                headers=HDRS)
            a = json.load(urllib.request.urlopen(req, timeout=30))
            break
        except Exception:  # noqa: BLE001
            if attempt == _tries - 1:
                return wts, None
            time.sleep(0.5 * (2 ** attempt))
    try:
        if not a:
            return wts, None
        m = a[0]
        outs = m["outcomes"]
        prices = m["outcomePrices"]
        if isinstance(outs, str):
            outs = json.loads(outs)
        if isinstance(prices, str):
            prices = json.loads(prices)
        hit = [o.lower() for o, p in zip(outs, prices) if float(p) == 1.0]
        return wts, (hit[0] if len(hit) == 1 else None)
    except Exception:  # noqa: BLE001
        return wts, None


def main():
    g = load_grid()
    lo, hi = min(g), max(g)
    print(f"grid: {len(g)} samples, {(hi-lo)/3600:.1f}h "
          f"({time.strftime('%m-%d %H:%M', time.gmtime(lo))} -> "
          f"{time.strftime('%m-%d %H:%M', time.gmtime(hi))} UTC), "
          f"density {100*len(g)/max(1, hi-lo+1):.1f}%")
    print(f"family {FAMILY}: window {WINDOW}s, TWAP {NSEC}s\n")

    def spot(t):
        for k in (t, t - 1, t - 2):     # tolerate a missing second at the edge
            if k in g:
                return g[k]
        return None

    def mean(a, b):                     # mean over [a, b)
        v = [g[s] for s in range(a, b) if s in g]
        return (sum(v) / len(v), len(v) / (b - a)) if v else (None, 0.0)

    wtss = [w for w in range(lo - lo % WINDOW + WINDOW, hi - WINDOW + 1, WINDOW)]
    if not wtss:
        raise SystemExit("no complete window in the recording yet — record longer")
    with ThreadPoolExecutor(8) as ex:
        wins = {k: v for k, v in ex.map(official, wtss) if v}
    print(f"windows in recording: {len(wtss)} | official outcomes fetched: {len(wins)}")

    RULES = {}
    for shift, tag in ((0, "[t-N,t)"), (1, "(t-N,t]")):
        RULES[f"TWAP/TWAP {tag}"] = (
            lambda T, s=shift: (mean(T + WINDOW - NSEC + s, T + WINDOW + s),
                                mean(T - NSEC + s, T + s)))
        RULES[f"TWAP/spot {tag}"] = (
            lambda T, s=shift: (mean(T + WINDOW - NSEC + s, T + WINDOW + s),
                                (spot(T), 1.0)))
    RULES["OLD spot/spot"] = lambda T: ((spot(T + WINDOW), 1.0), (spot(T), 1.0))

    tally = {k: [0, 0, []] for k in RULES}      # agree, tested, disagreements
    for T, w in sorted(wins.items()):
        for name, f in RULES.items():
            (c, cc), (o, oc) = f(T)
            if c is None or o is None or cc < MIN_COVER or oc < MIN_COVER:
                continue
            pred = "up" if c >= o else "down"
            tally[name][1] += 1
            if pred == w:
                tally[name][0] += 1
            else:
                tally[name][2].append((T, w, pred, (c - o) / o * 1e4))

    print(f"\n{'rule':26s} {'tested':>7s} {'agree':>7s} {'agree%':>8s}")
    ranked = sorted(tally.items(), key=lambda kv: -(kv[1][0] / kv[1][1]) if kv[1][1] else 0)
    for name, (ag, n, _) in ranked:
        if n:
            print(f"{name:26s} {n:7d} {ag:7d} {100*ag/n:7.2f}%")
        else:
            print(f"{name:26s} {'-':>7s} {'-':>7s}   (no covered windows)")

    best, (bag, bn, bad) = ranked[0]
    if bn:
        print(f"\nBEST: {best} at {100*bag/bn:.2f}% over {bn} windows")
        if bad:
            marg = sorted(abs(m) for *_, m in bad)
            print(f"  {len(bad)} disagreements | |margin| median {marg[len(marg)//2]:.3f}bp "
                  f"max {marg[-1]:.3f}bp")
            for T, w, pred, m in bad[:12]:
                print(f"    {time.strftime('%m-%d %H:%M', time.gmtime(T))} "
                      f"official={w:4s} ours={pred:4s} margin={m:+.3f}bp")
        gate = 100 * bag / bn
        print(f"\nGATE: {'PASS' if gate >= 99.0 else 'NOT YET'} "
              f"({gate:.2f}% vs 99% required to migrate the oracle)")
        if gate < 99.0 and bn < 200:
            print("  (sample is small — keep recording and re-run)")


if __name__ == "__main__":
    main()
