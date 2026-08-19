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
CREATE INDEX IF NOT EXISTS ix_events_kind ON events(kind, ts);
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
        self.db.execute("PRAGMA synchronous=%s"
                        % ("FULL" if cfg.mode == "live" else "NORMAL"))
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
        prev = self.db.execute(
            "SELECT oracle_winner, mismatch FROM settlements WHERE wts=?",
            (wts,)).fetchone()
        if prev is not None and prev[0] is not None:
            # already settled WITH a cross-check: keep the original verdict
            oracle_winner, forced = prev[0], int(prev[1])
            mismatch = forced
        else:
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

    def mark_token_settle(self, wts, token, settle):
        """xwin packages span TWO markets (four tokens), so the single-winner
        record_settlement model doesn't fit — mark one token's fills directly."""
        for rowid, px, sz, fee in self.db.execute(
                "SELECT rowid, price, size, fee FROM fills "
                "WHERE wts=? AND token=? AND settle IS NULL",
                (wts, token)).fetchall():
            pnl = (settle - px) * sz - fee
            self.db.execute("UPDATE fills SET settle=?, pnl=? WHERE rowid=?",
                            (settle, pnl, rowid))
        self.db.commit()

    def max_order_id(self):
        return self.db.execute(
            "SELECT COALESCE(MAX(id), 0) FROM orders").fetchone()[0]

    def event(self, kind, detail=""):
        self.db.execute("INSERT INTO events VALUES(?,?,?)", (time.time(), kind, detail))
        self.db.commit()

    # ---- queries ----
    def realized_pnl_today(self):
        """Today's P&L attributed by SETTLEMENT day, not fill day.

        The old fill-ts filter had a blind spot (audit 2026-08-13): pnl is
        WRITTEN at settlement, so a 23:58 fill settling 00:06 landed in
        yesterday's bucket — a bucket whose breaker never runs again. A -$300
        boundary loss produced no halt and no shadow event at all. Joining
        through settlements.settle_ts puts every loss in the day the money
        actually moved; fills whose window has no settlement row yet fall
        back to fill ts (they also have pnl NULL, so they contribute 0).
        """
        day0 = time.time() - time.time() % 86400
        row = self.db.execute(
            "SELECT COALESCE(SUM(f.pnl),0) FROM fills f "
            "LEFT JOIN settlements s ON f.wts = s.wts "
            "WHERE COALESCE(s.settle_ts, f.ts) >= ?", (day0,)).fetchone()
        return row[0]

    def has_fill(self, wts, strategy):
        """Does any fill exist for this window+strategy? The persistent memory
        behind the strategies' in-memory `done` sets: a crash + fast restart
        forgets `done` but not the ledger, and re-entering a filled window
        doubles a real position (audit 2026-08-13)."""
        return self.db.execute(
            "SELECT 1 FROM fills WHERE wts=? AND strategy=? LIMIT 1",
            (wts, strategy)).fetchone() is not None

    def needs_ack(self):
        """The newest live incident not yet acknowledged by a human, or None.

        Live sticky halts (ambiguous POST, unexpected order error, reconciler
        breach) lived only in RiskManager memory: a restart cleared them and
        NOTHING re-derived them — 'sticky' halts that systemd Restart= could
        silently lift (audit 2026-08-13). These event kinds persist in the
        ledger; scripts/ack_incident.py writes the 'ack' row after a human
        has reconciled against the exchange."""
        row = self.db.execute(
            "SELECT MAX(ts) FROM events WHERE kind IN "
            "('live_unconfirmed','live_error','reconciler_breach')").fetchone()
        if row[0] is None:
            return None
        ack = self.db.execute(
            "SELECT COALESCE(MAX(ts),0) FROM events WHERE kind='ack'"
        ).fetchone()[0]
        return row[0] if row[0] > ack else None

    def snipe_trailing_pnl(self, n):
        # per-TAKE aggregation (audit F4: one sweep writes a row per price
        # level — on thin books 5-7 rows per take — so row-counting turned
        # "trailing 30 trades" into "trailing ~5 windows"); 7d wall-clock
        # bound so ancient fills can't dominate a slow unit's breaker.
        # Bounded by the last trailing HALT: the documented contract is
        # "a restart = the human chose to resume; judge the NEW trading" —
        # before this bound, pre-halt losers stayed in the window and
        # re-halted every ~10 good fills (2026-08-05: 9/10 winners, halted
        # by the 08-02 cluster anyway). Now a resumed bot is judged on a
        # full fresh window; absolute damage stays capped by the daily stop.
        since = time.time() - 7 * 86400
        row = self.db.execute(
            "SELECT MAX(ts) FROM events WHERE kind='HALT' "
            "AND detail LIKE 'snipe: trailing%'").fetchone()
        if row and row[0] is not None:
            since = max(since, row[0])
        rows = self.db.execute(
            "SELECT SUM(pnl) FROM fills WHERE strategy LIKE 'snipe%' "
            "AND pnl IS NOT NULL AND ts > ? GROUP BY order_id "
            "ORDER BY MAX(ts) DESC LIMIT ?",
            (since, n)).fetchall()
        return sum(p for (p,) in rows), len(rows)

    def preopen_trailing_pnl(self, n):
        """Sum of the last n settled preopen DECISIONS' pnl (grouped by
        window — audit F4: one sweep writes a row per price level). Same
        contract as the snipe version: 7d wall-clock bound, and bounded by
        the last preopen trailing HALT so a human restart is judged on a
        full fresh window of new trading."""
        since = time.time() - 7 * 86400
        row = self.db.execute(
            "SELECT MAX(ts) FROM events WHERE kind='HALT' "
            "AND detail LIKE 'preopen: trailing%'").fetchone()
        if row and row[0] is not None:
            since = max(since, row[0])
        rows = self.db.execute(
            "SELECT SUM(pnl) FROM fills WHERE strategy='preopen' "
            "AND pnl IS NOT NULL AND ts > ? GROUP BY wts "
            "ORDER BY MAX(ts) DESC LIMIT ?", (since, n)).fetchall()
        return sum(p for (p,) in rows), len(rows)

    def unmarked_old_fills(self, older_than_s=900, newer_than_s=172800):
        """Unmarked fills in the recent window only: this feeds the
        'reconciler falling behind' halt, which must reflect the reconciler's
        CURRENT health — ancient windows Polymarket never resolved (rare
        no_outcome events) are the healer's job, not a reason to halt."""
        now = time.time()
        return self.db.execute(
            "SELECT COUNT(DISTINCT wts) FROM fills WHERE pnl IS NULL "
            "AND ts < ? AND ts > ?",
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
