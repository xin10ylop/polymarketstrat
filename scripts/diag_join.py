import os, sqlite3, time
for coin in ("btc", "eth"):
    print(f"\n=== {coin} ===")
    gp = f"bot/data/twapcal/{coin}_1s.db"
    bp = f"bot/data/bookcal/{coin}_5m_book.db"
    if os.path.exists(gp):
        g = sqlite3.connect(gp)
        n, lo, hi = g.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM px").fetchone()
        print(f"  grid: {n} rows  {time.strftime('%m-%d %H:%M', time.gmtime(lo))}"
              f" -> {time.strftime('%m-%d %H:%M', time.gmtime(hi))}"
              f"  density {100*n/max(1,hi-lo+1):.1f}%")
    else:
        print(f"  grid: MISSING {gp}")
    if os.path.exists(bp):
        b = sqlite3.connect(bp)
        n, lo2, hi2 = b.execute("SELECT COUNT(*), MIN(wts), MAX(wts) FROM book").fetchone()
        leads = [r[0] for r in b.execute("SELECT DISTINCT lead FROM book ORDER BY lead DESC")]
        print(f"  book: {n} rows  windows {time.strftime('%m-%d %H:%M', time.gmtime(lo2))}"
              f" -> {time.strftime('%m-%d %H:%M', time.gmtime(hi2))}")
        print(f"  book leads: {leads}")
        print(f"  book wts %300: {sorted({r[0] for r in b.execute('SELECT wts%300 FROM book')})}")
        print(f"  rows with a real ask: "
              f"{b.execute('SELECT COUNT(*) FROM book WHERE ask IS NOT NULL').fetchone()[0]}")
        if os.path.exists(gp):
            gw = {w for (w,) in g.execute(
                "SELECT DISTINCT (ts/300)*300 FROM px")}
            bw = {r[0] for r in b.execute("SELECT DISTINCT wts FROM book")}
            print(f"  windows in BOTH grid and book: {len(gw & bw)}")
    else:
        print(f"  book: MISSING {bp}")
