"""Paper/live trading bot for Polymarket BTC 5m Up/Down.

    python3 -m bot.main            # paper mode (default; simulates on live data)
    BOT_MODE=live python3 -m bot.main   # refuses until LiveExecutor guard is lifted

Everything runs from live exchange data; PAPER differs from LIVE only in the
executor. See bot/README.md for deployment.
"""
import asyncio
import logging
import signal
import time

from bot.config import CFG
from bot.engine.executor import PaperExecutor
from bot.engine.ledger import Ledger
from bot.engine.risk import RiskManager
from bot.feeds.clob import ClobFeed
from bot.feeds.oracle import Oracle
from bot.feeds.spot import SpotFeed
from bot.strategies.snipe import SnipeStrategy
from bot.strategies.toll import TollStrategy

log = logging.getLogger("main")


async def reconciler(cfg, clob, ledger, toll, oracle):
    """Post-settlement truth: fetch each window's official outcome from gamma,
    mark PnL, and compare with OUR OWN oracle read of the window. The oracle
    winner is computed here directly from the resolution feed (not via the
    toll strategy) so the mismatch tripwire works even when the toll is
    disabled — which is exactly the live configuration (audit 2026-07-30 #6)."""
    done = set()
    while True:
        now = time.time()
        for wts, mk in list(clob.markets.items()):
            if wts in done or now < wts + cfg.window_secs + 40:
                continue
            winner = await clob.fetch_outcome(mk)
            if winner is None:
                if now > wts + cfg.window_secs + 600:
                    done.add(wts)   # give up after 10 min; leave fills unmarked
                    ledger.event("no_outcome", mk.slug)
                continue
            oracle_winner = toll.oracle_calls.get(wts)
            if oracle_winner is None:
                k_open = oracle.price_at(wts, exact=True, tolerance=2)
                k_close = oracle.price_at(wts + cfg.window_secs, exact=True)
                if k_open is not None and k_close is not None:
                    oracle_winner = "up" if k_close >= k_open else "down"
            mismatch = ledger.record_settlement(
                wts, winner, oracle_winner,
                token_of=lambda w, m=mk: m.token_up if w == "up" else m.token_down)
            if mismatch:
                log.error("w%s WINNER MISMATCH oracle=%s exchange=%s",
                          wts, oracle_winner, winner)
            done.add(wts)
        await asyncio.sleep(10)


async def status(cfg, ledger, oracle, spot, clob, toll, snipe):
    while True:
        await asyncio.sleep(cfg.status_every_s)
        s = ledger.summary()
        log.info("STATUS pnl_today=%.2f oracle=%.2f(%s, %.1fs) spot=%.2f basis=%s "
                 "markets=%d clip=%.0f pre[on=%d placed=%d ok=%d wrong=%d mcxl=%d gcxl=%d] "
                 "snipe[evals=%d nodata=%d near=%d gate=%d sig=%d lastfv=%s] fills=%s",
                 ledger.realized_pnl_today(),
                 oracle.last_price or 0, "DEGRADED" if oracle.degraded else "ok",
                 oracle.staleness(), spot.last_price or 0,
                 f"{spot.basis():.6f}" if spot.basis() else "n/a",
                 len(clob.markets), toll.clip,
                 1 if cfg.toll_pre_position else 0, toll.pre_placed, toll.pre_correct,
                 toll.pre_wrong, toll.pre_marginal_cancel, toll.pre_guard_cancel,
                 snipe.evals, snipe.no_data, snipe.near_misses, snipe.recheck_fail,
                 snipe.signals,
                 f"{snipe.last_fv:.4f}" if snipe.last_fv is not None else "n/a", s)


async def amain():
    logging.basicConfig(
        level=getattr(logging, CFG.log_level),
        format="%(asctime)s %(levelname)-7s %(name)-7s %(message)s")
    log.info("starting bot mode=%s spot=%s family=%s", CFG.mode, CFG.spot_feed, CFG.family)

    ledger = Ledger(CFG)
    clob = ClobFeed(CFG)
    spot = SpotFeed(CFG)
    oracle = Oracle(CFG, spot=spot)
    spot.oracle_ref = oracle
    risk = RiskManager(CFG, ledger, oracle, spot)
    reconciler_task = prewarm_task = None
    if CFG.mode == "live":
        from bot.engine.live import (Bankroll, LiveExecutor, balance_reconciler,
                                     prewarm_loop)
        bankroll = Bankroll(CFG)
        CFG.max_daily_loss = bankroll.daily_stop     # risk breaker scales with bankroll
        executor = LiveExecutor(CFG, clob, ledger, bankroll, risk)
        reconciler_task = balance_reconciler(CFG, ledger, executor, risk)
        prewarm_task = prewarm_loop(clob, executor)
    else:
        executor = PaperExecutor(CFG, clob, ledger)
    toll = TollStrategy(CFG, clob, oracle, spot, executor, ledger, risk)
    snipe = SnipeStrategy(CFG, clob, oracle, spot, executor, ledger, risk)

    ledger.event("start", f"mode={CFG.mode} spot={CFG.spot_feed}")
    tasks = [
        asyncio.create_task(clob.ws_loop(), name="clob-ws"),
        asyncio.create_task(clob.discover_loop(), name="discover"),
        asyncio.create_task(spot.run(), name="spot"),
        asyncio.create_task(oracle.run(), name="oracle"),
        asyncio.create_task(risk.run(), name="risk"),
        asyncio.create_task(toll.run(), name="toll"),
        asyncio.create_task(snipe.run(), name="snipe"),
        asyncio.create_task(reconciler(CFG, clob, ledger, toll, oracle), name="reconciler"),
        asyncio.create_task(status(CFG, ledger, oracle, spot, clob, toll, snipe), name="status"),
    ]
    if reconciler_task is not None:
        tasks.append(asyncio.create_task(reconciler_task, name="balance-reconciler"))
    if prewarm_task is not None:
        tasks.append(asyncio.create_task(prewarm_task, name="prewarm"))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    done, _ = await asyncio.wait(
        [asyncio.create_task(stop.wait()), *tasks],
        return_when=asyncio.FIRST_COMPLETED)
    for t in done:
        if t.exception():
            log.error("task died: %s", t.exception())
    log.info("shutting down")
    for t in tasks:
        t.cancel()
    ledger.event("stop")


if __name__ == "__main__":
    asyncio.run(amain())
