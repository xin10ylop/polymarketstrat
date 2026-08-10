"""Is the paper record reachable in live? The collapse-fill audit.

  venv/bin/python -m bot.parity_audit
  DROP=0.03 venv/bin/python -m bot.parity_audit
  DB=/opt/polymarketstrat/bot/data/paper.db venv/bin/python -m bot.parity_audit

THE QUESTION THIS EXISTS TO SETTLE. Everything we believe about this
strategy rests on a paper ledger, and one path through the paper executor
has never been checked against what a real order would do.

In paper the snipe sees an ask, sleeps snipe_take_recheck_s (0.5s) to
imitate order latency, RE-READS the book, and sweeps whatever it now finds.
The re-read runs the full entry gate — except that snipe_price_floor
defaults to 0.0, so a book that has fallen from 0.97 to a penny still
passes, and paper buys the wreckage at the wreckage price.

Live does not work that way. It sends a marketable limit at the price it
saw and the exchange decides. It reaches the same collapsed book — a FAK at
0.97 does sweep a 0.29 ask — so the fill is not fictional. What is
optimistic is the RACE: paper takes the top of the collapsed ladder
deterministically, while a real order arrives a quarter-second late into
the one moment when every other taker is grabbing the same cheap shares.

So the paper price on a collapse fill is the best case, not the expected
case. This measures how much of the record depends on it.

THE NUMBER THAT NEEDS NO ASSUMPTIONS is the first table: do collapse fills
WIN less often than clean ones? If they do, the collapse was informed, and
a worse fill price is the smaller half of the problem. If they win at the
same rate, the drop is noise and the cheap price was a gift we would
partly have kept.

Then three counterfactuals:
  RECORDED  the ledger as it stands — we won every race
  AT SIGNAL every fill repriced at the ask we SAW when we decided — we lost
            every race and filled against what was left behind it
  DROPPED   the collapse fills simply never happen
LIVE LIES BETWEEN AT SIGNAL AND RECORDED, because a real fill price sits
between the collapsed ask and the limit we sent. DROPPED is not a bound; it
is the separate question of how much of the record the collapse path is
carrying at all. If the cheap buckets survive AT SIGNAL, this is closed and
the record stands.

Read-only. Opens the ledger read-only and writes nothing.
"""
import glob
import json
import os
import sqlite3

# every unit keeps its own ledger under bot/data/<unit>/paper.db, so pool them
DB = os.environ.get("DB", "")
ROOT = os.environ.get("DATA_ROOT", "bot/data")
DROP = float(os.environ.get("DROP", "0.05"))     # what counts as a collapse
# the venue switched 5m/15m settlement to a rolling TWAP at this instant; the
# "book watches the tape, we settle on chainlink" story only applies BEFORE it
CUTOVER = int(os.environ.get("CUTOVER", "1786060800"))   # 2026-08-07 00:00 UTC
FEE = float(os.environ.get("TAKER_FEE_MULT", "0.07"))
STRAT = os.environ.get("STRAT", "snipe")


def fee_of(p, sz):
    return FEE * p * (1 - p) * sz


def wilson(k, n):
    if not n:
        return (0.0, 1.0)
    z, p = 1.96, k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, c - h), min(1.0, c + h))


def load(path):
    """(settled takes, joined [(order, pre_ask, post_ask)]) for one ledger."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    # orders that actually filled, with settled economics rolled up per order
    orders = {}
    for oid, ts, wts, strat, px, filled in db.execute(
            "SELECT id, ts, wts, strategy, price, filled FROM orders "
            "WHERE filled > 0 AND strategy LIKE ?", (STRAT + "%",)):
        orders[oid] = dict(ts=ts, wts=wts, strat=strat, px=px, filled=filled,
                           sz=0.0, fee=0.0, pnl=0.0, settle=None, n=0)
    for oid, px, sz, fee, settle, pnl in db.execute(
            "SELECT order_id, price, size, fee, settle, pnl FROM fills "
            "WHERE strategy LIKE ?", (STRAT + "%",)):
        o = orders.get(oid)
        if o is None or settle is None:
            continue
        o["sz"] += sz
        o["fee"] += fee
        o["pnl"] += pnl
        o["settle"] = settle
        o["n"] += 1
    settled = {k: v for k, v in orders.items() if v["settle"] is not None
               and v["sz"] > 0}

    # depth telemetry: the ask ladder at signal time and after the latency gate
    evs = []
    for ts, detail in db.execute(
            "SELECT ts, detail FROM events WHERE kind='depth' ORDER BY ts"):
        try:
            d = json.loads(detail)
        except Exception:  # noqa: BLE001
            continue
        if not d.get("fill"):
            continue
        pre = d.get("pre") or []
        post = d.get("post") or []
        if not pre or not post:
            continue
        evs.append((ts, d["w"], [(float(a), float(b)) for a, b in pre],
                    float(post[0][0]), float(d["fill"])))

    # join each order to the depth event written immediately after its take
    joined = []
    by_w = {}
    for e in evs:
        by_w.setdefault(e[1], []).append(e)
    for oid, o in settled.items():
        cands = [e for e in by_w.get(o["wts"], [])
                 if -1.0 <= e[0] - o["ts"] <= 6.0
                 and abs(e[4] - o["filled"]) < 0.6]
        if not cands:
            continue
        e = min(cands, key=lambda e: abs(e[0] - o["ts"]))
        joined.append((o, e[2], e[3]))          # order, pre LADDER, post_ask
    return list(settled.values()), joined


def sweep(ladder, size):
    """Average price to buy `size` by walking the ladder we SAW, cheapest
    first. The old version used only the best level, which quietly assumed
    the whole clip filled at the touch — that made the counterfactual
    OPTIMISTIC for ordinary fills, not pessimistic, and inverted the test."""
    need, cost, got = size, 0.0, 0.0
    for px, sz in sorted(ladder):
        if need <= 0:
            break
        t = min(sz, need)
        cost += px * t
        got += t
        need -= t
    if need > 0 and ladder:                 # deeper than we could see
        cost += max(p for p, _ in ladder) * need
        got += need
    return cost / got if got else None


def main():
    paths = ([DB] if DB else
             sorted(glob.glob(os.path.join(ROOT, "*", "paper.db"))))
    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        raise SystemExit(f"no ledger found under {ROOT}/*/paper.db (set DB=)")

    settled, joined = [], []
    print(f"{'unit':>14} {'takes':>7} {'PnL':>11} {'joined':>7} {'joined PnL':>12}")
    for p in paths:
        s, j = load(p)
        if not s:
            continue
        settled += s
        joined += j
        print(f"{os.path.basename(os.path.dirname(p)):>14} {len(s):>7} "
              f"{sum(o['pnl'] for o in s):>+11.2f} {len(j):>7} "
              f"{sum(o['pnl'] for o, _, _ in j):>+12.2f}")

    # WHEN was the record made? The telemetry only starts 07-31, so before
    # arguing about fill fidelity, see which days the money actually came from.
    import time as _tt
    byday = {}
    for o in settled:
        d = _tt.strftime("%m-%d", _tt.gmtime(o["ts"]))
        a = byday.setdefault(d, [0, 0.0])
        a[0] += 1
        a[1] += o["pnl"]
    print("\nWHEN THE MONEY WAS MADE (all settled takes, every unit)")
    run = 0.0
    for d in sorted(byday):
        n_, p_ = byday[d]
        run += p_
        print(f"  {d}  {n_:>4} takes  {p_:>+10.2f}   running {run:>+10.2f}")

    tot_orders, tot_pnl = len(settled), sum(o["pnl"] for o in settled)
    print(f"\npooled: {tot_orders} settled takes, ${tot_pnl:+,.2f} | "
          f"depth telemetry joined to {len(joined)} "
          f"({100*len(joined)/max(1,tot_orders):.0f}%)")
    if not joined:
        print("\nnothing joinable — the depth telemetry does not cover these "
              "fills, so this question cannot be answered from the ledgers")
        return
    cov_pnl = sum(o["pnl"] for o, _, _ in joined)
    ts_all = sorted(o["ts"] for o, _, _ in joined)
    import time as _t
    print(f"joined takes span "
          f"{_t.strftime('%m-%d', _t.gmtime(ts_all[0]))} -> "
          f"{_t.strftime('%m-%d', _t.gmtime(ts_all[-1]))} UTC "
          f"(the depth tap was added 07-31; nothing before it is covered)")
    if tot_pnl and abs(cov_pnl) < 0.5 * abs(tot_pnl):
        print(f"*** PnL COVERAGE IS THE REAL LIMIT: the joined takes carry "
              f"${cov_pnl:+,.2f}\n    of a ${tot_pnl:+,.2f} record. This audit "
              f"says NOTHING about the\n    ${tot_pnl-cov_pnl:+,.2f} earned "
              f"outside the telemetry window. ***")
    print(f"those {len(joined)} takes carry ${cov_pnl:+,.2f} of it"
          + ("" if len(joined) >= 0.6 * tot_orders else
             "\nCOVERAGE IS LOW — the rest predate the telemetry and this "
             "audit says nothing about them") + "\n")

    coll = [j for j in joined if j[1][0][0] - j[2] >= DROP]
    clean = [j for j in joined if j[1][0][0] - j[2] < DROP]

    # ---------------- the assumption-free number: do collapses win less? ----
    print(f"DID THE BOOK KNOW SOMETHING?  (collapse = ask fell >= {DROP:.2f} "
          f"during the {0.5:.1f}s latency gate)")
    print(f"{'group':>10} {'takes':>6} {'shares':>8} {'won%':>6} {'95% CI':>13} "
          f"{'avg paid':>9} {'avg seen':>9} {'PnL':>11}")
    for lab, grp in (("collapse", coll), ("clean", clean)):
        if not grp:
            print(f"{lab:>10} {0:>6}")
            continue
        n = len(grp)
        k = sum(1 for o, _, _ in grp if o["settle"] == 1.0)
        lo, hi = wilson(k, n)
        sz = sum(o["sz"] for o, _, _ in grp)
        paid = sum(o["px"] * o["sz"] for o, _, _ in grp) / sz
        seen = sum(p[0][0] * o["sz"] for o, p, _ in grp) / sz
        print(f"{lab:>10} {n:>6} {sz:>8.0f} {100*k/n:>5.0f}% "
              f"[{100*lo:>4.0f},{100*hi:>4.0f}]% {paid:>9.3f} {seen:>9.3f} "
              f"{sum(o['pnl'] for o, _, _ in grp):>+11.2f}")
    if coll and clean:
        kc = sum(1 for o, _, _ in coll if o["settle"] == 1.0) / len(coll)
        kl = sum(1 for o, _, _ in clean if o["settle"] == 1.0) / len(clean)
        print(f"\n  collapse fills win {100*(kc-kl):+.1f} points vs clean ones.")
        print("  Clearly negative => the drop was informed and live's worse")
        print("  price is the smaller half of the problem. Near zero => the")
        print("  drop is noise, and the cheap price was partly real.")

    # -------------------------------- the same question, either side of the
    # rule change. The "book watches the tape, we settle on chainlink" story
    # predicts collapses were INFORMATIVE FOR US before 08-07 and against us
    # after, when the book started tracking the TWAP. Split it and see.
    print(f"\nSPLIT AT THE 08-07 CUTOVER — the prediction only ever applied "
          f"to the left column")
    print(f"{'era':>22} {'collapse n':>11} {'won%':>6} {'clean n':>8} "
          f"{'won%':>6} {'gap':>7}")
    for lab, lo_t, hi_t in (("pre-change (to 08-07)", 0, CUTOVER),
                            ("post-change", CUTOVER, 1 << 62)):
        c = [j for j in coll if lo_t <= j[0]["ts"] < hi_t]
        cl = [j for j in clean if lo_t <= j[0]["ts"] < hi_t]
        if not c or not cl:
            print(f"{lab:>22} {len(c):>11} {'-':>6} {len(cl):>8} {'-':>6} "
                  f"{'(too few)':>7}")
            continue
        kc = sum(1 for o, _, _ in c if o["settle"] == 1.0) / len(c)
        kl = sum(1 for o, _, _ in cl if o["settle"] == 1.0) / len(cl)
        print(f"{lab:>22} {len(c):>11} {100*kc:>5.0f}% {len(cl):>8} "
              f"{100*kl:>5.0f}% {100*(kc-kl):>+6.0f}p")

    # ------------------------------------------------ the counterfactuals ---
    def rescore(grp, price_of):
        t = 0.0
        for o, pre, post in grp:
            p = price_of(o, pre, post)
            if p is None:
                p = o["px"]
            t += (o["settle"] - p) * o["sz"] - fee_of(p, o["sz"])
        return t

    at_px = lambda o, pre, post: sweep(pre, o["sz"])

    rec = sum(o["pnl"] for o, _, _ in joined)
    at_signal = rescore(joined, at_px)
    dropped = sum(o["pnl"] for o in (j[0] for j in clean))
    print(f"\nWHAT THE RECORD IS WORTH UNDER EACH ASSUMPTION "
          f"(the {len(joined)} joined takes only)")
    print(f"{'RECORDED  (we won every race)':<44} {rec:>+11.2f}")
    print(f"{'AT SIGNAL (we lost every race)':<44} {at_signal:>+11.2f}")
    print(f"{'DROPPED   (collapse fills never happen)':<44} {dropped:>+11.2f}")
    print(f"\nLIVE IS SOMEWHERE IN THE FIRST TWO: ${min(rec, at_signal):+,.2f} "
          f"to ${max(rec, at_signal):+,.2f}, a spread of "
          f"${abs(rec - at_signal):,.2f}.")
    print(f"DROPPED is not a bound — it says the collapse path accounts for "
          f"${rec - dropped:+,.2f}\nof the recorded result on these takes.")

    # ------------------------------------------- where it lands, by price ---
    print(f"\nBY ENTRY PRICE — the split that located the edge")
    print(f"{'bucket':>12} {'takes':>6} {'shares':>8} {'recorded':>11} "
          f"{'at signal':>11} {'of which collapse':>18}")
    for a, b in ((0.0, 0.50), (0.50, 0.80), (0.80, 0.95), (0.95, 1.01)):
        sel = [j for j in joined if a <= j[0]["px"] < b]
        if not sel:
            continue
        c = [j for j in sel if j[1][0][0] - j[2] >= DROP]
        print(f"{f'{a:.2f}-{b:.2f}':>12} {len(sel):>6} "
              f"{sum(o['sz'] for o, _, _ in sel):>8.0f} "
              f"{sum(o['pnl'] for o, _, _ in sel):>+11.2f} "
              f"{rescore(sel, at_px):>+11.2f} "
              f"{f'{len(c)} takes':>18}")
    print("\nIf the cheap buckets hold up under AT SIGNAL, the record is not a")
    print("latency artifact and the collapse path is a detail. If they only")
    print("exist under RECORDED, then the 81% of profit that came from cheap")
    print("entries was the executor buying wreckage it could not have won.")


if __name__ == "__main__":
    main()
