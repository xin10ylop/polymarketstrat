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
from bot.engine.executor import LiveExecutor, PaperExecutor
from bot.engine.ledger import Ledger
from bot.engine.risk import RiskManager
from bot.feeds.clob import ClobFeed
from bot.feeds.oracle import Oracle
from bot.feeds.spot import SpotFeed
from bot.strategies.snipe import SnipeStrategy
from bot.strategies.toll import TollStrategy

log = logging.getLogger("main")


async def reconciler(cfg, clob, ledger, toll):
    """Post-settlement truth: fetch each window's official outcome from gamma,
    mark PnL, and compare with the toll's own oracle call."""
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
            mismatch = ledger.record_settlement(
                wts, winner, oracle_winner,
                token_of=lambda w, m=mk: m.token_up if w == "up" else m.token_down)
            if mismatch:
                log.error("w%s WINNER MISMATCH oracle=%s exchange=%s",
                          wts, oracle_winner, winner)
            done.add(wts)
        await asyncio.sleep(10)


async def status(cfg, ledger, oracle, spot, clob, toll):
    while True:
        await asyncio.sleep(cfg.status_every_s)
        s = ledger.summary()
        log.info("STATUS pnl_today=%.2f oracle=%.2f(%s, %.1fs) spot=%.2f basis=%s "
                 "markets=%d clip=%.0f fills=%s",
                 ledger.realized_pnl_today(),
                 oracle.last_price or 0, "DEGRADED" if oracle.degraded else "ok",
                 oracle.staleness(), spot.last_price or 0,
                 f"{spot.basis():.6f}" if spot.basis() else "n/a",
                 len(clob.markets), toll.clip, s)


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
    if CFG.mode == "live":
        executor = LiveExecutor(CFG, clob, ledger)   # fails closed by design
    else:
        executor = PaperExecutor(CFG, clob, ledger)
    risk = RiskManager(CFG, ledger, oracle, spot)
    toll = TollStrategy(CFG, clob, oracle, executor, ledger, risk)
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
        asyncio.create_task(reconciler(CFG, clob, ledger, toll), name="reconciler"),
        asyncio.create_task(status(CFG, ledger, oracle, spot, clob, toll), name="status"),
    ]
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
