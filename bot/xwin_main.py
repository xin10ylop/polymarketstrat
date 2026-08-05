"""Entry point for the XWIN shared-close scanner (paper-only, BTC).

Runs TWO market feeds (5m + 15m discovery/books) plus the chainlink oracle
(strike source) in one lean process — no spot feed, no snipe, no toll. Its
own ledger (BOT_DATA_DIR) so PnL is separately comparable, per the operator's
requirement. Refuses to start in live mode: the strategy has no live executor
and must earn one through leg-fail telemetry first.
"""
import asyncio
import dataclasses
import logging
import time

from bot.config import CFG
from bot.engine.executor import PaperExecutor
from bot.engine.ledger import Ledger
from bot.engine.risk import RiskManager
from bot.feeds.clob import ClobFeed
from bot.feeds.oracle import Oracle
from bot.strategies.xwin import XwinStrategy

log = logging.getLogger("xwin-main")


class _StubSpot:
    """RiskManager wants a spot feed only for the snipe's silence check."""

    def silence(self):
        return 0.0


async def _supervised(name, fn, *args):
    while True:
        try:
            await fn(*args)
            log.error("task %s exited; restarting in 10s", name)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("task %s crashed; restarting in 10s", name)
        await asyncio.sleep(10)


async def xwin_reconciler(cfg, clob5, clob15, ledger):
    """Package-aware settlement: marks each token directly (a package spans
    two markets, so record_settlement's single-winner model does not apply).
    One bookkeeping row per market keyed by its own wts is NOT written —
    instead one row per (market close) pair via INSERT OR IGNORE on C."""
    done = set()
    while True:
        now = time.time()
        for feed, T in ((clob5, 300), (clob15, 900)):
            for wts, mk in list(feed.markets.items()):
                key = (wts, T)
                if key in done or now < wts + T + 60:
                    continue
                winner = await feed.fetch_outcome(mk)
                if winner is None:
                    if now > wts + T + cfg.outcome_patience_s:
                        done.add(key)
                        ledger.event("no_outcome", mk.slug)
                    continue
                win_tok = mk.token_up if winner == "up" else mk.token_down
                lose_tok = mk.token_down if winner == "up" else mk.token_up
                ledger.mark_token_settle(wts, win_tok, 1.0)
                ledger.mark_token_settle(wts, lose_tok, 0.0)
                ledger.db.execute(
                    "INSERT OR IGNORE INTO settlements VALUES(?,?,?,?,?)",
                    (wts + T, f"{T}s:{winner}", None, 0, time.time()))
                ledger.db.commit()
                done.add(key)
        if len(done) > 400:
            done = {k for k in done if k[0] > now - 6 * 3600}
        await asyncio.sleep(20)


async def status(ledger, strat, oracle, clob5, clob15):
    while True:
        await asyncio.sleep(60)
        s = ledger.summary().get("xwin", {})
        log.info("STATUS xwin entries=%d leg_fails=%d fills=%s pnl=%s "
                 "oracle_stale=%.1fs mkts=%d/%d",
                 strat.entries, strat.leg_fails, s.get("fills", 0),
                 s.get("pnl", 0.0), oracle.staleness(),
                 len(clob5.markets), len(clob15.markets))


async def amain():
    logging.basicConfig(
        level=getattr(logging, CFG.log_level),
        format="%(asctime)s %(levelname)-7s %(name)-9s %(message)s")
    if CFG.mode != "paper":
        raise SystemExit("CONFIG ERROR: xwin is paper-only until leg-fail "
                         "telemetry earns a live executor (runbook 2026-08-04)")
    if CFG.coin != "btc":
        raise SystemExit("CONFIG ERROR: xwin is BTC-only (15m markets exist "
                         "for btc; the backtest evidence is btc)")

    cfg5 = dataclasses.replace(CFG, window_secs=300,
                               slug_prefix="btc-updown-5m", slug_style="wts",
                               book_max_age_s=CFG.xwin_book_max_age_s)
    cfg15 = dataclasses.replace(CFG, window_secs=900,
                                slug_prefix="btc-updown-15m", slug_style="wts",
                                book_max_age_s=CFG.xwin_book_max_age_s)

    ledger = Ledger(CFG)
    clob5, clob15 = ClobFeed(cfg5), ClobFeed(cfg15)
    oracle = Oracle(CFG)                      # chainlink stream = strike truth
    risk = RiskManager(CFG, ledger, oracle, _StubSpot())
    exec5 = PaperExecutor(cfg5, clob5, ledger)
    exec15 = PaperExecutor(cfg15, clob15, ledger)
    strat = XwinStrategy(CFG, clob5, clob15, oracle, exec5, exec15, ledger, risk)

    import bot.engine.executor as _exmod
    import itertools
    _exmod._ids = itertools.count(max(ledger.max_order_id() + 1, 1))
    ledger.event("start", "mode=paper xwin btc 5mx15m")

    tasks = [
        asyncio.create_task(clob5.ws_loop(), name="clob5-ws"),
        asyncio.create_task(clob5.discover_loop(), name="discover5"),
        asyncio.create_task(clob15.ws_loop(), name="clob15-ws"),
        asyncio.create_task(clob15.discover_loop(), name="discover15"),
        asyncio.create_task(oracle.run(), name="oracle"),
        asyncio.create_task(risk.run(), name="risk"),
        asyncio.create_task(strat.run(), name="xwin"),
        asyncio.create_task(_supervised("reconciler", xwin_reconciler,
                                        CFG, clob5, clob15, ledger), name="reconciler"),
        asyncio.create_task(_supervised("status", status, ledger, strat,
                                        oracle, clob5, clob15), name="status"),
    ]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(amain())
