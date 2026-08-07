"""Wall-behind audit: venv/bin/python -m bot.loss_audit [--csv out.csv]

The 2026-08-07 big-loss analysis found every catastrophic high-price flip
had a giant 0.98/0.99 ask wall sitting behind the entry (2.3k-42k shares)
— but depth was only pulled for LOSS windows, so the wall-veto could not
be scored (selection bias: winners may have walls too). This tool closes
that gap on the droplet, where ALL depth events live.

For every depth event with a fill, it computes wall features from the
post-latency ask ladder, joins the window's settled pnl, and prints:
  1. win-rate and pnl by wall-size bucket (the discrimination test)
  2. veto counterfactual: drop windows whose max wall >= X, for a grid
     of X — pnl delta, windows dropped, crash-window coverage
Read-only: touches nothing, changes nothing.
"""
import csv
import json
import os
import sqlite3
import sys
import time

BOTS = [
    ("snipe", "bot/data/snipe"),
    ("snipe-eth", "bot/data/snipe-eth"),
    ("snipe-sol", "bot/data/snipe-sol"),
    ("snipe-btc15", "bot/data/snipe-btc15"),
    ("snipe-eth15", "bot/data/snipe-eth15"),
]

WALL_GRID = (500, 1000, 2000, 5000, 10000)


def wall_features(ev):
    """Size sitting at asks ABOVE the level we take, from the post-latency
    ladder (fall back to signal-time ladder when the post book was empty)."""
    ladder = ev.get("post") or ev.get("pre") or []
    if not ladder:
        return None
    entry_px = ladder[0][0]
    above = [(p, z) for p, z in ladder[1:]]
    return {
        "entry_px": entry_px,
        "sz_above": sum(z for _, z in above),
        "sz_ge97": sum(z for p, z in above if p >= 0.97),
        "max_level": max((z for _, z in above), default=0.0),
    }


def load_windows(db):
    """wts -> (pnl, filled shares) for settled windows."""
    out = {}
    for wts, pnl, sh in db.execute(
            "SELECT wts, SUM(pnl), SUM(size) FROM fills "
            "WHERE pnl IS NOT NULL GROUP BY wts"):
        out[wts] = (pnl, sh)
    return out


def audit(name, data_dir, writer=None):
    db_path = os.path.join(data_dir, "paper.db")
    if not os.path.exists(db_path):
        print(f"\n=== {name}: no ledger ===")
        return []
    db = sqlite3.connect(db_path)
    windows = load_windows(db)
    per_win = {}          # wts -> dict(max wall features over FILLED events)
    n_ev = n_fill = 0
    for ts, detail in db.execute("SELECT ts, detail FROM events WHERE kind='depth'"):
        try:
            ev = json.loads(detail)
        except ValueError:
            continue
        n_ev += 1
        if ev.get("fill", 0) <= 0:
            continue                       # attempts that never traded
        n_fill += 1
        f = wall_features(ev)
        if f is None:
            continue
        w = ev["w"]
        cur = per_win.setdefault(w, {"sz_above": 0.0, "sz_ge97": 0.0,
                                     "max_level": 0.0, "entry_px": f["entry_px"],
                                     "fills": 0})
        cur["sz_above"] = max(cur["sz_above"], f["sz_above"])
        cur["sz_ge97"] = max(cur["sz_ge97"], f["sz_ge97"])
        cur["max_level"] = max(cur["max_level"], f["max_level"])
        cur["entry_px"] = max(cur["entry_px"], f["entry_px"])
        cur["fills"] += 1
        if writer:
            writer.writerow([name, w, ev.get("s"), ev.get("fv"), ev.get("fill"),
                             f["entry_px"], round(f["sz_above"], 1),
                             round(f["sz_ge97"], 1), round(f["max_level"], 1),
                             round(windows.get(w, (0, 0))[0], 2)])

    joined = [(w, f, windows[w][0]) for w, f in per_win.items() if w in windows]
    print(f"\n=== {name}: depth events {n_ev} (filled {n_fill}) | "
          f"windows with depth+outcome: {len(joined)} ===")
    if not joined:
        return []

    print("  wall(sz_above) bucket  |   n | win% | total pnl   (high-px entries >=0.80 only)")
    hi = [(w, f, p) for w, f, p in joined if f["entry_px"] >= 0.80]
    buckets = [(0, 500), (500, 2000), (2000, 10000), (10000, 1e12)]
    for lo, hb in buckets:
        rows = [(w, f, p) for w, f, p in hi if lo <= f["sz_above"] < hb]
        if rows:
            wins = sum(1 for _, _, p in rows if p > 0)
            tot = sum(p for _, _, p in rows)
            lab = f"[{lo:.0f},{'inf' if hb > 1e9 else f'{hb:.0f}'})"
            print(f"  {lab:20s}  | {len(rows):3d} | {100*wins/len(rows):3.0f}% | ${tot:+9.2f}")

    print("  veto counterfactual (drop window when max sz_above >= X):")
    base = sum(p for _, _, p in joined)
    for x in WALL_GRID:
        kept = sum(p for _, f, p in joined if f["sz_above"] < x)
        dropped = [(w, p) for w, f, p in joined if f["sz_above"] >= x]
        d_loss = sum(1 for _, p in dropped if p <= -50)
        print(f"    X={x:6d}: pnl ${kept:+9.2f} (delta {kept-base:+9.2f}) | "
              f"dropped {len(dropped)} windows ({d_loss} were big losses)")
    return joined


if __name__ == "__main__":
    writer = None
    if "--csv" in sys.argv:
        out = sys.argv[sys.argv.index("--csv") + 1]
        fh = open(out, "w", newline="")
        writer = csv.writer(fh)
        writer.writerow(["bot", "wts", "side", "fv", "fill", "entry_px",
                         "sz_above", "sz_ge97", "max_level", "window_pnl"])
    print(f"loss_audit @ {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")
    for name, d in BOTS:
        audit(name, d, writer)
    if writer:
        fh.close()
        print(f"\nper-event csv written: {out}")
