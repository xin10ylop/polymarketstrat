"""What did we PAY versus what was QUOTED? The gap is the clip sweeping.

  venv/bin/python -m bot.slippage
  DATA=bot/data/preopen-eth COIN=eth venv/bin/python -m bot.slippage

WHERE THIS CAME FROM. bot/lean_test.py priced the pre-open book ARCHIVE and
found eth's gated windows quoted at an average ask of 0.5212 and worth about
+3.12c a share. The live eth bot's fills average 0.5338 and it has lost $542.
btc shows the same gap, smaller: archive 0.5210, live 0.5244.

    btc   quoted 0.5210   paid 0.5244   -0.34c
    eth   quoted 0.5212   paid 0.5338   -1.26c

On a 3c edge, eth's 1.26c is nearly half of it, and it is not a strategy
problem at all — the signal picked the same windows either way.

THE MECHANISM, WHICH THIS TOOL IS HERE TO CONFIRM OR KILL. The executor
sends a marketable limit at PREOPEN_MAX_PX for PREOPEN_CLIP shares:

    self.exec.take(wts, "preopen", token, self.cfg.preopen_max_px,
                   self.cfg.preopen_clip)

A limit priced at the CEILING will walk the book as far as the ceiling to
fill the whole clip. If the touch holds fewer shares than the clip, the rest
fills higher, and the average price is worse than the quote by however thin
the top of book was. That is a size problem with a size fix — a smaller clip
pays the touch — and it costs nothing in signal.

WHAT WOULD KILL THE HYPOTHESIS: if the paid price matches the quote, the gap
is elsewhere (a stale snapshot, a moving book between T-3 and the fire) and
the clip is innocent. The tool reports depth at the touch beside the gap, so
the two explanations separate: sweeping shows up as a gap that GROWS as depth
falls below the clip.

Joins each preopen_entry event to the book recorder's T-LEAD snapshot for the
same window and side. Read-only.
"""
import json
import os
import sqlite3
import statistics as st

from bot.config import CFG
from bot.twap_verify import COIN, FAMILY, WINDOW

DATA = os.environ.get("DATA", "bot/data/preopen-btc")
BOOK_DIR = os.environ.get("BOOK_DIR", "bot/data/bookcal")
LEAD = int(os.environ.get("LEAD", "3"))
CLIP = float(os.environ.get("PREOPEN_CLIP", CFG.preopen_clip))


def main():
    lp = os.path.join(DATA, "paper.db")
    bp = os.path.join(BOOK_DIR, f"{COIN}_{FAMILY}_book.db")
    for p in (lp, bp):
        if not os.path.exists(p):
            raise SystemExit(f"missing {p}")
    db = sqlite3.connect(f"file:{lp}?mode=ro", uri=True)
    entries = {}
    for (d,) in db.execute("SELECT detail FROM events WHERE kind=?",
                           ("preopen_entry",)):
        try:
            j = json.loads(d)
        except ValueError:
            continue
        if "w" in j and "px" in j and "side" in j:
            entries[int(j["w"])] = (j["side"], float(j["px"]),
                                    float(j.get("sz", 0)))
    if not entries:
        raise SystemExit("no preopen_entry events in this ledger")

    bdb = sqlite3.connect(f"file:{bp}?mode=ro", uri=True)
    cols = {r[1] for r in bdb.execute("PRAGMA table_info(book)")}
    depth = "COALESCE(ask_cum, ask_sz)" if "ask_cum" in cols else "ask_sz"
    quote = {(w, s): (a, z, c) for w, s, a, z, c in bdb.execute(
        f"SELECT wts, side, ask, ask_sz, {depth} FROM book "
        f"WHERE lead=? AND ask IS NOT NULL", (WINDOW + LEAD,))}

    rows = []
    for w, (side, paid, sz) in sorted(entries.items()):
        q = quote.get((w, side))
        if not q:
            continue
        ask, touch_sz, cum = q
        rows.append((w, ask, paid, (paid - ask) * 100, touch_sz or 0.0,
                     cum or 0.0, sz))
    print(f"{DATA}  {COIN} {FAMILY}  clip {CLIP:.0f} shares  "
          f"book snapshot T-{LEAD}")
    print(f"{len(rows)} of {len(entries)} entries have a matching quote\n")
    if len(rows) < 10:
        print("Too few matched entries. The book recorder and the bot must")
        print("both have covered the same windows; give it more time.")
        return

    gaps = [r[3] for r in rows]
    gaps.sort()
    print(f"QUOTED at T-{LEAD} : {st.mean(r[1] for r in rows):.4f}")
    print(f"ACTUALLY PAID     : {st.mean(r[2] for r in rows):.4f}")
    print(f"SLIPPAGE          : {st.mean(gaps):+.2f}c per share   "
          f"(median {gaps[len(gaps)//2]:+.2f}, "
          f"worst {gaps[-1]:+.2f}, best {gaps[0]:+.2f})")
    worse = sum(1 for g in gaps if g > 0.001)
    print(f"                    paid ABOVE the quote on {worse}/{len(gaps)} "
          f"({100*worse/len(gaps):.0f}%)")

    # THE TEST THAT SEPARATES SWEEPING FROM A STALE QUOTE. If the clip is
    # walking the book, the gap grows as the touch gets thinner than the
    # clip. If the quote is merely stale, depth has nothing to do with it.
    print(f"\nSLIPPAGE BY DEPTH AT THE TOUCH (clip is {CLIP:.0f})")
    print(f"{'touch size':>14} {'n':>5} {'slippage¢':>11} {'avg quote':>10}")
    bands = [(0, 0.5 * CLIP), (0.5 * CLIP, CLIP), (CLIP, 2 * CLIP),
             (2 * CLIP, float("inf"))]
    label = [f"under {0.5*CLIP:.0f}", f"{0.5*CLIP:.0f}-{CLIP:.0f}",
             f"{CLIP:.0f}-{2*CLIP:.0f}", f"over {2*CLIP:.0f}"]
    for (lo, hi), lab in zip(bands, label):
        sub = [r for r in rows if lo <= r[4] < hi]
        if len(sub) < 3:
            continue
        print(f"{lab:>14} {len(sub):>5} "
              f"{st.mean(r[3] for r in sub):>+10.2f}c "
              f"{st.mean(r[1] for r in sub):>10.4f}")
    thin = [r for r in rows if r[4] < CLIP]
    fat = [r for r in rows if r[4] >= CLIP]
    print()
    if len(thin) >= 5 and len(fat) >= 5:
        t, f = st.mean(r[3] for r in thin), st.mean(r[3] for r in fat)
        print(f"touch THINNER than the clip ({len(thin)}): {t:+.2f}c")
        print(f"touch DEEPER  than the clip ({len(fat)}): {f:+.2f}c")
        if t > f + 0.2:
            print("\nTHE CLIP IS SWEEPING. Slippage tracks depth, which a")
            print("stale quote cannot do. A smaller PREOPEN_CLIP pays the")
            print("touch instead of walking the book, and costs no signal —")
            print("the same windows are still entered, just smaller.")
        else:
            print("\nSLIPPAGE DOES NOT TRACK DEPTH, so the clip is not the")
            print("cause. The quote is stale relative to the fire, or the")
            print("book moves in the seconds between. A smaller clip would")
            print("not help; look at the timing instead.")
    else:
        print("Not enough windows on both sides of the clip to separate")
        print("sweeping from a stale quote yet.")


if __name__ == "__main__":
    main()
