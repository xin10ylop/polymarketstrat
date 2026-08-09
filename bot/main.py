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
            # grace must outlive the toll's fill-polling horizon (cancel at
            # +55s): settling earlier strands late maker fills unmarked forever
            if wts in done or now < wts + cfg.window_secs + max(60, cfg.toll_cancel_after_s + 10):
                continue
            winner = await clob.fetch_outcome(mk)
            if winner is None:
                if now > wts + cfg.window_secs + cfg.outcome_patience_s:
                    done.add(wts)   # out of patience; leave fills unmarked
                    ledger.event("no_outcome", mk.slug)
                continue
            oracle_winner = toll.oracle_calls.get(wts)
            if oracle_winner is None and cfg.oracle_twap_s:
                # venue rule since 2026-08-07: rolling TWAP at BOTH ends.
                # twap_winner already returns None on thin coverage or when
                # the two boundary conventions disagree (an uncallable tie).
                n = cfg.oracle_twap_s
                C = wts + cfg.window_secs
                oracle_winner = oracle.twap_winner(
                    wts, C, n, cfg.oracle_twap_min_coverage)
                t_open, _ = oracle.twap_at(wts, n)
                t_close, _ = oracle.twap_at(C, n)
                if (oracle_winner is not None and t_open and t_close
                        and abs(t_close - t_open) / t_open < cfg.oracle_tie_bps * 1e-4):
                    ledger.event("near_tie",
                                 f"w{wts} twap_open={t_open} twap_close={t_close}")
                    oracle_winner = None
            elif oracle_winner is None:
                k_open = oracle.price_at(wts, exact=True, tolerance=2)
                k_close = oracle.price_at(wts + cfg.window_secs, exact=True)
                if k_open is not None and k_close is not None:
                    # photo-finish windows: our sample second vs the official
                    # resolution print legitimately land on opposite sides of
                    # a <2bp move (two benign SOL halts, 08-02 and 08-04).
                    # The tripwire exists to catch SYSTEMATIC misreads — a
                    # real bug also disagrees on decided windows, which still
                    # halt. Near-ties are logged, never flagged.
                    if abs(k_close - k_open) / k_open < cfg.oracle_tie_bps * 1e-4:
                        ledger.event("near_tie", f"w{wts} open={k_open} close={k_close}")
                        oracle_winner = None
                    else:
                        oracle_winner = "up" if k_close >= k_open else "down"
            if not cfg.oracle_authoritative:
                # 1h family resolves on the Binance candle, not our oracle
                # feed: a disagreement is vendor dispersion, not our bug —
                # log it for the K-frame delta record, never flag/halt.
                if oracle_winner is not None and oracle_winner != winner:
                    log.warning("w%s oracle-proxy disagrees: oracle=%s official=%s "
                                "(non-authoritative, no halt)", wts, oracle_winner, winner)
                oracle_winner = None
            mismatch = ledger.record_settlement(
                wts, winner, oracle_winner,
                token_of=lambda w, m=mk: m.token_up if w == "up" else m.token_down)
            if mismatch:
                log.error("w%s WINNER MISMATCH oracle=%s exchange=%s",
                          wts, oracle_winner, winner)
            done.add(wts)
        await asyncio.sleep(10)


async def _supervised(name, fn, *args):
    """Bookkeeping tasks must not tear the process down (audit M8): one
    unhandled exception in reconciler/healer/status restarts the TASK after
    10s instead of killing every feed and re-arming the 10-30min warmup."""
    while True:
        try:
            await fn(*args)
            log.error("task %s exited unexpectedly; restarting in 10s", name)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("task %s crashed; restarting in 10s", name)
        await asyncio.sleep(10)


async def settlement_healer(cfg, ledger):
    """Second-chance settlement: windows whose outcome was missing when the
    live reconciler gave up (Polymarket occasionally publishes results late)
    leave fills unmarked forever, understating PnL. Re-query gamma hourly for
    any unmarked window (<=7 days old) and mark late results by token id."""
    import json as _json

    import aiohttp
    tried = {}
    while True:
        await asyncio.sleep(120)
        for wts in ledger.unmarked_windows():
            if time.time() - tried.get(wts, 0) < 3600:
                continue
            tried[wts] = time.time()
            from bot.config import et_slug_ambiguous, slug_for
            if et_slug_ambiguous(cfg, wts):
                continue
            slug = slug_for(cfg, wts)
            url = f"{cfg.gamma_url}/markets?slug={slug}&closed=true"
            try:
                async with aiohttp.ClientSession(trust_env=True) as s:
                    async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                        arr = await r.json()
                if not arr:
                    continue
                m = arr[0]
                op = m.get("outcomePrices")
                prices = _json.loads(op) if isinstance(op, str) else op
                if not prices or float(max(prices, key=float)) != 1.0:
                    continue
                outcomes = m.get("outcomes")
                outcomes = _json.loads(outcomes) if isinstance(outcomes, str) else outcomes
                toks = m.get("clobTokenIds")
                toks = _json.loads(toks) if isinstance(toks, str) else toks
                idx = [float(p) for p in prices].index(1.0)
                winner = outcomes[idx].lower()
                n = ledger.mark_window_by_token(wts, toks[idx], winner)
                ledger.event("late_settlement", f"w{wts} {winner} ({n} fills)")
                log.warning("late settlement healed w%s: %s (%d fills marked)",
                            wts, winner, n)
            except Exception as e:  # noqa: BLE001
                log.debug("healer %s: %s", slug, e)
        await asyncio.sleep(3600)


async def status(cfg, ledger, oracle, spot, clob, toll, snipe):
    while True:
        await asyncio.sleep(cfg.status_every_s)
        s = ledger.summary()
        log.info("STATUS pnl_today=%.2f oracle=%.2f(%s, %.1fs) spot=%.2f basis=%s "
                 "markets=%d clip=%.0f pre[on=%d placed=%d ok=%d wrong=%d mcxl=%d gcxl=%d] "
                 "snipe[evals=%d nodata=%d thingap=%d near=%d nm=%s gate=%d sig=%d lastfv=%s] fills=%s",
                 ledger.realized_pnl_today(),
                 oracle.last_price or 0, "DEGRADED" if oracle.degraded else "ok",
                 oracle.staleness(), spot.last_price or 0,
                 f"{spot.basis():.6f}" if spot.basis() else "n/a",
                 len(clob.markets), toll.clip,
                 1 if cfg.toll_pre_position else 0, toll.pre_placed, toll.pre_correct,
                 toll.pre_wrong, toll.pre_marginal_cancel, toll.pre_guard_cancel,
                 snipe.evals, snipe.no_data, getattr(snipe, 'thin_gap', 0),
                 snipe.near_misses, getattr(snipe, 'nm', {}),
                 snipe.recheck_fail,
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

    kfeed = None
    if CFG.kalshi_telemetry:
        if CFG.family == "15m" and CFG.coin in ("btc", "eth"):
            from bot.feeds.kalshi import KalshiFeed
            kfeed = KalshiFeed(CFG)
            snipe.kalshi = kfeed     # telemetry-only: read in _depth_event
        else:
            log.warning("KALSHI_TELEMETRY ignored: btc/eth 15m families only")

    if CFG.slug_style == "et_hourly" and CFG.window_secs != 3600:
        raise SystemExit("CONFIG ERROR: SLUG_STYLE=et_hourly requires WINDOW_SECS=3600 "
                         "(endDate coincidences would bind wrong markets)")
    if CFG.mode == "live" and ledger.db.execute(
            "SELECT COUNT(*) FROM orders WHERE mode LIKE 'paper%'").fetchone()[0]:
        raise SystemExit("CONFIG ERROR: live mode on a ledger containing paper fills "
                         "— risk breakers would read paper PnL. Use a fresh BOT_DATA_DIR.")
    import bot.engine.executor as _exmod
    _exmod._ids = __import__("itertools").count(max(ledger.max_order_id() + 1, 1))
    ledger.event("start", f"mode={CFG.mode} spot={CFG.spot_feed}")
    tasks = [
        asyncio.create_task(clob.ws_loop(), name="clob-ws"),
        asyncio.create_task(clob.discover_loop(), name="discover"),
        asyncio.create_task(spot.run(), name="spot"),
        asyncio.create_task(oracle.run(), name="oracle"),
        asyncio.create_task(risk.run(), name="risk"),
        asyncio.create_task(toll.run(), name="toll"),
        asyncio.create_task(snipe.run(), name="snipe"),
        asyncio.create_task(_supervised("reconciler", reconciler, CFG, clob, ledger, toll, oracle), name="reconciler"),
        asyncio.create_task(_supervised("healer", settlement_healer, CFG, ledger), name="healer"),
        asyncio.create_task(_supervised("status", status, CFG, ledger, oracle, spot, clob, toll, snipe), name="status"),
    ]
    if kfeed is not None:
        tasks.append(asyncio.create_task(_supervised("kalshi", kfeed.run), name="kalshi"))
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
