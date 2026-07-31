"""SQLite ledger: every order, fill, settlement and a daily PnL rollup.

PnL truth comes from the post-settlement reconciler (gamma outcome), not from
the bot's own oracle read — so an oracle misread shows up as a loss AND as a
winner_mismatch, never silently.
"""
import logging
import os
import sqlite3
import time

log = logging.getLogger("ledger")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY, ts REAL, wts INTEGER, strategy TEXT, token TEXT,
  side TEXT, price REAL, size REAL, mode TEXT, status TEXT,
  filled REAL DEFAULT 0, fees REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS fills(
  order_id INTEGER, ts REAL, wts INTEGER, strategy TEXT, token TEXT,
  price REAL, size REAL, fee REAL, maker INTEGER,
  settle REAL, pnl REAL);
CREATE TABLE IF NOT EXISTS settlements(
  wts INTEGER PRIMARY KEY, winner TEXT, oracle_winner TEXT, mismatch INTEGER,
  settle_ts REAL);
CREATE TABLE IF NOT EXISTS events(ts REAL, kind TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_fills_ts ON fills(ts);
CREATE INDEX IF NOT EXISTS ix_fills_wts ON fills(wts);
CREATE INDEX IF NOT EXISTS ix_fills_strat_ts ON fills(strategy, ts);
"""


class Ledger:
    def __init__(self, cfg):
        os.makedirs(cfg.data_dir, exist_ok=True)
        # check_same_thread=False: in live mode the order path runs in a worker
        # thread (asyncio.to_thread) so the event loop never blocks on exchange
        # I/O; sqlite serializes cross-thread writes internally (WAL + timeout).
        self.db = sqlite3.connect(os.path.join(cfg.data_dir, "paper.db"),
                                  check_same_thread=False, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(_SCHEMA)
        self.db.commit()

    def commit(self):
        self.db.commit()

    def record_order(self, o, mode):
        self.db.execute(
            "INSERT OR REPLACE INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (o.id, o.placed_ts, o.wts, o.strategy, o.token, o.side, o.price,
             o.size, mode, o.status, o.filled, o.fees))
        self.db.commit()

    def record_fill(self, o, ts, px, sz, fee, maker, commit=True):
        self.db.execute(
            "INSERT INTO fills(order_id,ts,wts,strategy,token,price,size,fee,maker) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (o.id, ts, o.wts, o.strategy, o.token, px, sz, fee, int(maker)))
        self.db.execute("UPDATE orders SET filled=?, fees=?, status=? WHERE id=?",
                        (o.filled, o.fees, o.status, o.id))
        if commit:
            self.db.commit()

    def close_order(self, o):
        self.db.execute("UPDATE orders SET status=?, filled=?, fees=? WHERE id=?",
                        (o.status, o.filled, o.fees, o.id))
        self.db.commit()

    def record_settlement(self, wts, winner, oracle_winner, token_of):
        mismatch = int(winner is not None and oracle_winner is not None
                       and winner != oracle_winner)
        self.db.execute("INSERT OR REPLACE INTO settlements VALUES(?,?,?,?,?)",
                        (wts, winner, oracle_winner, mismatch, time.time()))
        # mark PnL on this window's fills: winner token pays 1, loser pays 0
        if winner is not None:
            win_tok = token_of(winner)
            for rowid, tok, px, sz, fee in self.db.execute(
                    "SELECT rowid,token,price,size,fee FROM fills "
                    "WHERE wts=? AND settle IS NULL", (wts,)).fetchall():
                settle = 1.0 if tok == win_tok else 0.0
                pnl = (settle - px) * sz - fee
                self.db.execute("UPDATE fills SET settle=?, pnl=? WHERE rowid=?",
                                (settle, pnl, rowid))
        self.db.commit()
        return mismatch

    def event(self, kind, detail=""):
        self.db.execute("INSERT INTO events VALUES(?,?,?)", (time.time(), kind, detail))
        self.db.commit()

    # ---- queries ----
    def realized_pnl_today(self):
        day0 = time.time() - time.time() % 86400
        row = self.db.execute("SELECT COALESCE(SUM(pnl),0) FROM fills WHERE ts>=?",
                              (day0,)).fetchone()
        return row[0]

    def snipe_trailing_pnl(self, n):
        rows = self.db.execute(
            "SELECT pnl FROM fills WHERE strategy LIKE 'snipe%' AND pnl IS NOT NULL "
            "ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
        return sum(p for (p,) in rows), len(rows)

    def unmarked_old_fills(self, older_than_s=900, newer_than_s=172800):
        """Unmarked fills in the recent window only: this feeds the
        'reconciler falling behind' halt, which must reflect the reconciler's
        CURRENT health — ancient windows Polymarket never resolved (rare
        no_outcome events) are the healer's job, not a reason to halt."""
        now = time.time()
        return self.db.execute(
            "SELECT COUNT(*) FROM fills WHERE pnl IS NULL AND ts < ? AND ts > ?",
            (now - older_than_s, now - newer_than_s)).fetchone()[0]

    def unmarked_windows(self, older_than_s=900, max_age_s=7 * 86400):
        """Distinct windows with unmarked fills, for the settlement healer."""
        now = time.time()
        return [r[0] for r in self.db.execute(
            "SELECT DISTINCT wts FROM fills WHERE pnl IS NULL "
            "AND ts < ? AND ts > ?",
            (now - older_than_s, now - max_age_s)).fetchall()]

    def mark_window_by_token(self, wts, win_token, winner):
        """Late settlement: mark a window's fills against the winning token id
        (used when the market has long left the live discovery set)."""
        # OR IGNORE, not REPLACE: if the reconciler already wrote this window's
        # settlement (with its real mismatch flag), the healer must never
        # clobber it (audit M7 — a REPLACE would erase a recorded mismatch)
        self.db.execute(
            "INSERT OR IGNORE INTO settlements VALUES(?,?,?,?,?)",
            (wts, winner, None, 0, time.time()))
        n = 0
        for rowid, tok, px, sz, fee in self.db.execute(
                "SELECT rowid,token,price,size,fee FROM fills "
                "WHERE wts=? AND settle IS NULL", (wts,)).fetchall():
            settle = 1.0 if tok == win_token else 0.0
            self.db.execute("UPDATE fills SET settle=?, pnl=? WHERE rowid=?",
                            (settle, (settle - px) * sz - fee, rowid))
            n += 1
        self.db.commit()
        return n

    def snipe_fills_since_trailing_halt(self):
        """Settled snipe fills newer than the most recent trailing-PnL halt
        (a huge number if no such halt exists)."""
        row = self.db.execute(
            "SELECT MAX(ts) FROM events WHERE kind='HALT' "
            "AND detail LIKE 'snipe: trailing%'").fetchone()
        if not row or row[0] is None:
            return 1 << 30
        return self.db.execute(
            "SELECT COUNT(*) FROM fills WHERE strategy LIKE 'snipe%' "
            "AND pnl IS NOT NULL AND ts > ?", (row[0],)).fetchone()[0]

    def lifetime_pnl(self):
        return self.db.execute(
            "SELECT COALESCE(SUM(pnl),0) FROM fills").fetchone()[0]

    def mismatches(self):
        return self.db.execute(
            "SELECT COALESCE(SUM(mismatch),0) FROM settlements").fetchone()[0]

    def summary(self):
        out = {}
        for strat, n, sz, pnl, fees in self.db.execute(
                "SELECT strategy, COUNT(*), COALESCE(SUM(size),0), "
                "COALESCE(SUM(pnl),0), COALESCE(SUM(fee),0) FROM fills GROUP BY strategy"):
            out[strat] = dict(fills=n, shares=round(sz, 1), pnl=round(pnl or 0, 3),
                              fees=round(fees, 3))
        out["_mismatches"] = self.mismatches()
        return out
