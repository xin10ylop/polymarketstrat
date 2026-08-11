"""How much of each paper bot's life was spent halted, and what that censored.

  venv/bin/python -m bot.halt_audit

WHY THIS MATTERS MORE THAN IT SOUNDS. A halted bot records nothing, and until
2026-08-11 it also SAID nothing — no counter, no log line. So the gap does not
appear as missing data; it appears as a shorter, tidier history. That is the
dangerous shape, because the gaps are not random. The breaker fires precisely
when the day is going badly, so every halt truncates a losing run while every
winning run records in full.

Two consequences, both of which make the ledgers read better than the truth:

  WIN RATE IS BIASED UP. The losses that would have followed the breaker are
  absent from the denominator as well as the numerator.

  DRAWDOWN IS A FLOOR, NOT A MEASUREMENT. "-$580 from peak" means the bot
  stopped losing because it stopped trading, not because the strategy turned.
  The real path is unobserved.

Neither can be repaired from the ledger — the windows were never traded. They
CAN be priced from the tape archive (bot/scalp_backtest.py), which is why the
tape backfill matters. What this tool does is tell you how much to distrust,
and which intervals to re-price.

Read-only. Reports every paper-* and preopen-* ledger it finds.
"""
import glob
import os
import shutil
import sqlite3
import subprocess
import time

# window seconds by family, for turning dark time into windows lost
FAMILY_SECS = {"5m": 300, "15m": 900, "1h": 3600}


def unit_state(name):
    """"active" | "stopped" | "unknown" for the bot behind a ledger directory.

    THE FIRST VERSION OF THIS TOOL HAD NO SUCH CHECK and ran every unlifted
    halt to the present instant. Retired units — snipe-*, stopped in early
    August — therefore reported 601, 225 and 106 hours "dark" apiece and a
    fleet total of 12,270 lost windows, when the real figure across the two
    bots that still exist was 141. A halt with no lift means the bot never
    recorded coming back; it does NOT mean the bot is sitting there halted.
    Distinguishing those is the difference between a number and a scare.

    A halted bot writes nothing, so file mtime cannot separate the two — only
    the service manager knows. No systemctl (or an unrecognised name) yields
    "unknown", which is reported as a bounded range rather than a guess.
    """
    if not shutil.which("systemctl"):
        return "unknown"
    for unit in (f"polybot-{name}",
                 f"polybot-{name}".replace("15", "-15m"),
                 f"polybot-{name}-15m"):
        try:
            r = subprocess.run(["systemctl", "is-active", unit],
                               capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        out = r.stdout.strip()
        if out == "active":
            return "active"
        if out in ("inactive", "failed"):
            return "stopped"
    return "unknown"


def family_of(path):
    """15m units carry it in the directory name; everything else is 5m."""
    return "15m" if "-15m" in path else "5m"


def spans(db):
    """[(start, end_or_None, reason, how)] — halts paired with how they ended.

    `how` is "lifted" (the breaker released it), "restart" (a deploy cleared
    the in-memory halt set, which writes no HALT_LIFTED), or None (no
    evidence it ever ended). HALT_LIFTED carries the same reason text as its
    HALT but the ledger does not link them by id, so this pairs strictly in
    time order.
    """
    # PROOF THE STRATEGY LOOP RAN AGAIN. A halt cleared by a RESTART writes no
    # HALT_LIFTED — the in-memory halt set is simply gone — so an unlifted
    # halt has no end marker and would run to the present for ever, growing by
    # an hour every hour.
    #
    # THE LIST IS SHORT ON PURPOSE, and getting it wrong has bitten in both
    # directions. `start` was in it once: a restart writes `start` whether or
    # not the bot then trades, and btc 5m restarted at 11:50, re-halted at
    # 11:51:15 and stayed dark until 12:17 — so `start` closed the outage 27
    # minutes early. The settlement healer (late_settlement, no_outcome) runs
    # while a bot is halted and would do the same. Only rows a HALTED bot
    # provably cannot write belong here: two the pre-open strategy writes
    # after its halt check, and SHADOW_HALT, which the risk manager writes
    # only when it has decided NOT to halt.
    #
    # Ending a dark period early UNDERSTATES the censoring, which is the
    # direction that makes a bad ledger look usable. When in doubt, leave a
    # kind out and let the period run long.
    alive = ("preopen_entry", "preopen_mark", "SHADOW_HALT")
    revive = [ts for (ts,) in db.execute(
        "SELECT ts FROM events WHERE kind IN "
        f"({','.join('?' * len(alive))}) ORDER BY ts", alive)]

    def back_after(t):
        return next((r for r in revive if r > t), None)

    rows = list(db.execute(
        "SELECT ts, kind, detail FROM events "
        "WHERE kind IN ('HALT','HALT_LIFTED','SHADOW_HALT') ORDER BY ts"))
    out, open_at, open_why = [], None, ""
    shadows = []
    for ts, kind, detail in rows:
        if kind == "SHADOW_HALT":
            shadows.append((ts, detail))
        elif kind == "HALT":
            if open_at is None:
                open_at, open_why = ts, detail
                continue
            # A SECOND HALT IS EITHER THE SAME OUTAGE OR A NEW ONE, and the
            # difference is whether the bot traded in between. Every restart
            # re-derives the same losing day and writes another HALT row, so
            # treating them all as new outages would multiply one bad day
            # into a dozen; treating them all as one would swallow a genuine
            # second outage whole. The revival is what separates them.
            back = back_after(open_at)
            if back is not None and back < ts:
                out.append((open_at, back, open_why, "restart"))
                open_at, open_why = ts, detail
        elif kind == "HALT_LIFTED" and open_at is not None:
            out.append((open_at, ts, open_why, "lifted"))
            open_at, open_why = None, ""
    if open_at is not None:
        back = back_after(open_at)
        out.append((open_at, back, open_why, "restart" if back else None))
    return out, shadows


def main():
    paths = sorted(glob.glob("bot/data/*/paper.db"))
    if not paths:
        raise SystemExit("no ledgers under bot/data/*/paper.db")
    now = time.time()
    print(f"{'ledger':<22} {'unit':>8} {'dark':>9} {'windows':>8}  periods")
    print("-" * 78)
    live_dark = live_win = 0
    retired, unsure = [], []
    for p in paths:
        name = os.path.basename(os.path.dirname(p))
        try:
            db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            per, shadows = spans(db)
            last = db.execute("SELECT MAX(ts) FROM events").fetchone()[0]
        except sqlite3.Error as e:
            print(f"{name:<22} unreadable ({str(e)[:40]})")
            continue
        if last is None:
            continue
        state = unit_state(name) if (per or shadows) else "-"
        T = FAMILY_SECS[family_of(name)]
        # AN OPEN HALT ENDS WHERE THE EVIDENCE ENDS. For a running bot that
        # is now; for a retired one it is its last ledger write, because
        # everything after that is time the bot did not exist for.
        def close(e):
            if e is not None:
                return e
            return now if state == "active" else last
        dark = sum(close(e) - s for s, e, _, _ in per)
        lost = int(dark // T)
        if state == "active":
            live_dark += dark
            live_win += lost
        elif per and state == "stopped":
            retired.append(name)
        elif per:
            # UNKNOWN IS NOT RETIRED. Without a service manager to ask, an
            # open halt could be a live bot sitting dark or a unit deleted
            # last week, and those differ by orders of magnitude. Report the
            # bounds instead of picking one and calling it a measurement.
            hi = sum((e if e is not None else now) - s
                     for s, e, _, _ in per)
            unsure.append((name, dark, hi, int(hi // T)))
        if per or shadows:
            print(f"{name:<22} {state:>8} {dark/3600:>8.2f}h {lost:>8}  "
                  f"{len(per)} halt(s), {len(shadows)} shadow")
            for s, e, why, how in per:
                end = close(e)
                tag = (how if how else
                       "STILL DARK" if state == "active" else
                       "unit gone" if state == "stopped" else "unknown")
                print(f"  {'':<20} "
                      f"{time.strftime('%m-%d %H:%M', time.gmtime(s))} -> "
                      f"{time.strftime('%m-%d %H:%M', time.gmtime(end))} "
                      f"{(close(e)-s)/3600:>6.2f}h {tag:<10} {why[:38]}")
            for s, why in shadows:
                print(f"  {'':<20} "
                      f"{time.strftime('%m-%d %H:%M', time.gmtime(s))} "
                      f"   SHADOW — kept trading, day fully recorded")
        else:
            print(f"{name:<22} {state:>8} {'-':>9} {'-':>8}  clean")
    print("-" * 78)
    print(f"{'LIVE UNITS':<22} {'':>8} {live_dark/3600:>8.2f}h {live_win:>8} "
          f"windows never evaluated")
    if retired:
        print(f"\n{len(retired)} retired ledger(s) excluded from the total "
              f"({', '.join(retired)}).")
        print("Their halts are real history but the units no longer run, so")
        print("their dark time is bounded at their last write, not at now.")
    if unsure:
        print(f"\n{len(unsure)} ledger(s) whose unit could not be identified — "
              f"reported as a range,")
        print("low = dark to the last ledger write (certain), "
              "high = dark to now (only if still running):")
        for nm, lo, hi, hw in unsure:
            print(f"  {nm:<20} {lo/3600:>7.2f}h .. {hi/3600:>8.2f}h "
                  f"(up to {hw} windows)")
    print()
    if live_win:
        print("THE LIVE FIGURE IS NOT RECOVERABLE FROM THE LEDGER — the bot")
        print("never traded those windows. It IS priceable from the tape")
        print("archive, and those are exactly the intervals where doing so")
        print("matters, because a halt only ever fires on a bad day. Until")
        print("then, treat live win rates as UPPER bounds and drawdowns as")
        print("LOWER bounds.")
    else:
        print("No halt is currently censoring a running bot.")


if __name__ == "__main__":
    main()
