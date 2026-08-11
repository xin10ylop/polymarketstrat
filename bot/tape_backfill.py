"""Pull the whole post-rule-change trade tape into the cache, before it ages out.

  venv/bin/python -m bot.tape_backfill                  # btc 5m
  COIN=eth FAMILY=15m venv/bin/python -m bot.tape_backfill
  ALL=1 venv/bin/python -m bot.tape_backfill            # every coin+family

WHY NOW. Of everything this project records, the trade tape is the ONLY thing
that can be recovered after the fact — Polymarket serves per-market trades for
roughly thirty days. The 1s Chainlink grid cannot: recording began 08-08 11:35
and the rule changed 08-07 00:00, so thirty-five hours of post-change windows
are gone permanently. The same will be true of the tape in early September if
nobody pulls it.

Today the cache holds only what some tool happened to ask for — about 650
windows against roughly 8,000 that exist post-change, because every tool
fetches only the windows passing its own gate. That is enough to answer the
questions already asked and nothing else. Any future question about the
windows we DIDN'T trade — what the book did when the tilt was flat, whether a
gate we never tried would have worked — needs the rest.

Cost: about 25KB per window, so the full post-change set across five coins and
both families is roughly 200MB against 16GB free. It is a one-shot script, not
a daemon, so it adds no memory pressure to the box.

Resumable and idempotent: already-cached windows are skipped, so it can be
killed and re-run.
"""
import os
import time

from bot.scalp_backtest import cache_db, fetch_window
from bot.twap_verify import COIN, FAMILY, WINDOW

RULE = 1786060800                     # 2026-08-07 00:00 UTC
WORKERS = int(os.environ.get("WORKERS", "6"))
COMBOS = os.environ.get("ALL") and [
    (c, f) for c in ("btc", "eth", "sol", "xrp", "doge")
    for f in ("5m", "15m")] or [(COIN, FAMILY)]


def run(coin, family):
    os.environ["COIN"], os.environ["FAMILY"] = coin, family
    import importlib
    import bot.twap_verify as tv
    importlib.reload(tv)
    import bot.scalp_backtest as sb
    importlib.reload(sb)
    win = tv.WINDOW
    now = int(time.time())
    # leave the last two windows alone: gamma has not resolved them yet, and
    # an unresolved market caches as a miss that would never be retried
    end = now - now % win - 2 * win
    wtss = list(range(RULE, end, win))
    db = sb.cache_db()
    have = {w for (w,) in db.execute("SELECT wts FROM tape")}
    todo = [w for w in wtss if w not in have]
    print(f"{coin} {family}: {len(wtss)} windows since the rule change, "
          f"{len(have)} cached, {len(todo)} to fetch", flush=True)
    if not todo:
        return 0
    from concurrent.futures import ThreadPoolExecutor
    got = miss = 0
    with ThreadPoolExecutor(WORKERS) as ex:
        for i, r in enumerate(ex.map(sb.fetch_window, todo), 1):
            if r:
                db.execute("INSERT OR REPLACE INTO tape VALUES(?,?,?,?)",
                           (r[0], r[1], __import__("json").dumps(
                               r[2], separators=(",", ":")), time.time()))
                got += 1
            else:
                miss += 1
            if i % 100 == 0:
                db.commit()
                print(f"  {i}/{len(todo)}  kept {got}  unresolvable {miss}",
                      flush=True)
    db.commit()
    print(f"  done: kept {got}, unresolvable {miss} "
          f"(markets that never minted or never settled)", flush=True)
    return got


def main():
    total = 0
    for coin, family in COMBOS:
        try:
            total += run(coin, family)
        except SystemExit as e:
            print(f"{coin} {family}: skipped ({e})")
        except Exception as e:  # noqa: BLE001
            print(f"{coin} {family}: failed ({str(e)[:90]})")
    print(f"\n{total} windows added. Re-run any time; cached windows are "
          f"skipped, so this is safe to kill and restart.")


if __name__ == "__main__":
    main()
