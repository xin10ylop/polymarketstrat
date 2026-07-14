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
"""


class Ledger:
    def __init__(self, cfg):
        os.makedirs(cfg.data_dir, exist_ok=True)
        self.db = sqlite3.connect(os.path.join(cfg.data_dir, "paper.db"))
        self.db.executescript(_SCHEMA)
        self.db.commit()

    def record_order(self, o, mode):
        self.db.execute(
            "INSERT OR REPLACE INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (o.id, o.placed_ts, o.wts, o.strategy, o.token, o.side, o.price,
             o.size, mode, o.status, o.filled, o.fees))
        self.db.commit()

    def record_fill(self, o, ts, px, sz, fee, maker):
        self.db.execute(
            "INSERT INTO fills(order_id,ts,wts,strategy,token,price,size,fee,maker) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (o.id, ts, o.wts, o.strategy, o.token, px, sz, fee, int(maker)))
        self.db.execute("UPDATE orders SET filled=?, fees=?, status=? WHERE id=?",
                        (o.filled, o.fees, o.status, o.id))
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
            for oid, tok, px, sz, fee in self.db.execute(
                    "SELECT order_id,token,price,size,fee FROM fills "
                    "WHERE wts=? AND settle IS NULL", (wts,)):
                settle = 1.0 if tok == win_tok else 0.0
                pnl = (settle - px) * sz - fee
                self.db.execute(
                    "UPDATE fills SET settle=?, pnl=? WHERE order_id=? AND token=? "
                    "AND settle IS NULL", (settle, pnl, oid, tok))
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

    def snipe_trailing(self, n):
        rows = self.db.execute(
            "SELECT pnl FROM fills WHERE strategy LIKE 'snipe%' AND pnl IS NOT NULL "
            "ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
        if not rows:
            return None, 0
        wins = sum(1 for (p,) in rows if p > 0)
        return wins / len(rows), len(rows)

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
