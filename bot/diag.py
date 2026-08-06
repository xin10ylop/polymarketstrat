"""One-command fleet diagnostics: venv/bin/python -m bot.diag [--ws]

Prints, per bot: last-48h fills, recent halts, special events, and the
latest STATUS line from journald. With --ws, also runs a 4-minute
standalone websocket probe (eth vs btc 5m books) measuring real
inter-message gaps — the receiver-independent staleness measurement.
Read-only: touches nothing, changes nothing.
"""
import json
import os
import sqlite3
import subprocess
import sys
import time

BOTS = [
    ("polybot-snipe", "bot/data/snipe"),
    ("polybot-snipe-eth", "bot/data/snipe-eth"),
    ("polybot-snipe-sol", "bot/data/snipe-sol"),
    ("polybot-snipe-btc15", "bot/data/snipe-btc15"),
    ("polybot-snipe-eth15", "bot/data/snipe-eth15"),
    ("polybot-snipe-btc1h", "bot/data/snipe-btc1h"),
    ("polybot-xwin-btc", "bot/data/xwin-btc"),
]


def last_status(unit):
    try:
        out = subprocess.run(
            ["journalctl", "-u", unit, "--since", "30 min ago", "--no-pager"],
            capture_output=True, text=True, timeout=20).stdout
        lines = [ln for ln in out.splitlines() if "STATUS" in ln]
        return lines[-1].split("STATUS", 1)[1].strip()[:220] if lines else "(no STATUS in 30m)"
    except Exception as e:  # noqa: BLE001
        return f"(journalctl failed: {e})"


def feed_churn(unit):
    """Websocket drop/reconnect counts, last 12h — the connection-crowding
    measurement (per-IP throttling suspect, audit 2026-08-06)."""
    try:
        out = subprocess.run(
            ["journalctl", "-u", unit, "--since", "12 hours ago", "--no-pager"],
            capture_output=True, text=True, timeout=25).stdout
        drops = out.count("ws dropped")
        reconn = sum(1 for ln in out.splitlines() if "reconnect" in ln.lower())
        return f"ws drops 12h: {drops} | reconnect lines: {reconn}"
    except Exception as e:  # noqa: BLE001
        return f"(churn check failed: {e})"


def applied_env(unit):
    """The env the RUNNING unit actually has (catches un-deployed changes)."""
    try:
        out = subprocess.run(
            ["systemctl", "show", unit, "--property=Environment"],
            capture_output=True, text=True, timeout=10).stdout
        keys = ("SPOT_MAX_BAR_AGE_S", "BOOK_MAX_AGE_S", "KALSHI_TELEMETRY",
                "SNIPE_MAX_CLIP", "MAX_DAILY_LOSS")
        got = [kv for kv in out.replace("Environment=", "").split()
               if kv.split("=")[0] in keys]
        return " ".join(got) if got else "(defaults)"
    except Exception as e:  # noqa: BLE001
        return f"(env check failed: {e})"


def bot_report(unit, data_dir):
    print(f"\n=== {unit} ===")
    print(f"  {feed_churn(unit)}")
    print(f"  applied env: {applied_env(unit)}")
    db_path = os.path.join(data_dir, "paper.db")
    if not os.path.exists(db_path):
        print("  (no ledger)")
        return
    db = sqlite3.connect(db_path)
    now = time.time()
    n48, last_ts = db.execute(
        "SELECT COUNT(*), MAX(ts) FROM fills WHERE ts > ?", (now - 2 * 86400,)).fetchone()
    lf = db.execute("SELECT MAX(ts) FROM fills").fetchone()[0]
    print(f"  fills 48h: {n48} | last fill: "
          f"{time.strftime('%m-%d %H:%M', time.gmtime(lf)) if lf else 'never'}")
    for ts, k, d in db.execute(
            "SELECT ts, kind, detail FROM events WHERE kind LIKE 'HALT%' "
            "AND ts > ? ORDER BY ts DESC LIMIT 3", (now - 3 * 86400,)):
        print(f"  {time.strftime('%m-%d %H:%M', time.gmtime(ts))} {k}: {d[:75]}")
    for kind, n in db.execute(
            "SELECT kind, COUNT(*) FROM events WHERE ts > ? AND kind IN "
            "('xwin_enter','xwin_deep','xwin_leg_fail','xwin_gone','near_tie',"
            "'no_outcome','mismatch_cleared') GROUP BY kind", (now - 2 * 86400,)):
        print(f"  events 48h: {kind} x{n}")
    if "15" in unit:   # kalshi telemetry coverage on decision records
        rows = db.execute("SELECT detail FROM events WHERE kind='depth' "
                          "AND ts > ? LIMIT 100", (now - 2 * 86400,)).fetchall()
        with_k = sum(1 for (d,) in rows if json.loads(d).get("k") is not None)
        print(f"  depth records 48h: {len(rows)} | with kalshi note: {with_k}")
    print(f"  STATUS: {last_status(unit)}")


def ws_probe(duration=240):
    import asyncio
    import urllib.request

    import aiohttp

    hdrs = {"User-Agent": "Mozilla/5.0"}

    def toks(slug):
        req = urllib.request.Request(
            f"https://gamma-api.polymarket.com/markets?slug={slug}", headers=hdrs)
        a = json.load(urllib.request.urlopen(req, timeout=10))
        return json.loads(a[0]["clobTokenIds"]) if a else []

    async def main():
        now = time.time()
        w = int(now - now % 300)
        m = {}
        for c in ("eth", "btc"):
            for ww in (w, w + 300):
                for t in toks(f"{c}-updown-5m-{ww}"):
                    m[t] = c
        print(f"\n=== ws probe: {len(m)} tokens, {duration}s ===", flush=True)
        last, gaps = {}, {"eth": [], "btc": []}
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(
                    "wss://ws-subscriptions-clob.polymarket.com/ws/market",
                    heartbeat=10) as ws:
                await ws.send_json({"type": "market", "assets_ids": list(m)})
                end = time.time() + duration
                while time.time() < end:
                    try:
                        r = await asyncio.wait_for(ws.receive(), timeout=5)
                    except asyncio.TimeoutError:
                        continue
                    if r.type != aiohttp.WSMsgType.TEXT:
                        continue
                    t = time.time()
                    try:
                        evs = json.loads(r.data)
                    except Exception:  # noqa: BLE001
                        continue
                    for ev in evs if isinstance(evs, list) else [evs]:
                        tok = ev.get("asset_id")
                        c = m.get(tok)
                        if c is None:
                            continue
                        if tok in last:
                            gaps[c].append(t - last[tok])
                        last[tok] = t
        for c, g in gaps.items():
            g.sort()
            if g:
                print(f"  {c}: msgs={len(g)+1} | gap p50 {g[len(g)//2]:.2f}s "
                      f"p90 {g[max(0, int(.9*len(g))-1)]:.2f}s max {g[-1]:.1f}s | "
                      f">3s gaps: {sum(x > 3 for x in g)}/{len(g)}")
            else:
                print(f"  {c}: NO MESSAGES")

    asyncio.run(main())


if __name__ == "__main__":
    subprocess.run(["uptime"], check=False)
    for unit, d in BOTS:
        bot_report(unit, d)
    if "--ws" in sys.argv:
        ws_probe()
