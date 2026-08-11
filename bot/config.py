"""Central configuration. Every live/paper knob lives here; env vars override.

The paper bot and the live bot run the SAME code path — only the executor
differs (PaperExecutor simulates fills from the live tape; LiveExecutor signs
real orders). Anything you would tune with real money is a field here.
"""
import os
from dataclasses import dataclass, field


def _env(name, default, cast=str):
    v = os.environ.get(name)
    return cast(v) if v is not None else default


_COIN = os.environ.get("COIN", "btc").lower()   # btc | eth | sol | xrp | doge


@dataclass
class Config:
    mode: str = _env("BOT_MODE", "paper")            # paper | live
    coin: str = _COIN
    family: str = _env("FAMILY", "5m")               # 5m | 15m | 1h
    window_secs: int = _env("WINDOW_SECS", 300, int)  # 900 for the 15m family
    slug_prefix: str = _env("SLUG_PREFIX", f"{_COIN}-updown-5m")
    # wts: <prefix>-<window start epoch> (5m/15m families)
    # et_hourly: <prefix>-<month>-<day>-<year>-<h>{am,pm}-et (1h family)
    slug_style: str = _env("SLUG_STYLE", "wts")
    # Whether the oracle feed is the market's official resolution source.
    # True for 5m/15m (Chainlink stream is named in resolutionSource; any
    # disagreement with gamma = OUR bug = sticky halt). The 1h family
    # resolves on the Binance BTC/USDT candle instead, so there the oracle
    # is only a signal proxy: disagreements are logged, never halted on.
    oracle_authoritative: bool = _env("ORACLE_AUTHORITATIVE", "1") == "1"

    # --- endpoints ---
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    clob_ws: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    coinbase_ws: str = "wss://ws-feed.exchange.coinbase.com"
    coinbase_product: str = _env("COINBASE_PRODUCT", f"{_COIN.upper()}-USD")
    binance_ws: str = _env("BINANCE_WS",
                           f"wss://stream.binance.com:9443/ws/{_COIN}usdt@aggTrade")
    spot_feed: str = _env("SPOT_FEED", "coinbase")   # coinbase | binance (binance is US-geoblocked)
    # Resolution feed: Polymarket's published Chainlink data stream for this
    # coin — the exact feed named in each market's resolutionSource. NEVER the
    # on-chain aggregator (33s rounds -> ~8.6% miscalled windows historically).
    pm_live_ws: str = "wss://ws-live-data.polymarket.com"
    pm_price_symbol: str = _env("PM_PRICE_SYMBOL", f"{_COIN}/usd")

    # --- fees (verified May-Jul 2026: taker 0.07*p*(1-p), maker 0). CLOB metadata
    # now shows base_fee=1000 for both sides; until a real fill proves otherwise we
    # model the verified curve and log metadata drift at startup. ---
    taker_fee_mult: float = _env("TAKER_FEE_MULT", 0.07, float)
    maker_fee_mult: float = _env("MAKER_FEE_MULT", 0.0, float)

    # --- toll strategy (S3): post-close bid on the determined winner ---
    toll_enabled: bool = _env("TOLL_ENABLED", "1") == "1"
    toll_place_delay_s: float = 0.2        # start trying this soon after close; the
                                           # order goes out the moment the close print
                                           # arrives (~T+0.5-1.2s) — every 100ms earlier
                                           # is queue position ahead of slower bots
    toll_cancel_after_s: float = 55.0      # settlement median ~19s but p90 ~52s; the
                                           # late tail keeps dumping and costs nothing to wait for
    toll_boundary_wait_s: float = 4.0      # give the close sample up to this long to arrive
    # skip windows decided by less than this: exact-boundary sampling was
    # historically 100.000% correct even at $0, but tiny-margin windows carry
    # all of the residual feed-hiccup risk for ~0.8c of upside
    toll_min_margin_usd: float = _env("TOLL_MIN_MARGIN_USD", 10.0, float)
    toll_max_window_loss: float = _env("TOLL_MAX_WINDOW_LOSS", 250.0, float)  # $ cap per window
    # --- toll Tier 2 (S3b): pre-position ahead of close for queue priority ---
    # The competitor wall forms EARLY (median ~2k shares resting by T-10s, ~12k
    # by T-2s, ~84k at close): meaningful capture requires arriving ~10s out.
    # A wrong-side resting bid loses ~size*0.99, ~125x the per-window gain, so
    # margins are set where 12,106 historical windows produced ZERO wrong calls:
    # stages tried in order as (lead_seconds, margin_floor_usd, sigma).
    toll_pre_position: bool = _env("TOLL_PREPOSITION", "0") == "1"
    toll_pre_stages: tuple = ((10.0, 80.0, 8.0), (5.0, 60.0, 7.0),
                              (3.0, 60.0, 6.0), (1.2, 40.0, 6.0))
    # guard: after pre-placing, cancel instantly if the predicted margin decays
    # below max(this floor, half the entry threshold) or flips sign
    toll_pre_guard_floor: float = 30.0
    toll_price_fine: float = 0.992         # when 0.001 tick regime is active
    toll_price_coarse: float = 0.99        # when tick regime is 0.01
    toll_min_clip: int = 200               # shares; under wall-aware fills most windows
                                           # fill ~0 regardless of size, so the controller
                                           # must not shrink the clip below tail-capture size
    toll_max_clip: int = 1000
    toll_start_clip: int = 250
    toll_float_budget: float = _env("TOLL_FLOAT", 1000.0, float)  # max $ parked per window
    # feedback controller: raise clip while marginal fills keep coming, shrink on misses
    toll_target_fill_share: float = 0.6    # aim to fill >=60% of posted size
    toll_controller_alpha: float = 0.2     # EMA step per window

    # --- snipe strategy (S2): basis-corrected terminal taker ---
    snipe_enabled: bool = _env("SNIPE_ENABLED", "1") == "1"
    snipe_eval_from_s: float = -6.0        # start evaluating at T-6s
    snipe_poll_s: float = 0.05             # eval cadence in the final seconds: the median
                                           # qualifying ask survives ~142ms, so every 50ms
                                           # of reaction time is fill share in the race
    # PAPER-ONLY live-fidelity gate (ignored when mode=live): after a signal,
    # wait this long and require the ask to still be there before "filling" —
    # simulates network latency + Polymarket's 250ms marketable-order hold.
    # ~82% of paper's instant fills fail this test (audited); with it ON, paper
    # P&L ~= what real money would capture. Live fires immediately: its latency
    # is real, and an artificial pre-send wait would forfeit the race. 0 = off.
    snipe_take_recheck_s: float = _env("SNIPE_TAKE_RECHECK_S", 0.5, float)
    snipe_signal_lag_s: float = 1.0        # act on spot data at least 1s old (validated latency)
    # Sparse-tape guards (audit F1/F3, SOL): refuse bars older than this,
    # and require the barrier to exceed N spot ticks — below the input
    # resolution, a 99.5% fv is model confidence, not market information.
    # 5.0, verified by two independent audits (07-31): with tau computed FROM
    # the bar's own second, every admitted signal is a STRICT SUBSET of the
    # config that earned the profitable record (older bar -> longer horizon ->
    # less extreme fv, self-limiting), and bar mechanics bake in ~1-2s of
    # phantom age (second-truncated stamps + completion-on-next-trade). An
    # earlier "~40% of evals" justification was WRONG (warmup polls + CLOB
    # book staleness misattributed) — see runbook 07-31. SOL unit overrides
    # to 3.0 (sparse tape, no record of its own to anchor the subset proof).
    spot_max_bar_age_s: float = _env("SPOT_MAX_BAR_AGE_S", 5.0, float)
    spot_tick: float = _env("SPOT_TICK", 0.01, float)
    snipe_min_ticks: float = _env("SNIPE_MIN_TICKS", 3.0, float)
    snipe_fv_min: float = _env("SNIPE_FV_MIN", 0.995, float)
    snipe_ask_max: float = 0.97
    snipe_min_ask_size: float = 12.0
    # Hard cap. 250 default from 5m evidence (EV collapse above: adverse
    # selection). 15m/1h tapes show the OPPOSITE (sz>250 entries ev>=+5c,
    # cap binds ~11% of entries) — slower-family paper bots run 500 to
    # measure the fatter cap during their gates.
    snipe_max_clip: int = _env("SNIPE_MAX_CLIP", 250, int)
    snipe_skip_ask_above: float = _env("SNIPE_SKIP_ASK_ABOVE", 500.0, float)
    # ^ giant late asks are informed (two audits: >=250-share bucket -2.4c/sh,
    #   first-shot $/day falls with size); refuse to engage walls of offers
    snipe_first_clip: int = _env("SNIPE_FIRST_CLIP", 100, int)
    # ^ small first bite: a fresh big ask is adversely selected; an ask that
    #   SURVIVES a first take is proven stale — retries size up to the full clip
    snipe_price_floor: float = _env("SNIPE_PRICE_FLOOR", 0.0, float)

    # ---- pre-open entry (bot/strategies/preopen.py) -------------------
    # Buy the tilt side while the book is still flat, seconds before the
    # window opens. Measured: sign accuracy at T-3 is 98.9% on windows over
    # 1bp and only 83% at T-10, so the lead is 3 and not 10.
    preopen_enabled: bool = _env("PREOPEN_ENABLED", 0, int) == 1
    preopen_lead_s: float = _env("PREOPEN_LEAD_S", 3.0, float)
    preopen_tilt_min_bp: float = _env("PREOPEN_TILT_MIN_BP", 1.0, float)
    # refuse if the book already leans: at a flat book both sides sit near
    # 0.50, so anything above this means the makers priced the tilt first
    preopen_max_px: float = _env("PREOPEN_MAX_PX", 0.56, float)
    preopen_clip: int = _env("PREOPEN_CLIP", 250, int)
    preopen_exit_c: float = _env("PREOPEN_EXIT_C", 0.05, float)
    preopen_mark_s: float = _env("PREOPEN_MARK_S", 15.0, float)
    # The strike's own coverage floor, SEPARATE from oracle_twap_min_coverage
    # because that one gates settlement and the snipe and must not move.
    # 0.9 was a guess and it refused 23.1% of btc windows (24.9% eth). Punching
    # real hole patterns into complete windows, the picked side is unchanged
    # from the complete-grid call in 84/84 btc and 47/47 eth cases at 21-24 of
    # 27 seconds — the exact region 0.9 was refusing. 0.75 recovers 96 of those
    # 100 btc windows while still refusing every feed blackout (n_present=0,
    # 6.5% of btc windows and 10.1% of eth) and leaving the two thinnest
    # buckets, where the evidence is n=1, out of scope.
    preopen_min_coverage: float = _env("PREOPEN_MIN_COVERAGE", 0.75, float)
    # A RESTING LIMIT SELL FILLS ON A TOUCH. Sampling the book at three fixed
    # instants answers "was the bid above target at this moment", which is a
    # strictly harder question and understates the fill rate by every touch
    # in between. These drive continuous tracking off the CLOB websocket the
    # bot already holds, so the cost is zero extra requests.
    # THE FLOOR ON HOW LATE AN ENTRY MAY BE. The loop used to fire only inside
    # a 0.6s slot at T-lead and silently dropped any window where the event
    # loop was busy at that instant — 22% of btc 5m windows and 35% of eth's
    # over 19 hours, with no log line of any kind, while both 15m bots hit
    # 101% and 104%. It now fires any time between the target lead and this
    # floor, which is the time an order still needs to reach the book before
    # the open.
    preopen_min_lead_s: float = _env("PREOPEN_MIN_LEAD_S", 0.5, float)
    preopen_track_s: float = _env("PREOPEN_TRACK_S", 90.0, float)
    preopen_track_levels: tuple = tuple(
        float(x) for x in _env(
            "PREOPEN_TRACK_LEVELS", "0.01,0.02,0.03,0.04,0.05,0.07,0.10"
        ).split(","))
    # ^ 0 = off. Jun-Jul reconstruction says deep (<=0.80) asks decayed to -EV,
    #   but live paper fills there still print +EV — let the paper ledger
    #   referee; flip to 0.90 if a week of deep fills bleeds (runbook item)
    snipe_eval_until_s: float = _env("SNIPE_EVAL_UNTIL_S", -1.5, float)
    # no trades until the process is this old AND estimators are mature.
    # Empirical: fills at 3-10min uptime went 0/4 while >10min steady-state
    # runs 81% win — the 300s vol window reads distorted until ~2x its
    # length has elapsed. Restarts are rare (deploys only), so the cost of
    # a long warmup is ~2 skipped windows per deploy.
    snipe_warmup_s: float = _env("SNIPE_WARMUP_S", 600.0, float)
    # ^ stop evaluating this close to the bell: the final second is sharply
    #   -EV (-5.6c/sh) — late cheap offers know the last tick
    # retry/replace: keep re-firing while the signal persists, until the window
    # budget is spent — a missed ask (someone beat us) costs nothing; the next
    # stale ask in the same window is a fresh chance. Marginal EV of a 3rd
    # repeat is ~zero and a lost window loses on EVERY attempt: cap at 3.
    snipe_max_attempts: int = _env("SNIPE_MAX_ATTEMPTS", 3, int)
    snipe_window_max_cost: float = _env("SNIPE_WINDOW_MAX_COST", 300.0, float)
    snipe_vol_floor: float = 1e-6
    basis_window_s: int = 60               # rolling median window for oracle/spot basis
    vol_window_s: int = 300                # realized vol estimator window
    # Max book age before _ask_ok refuses to trade on it. 3s suits 5m markets
    # (books tick constantly near the close). On the 1h family the winning
    # token often goes QUIET for tens of seconds — an unchanged book on a live
    # ws IS current (deltas remove taken levels), so the 1h unit widens this
    # (diagnosed 2026-08-02: stale_book was 57% of its rejections, blinding it
    # to the ~1/day opportunities the tape measured).
    book_max_age_s: float = _env("BOOK_MAX_AGE_S", 3.0, float)

    # --- risk / kill-switches ---
    max_daily_loss: float = _env("MAX_DAILY_LOSS", 250.0, float)  # $ paper, halt for the day
    # IN PAPER THE DAILY STOP IS RECORDED, NOT ENFORCED. A paper bot is a
    # measurement instrument with no capital to preserve, and halting it on
    # bad days censors the sample in exactly one direction — losing runs cut
    # off at the breaker, winning runs recorded in full — which biases every
    # win rate and every drawdown optimistically. Found 2026-08-11: btc 5m
    # was dark 5h and eth 5m 6h47m, and nothing in the ledgers said so.
    # An uncensored record can always be censored in analysis; a censored one
    # can never be repaired, so the default records. Set 0 to enforce.
    # IGNORED WHEN mode=live, where the stop is sticky and absolute.
    paper_shadow_daily_stop: bool = _env(
        "PAPER_SHADOW_DAILY_STOP", "1",
        str).strip().lower() not in ("0", "false", "no", "off", "")
    snipe_trailing_n: int = 30             # settled fills in the trailing window
    # Fast bleed tripwire, sized to the day's budget so it scales with bankroll
    # (paper $250 -> -$200; live 20%-of-bankroll -> -16%). A FIXED $ threshold
    # is wrong: single snipe losses are $85-210, so a trailing window routinely
    # dips tens of dollars on pure variance — a tight fixed value false-halts a
    # healthy +EV strategy. This fires only when a trailing window has lost most
    # of a full day's budget, i.e. a genuine bleed, not noise.
    snipe_trailing_pnl_frac: float = _env("SNIPE_TRAILING_PNL_FRAC", 0.8, float)
    # after a trailing halt, the breaker re-arms only once this many NEW fills
    # have settled — otherwise a restart re-reads the same 30 ledger fills and
    # re-halts instantly (deadlock: can't dilute the window while halted).
    # A restart = the human chose to resume; judge the NEW trading.
    snipe_trailing_rearm_fills: int = _env("SNIPE_TRAILING_REARM", 10, int)
    max_unmarked_fills: int = 5            # halt if this many old fills lack settlement
    # Windows finishing within this margin are photo-finishes: the oracle
    # cross-check is skipped (logged as near_tie) instead of arming the
    # mismatch halt — sampling dispersion, not a bug signal. Disagreements
    # on windows decided by more than this still halt everything.
    # 2.0 was sized for spot prints. Against a reconstructed TWAP the
    # measurement error is ~0.05bp, and a 2bp band switched the mismatch
    # tripwire off on 34-71% of windows (audit D10). Tighter for the TWAP
    # families; the 1h family keeps the old band via its own env override.
    # 2026-08-11: 0.3 WAS BELOW OUR OWN MEASUREMENT ERROR, so the tripwire
    # fired on our arithmetic and halted the fleet. The "~0.05bp" above was an
    # estimate, never measured. Measured on 809 settled windows across btc and
    # eth: every one of the 9 disagreements lies within 0.421bp of a tie, and
    # ZERO occur on windows decided by more than 1bp. Estimator and boundary
    # alignment were both ruled out first (mismatch_audit, twap_align), so
    # what is left is the residual of approximating a published TWAP STREAM
    # with a uniform mean of 1s samples — structural, and not removable
    # without subscribing to the stream.
    #   btc clean at 0.4bp (11.3% of windows blinded), eth at 0.5bp (9.6%).
    #   0.6 is one notch above the stricter of the two: it costs ~1 extra
    #   point of blindness and buys headroom, because a band sitting AT the
    #   observed maximum re-trips on the next slightly fatter residual.
    # Above the band the tripwire reads 100% on all 809 windows, so nothing
    # is given up in sensitivity to REAL defects — there are none up there.
    # 15m IS NOT MEASURED and stays at 0.3 until it has its own sweep; a 60s
    # mean should have a smaller residual, but should is not measured.
    oracle_tie_bps: float = _env(
        "ORACLE_TIE_BPS",
        {"5m": 0.6, "15m": 0.3}.get(_env("FAMILY", "5m"), 2.0), float)
    # How long past window close the reconciler keeps polling gamma for the
    # official outcome before giving up (no_outcome). 5m/15m publish well
    # inside 10 min, but the 1h family resolves via UMA proposal ~11-13 min
    # after the hour (measured closedTime 17:12:30 / 20:11:17 for 17:00 /
    # 20:00 closes, 2026-08-02) — 600s missed EVERY hourly outcome. The 1h
    # unit sets 1800.
    outcome_patience_s: float = _env("OUTCOME_PATIENCE_S", 600.0, float)
    # Kalshi price telemetry (btc/eth 15m only): log Kalshi's concurrent
    # same-window price on every take attempt. TELEMETRY ONLY — no behavior
    # change, no Kalshi trading. Runbook 2026-08-02 addendum has the evidence
    # and the staged plan (veto only after measurement on our own fills).
    kalshi_telemetry: bool = _env("KALSHI_TELEMETRY", "0") == "1"

    # --- xwin: shared-close 5m x 15m structural scanner (paper-only, btc) ---
    # Audited backtest (runbook 2026-08-04): conservative tier $8-20/day after
    # latency+competition; deep tier log-only until leg-fail telemetry.
    xwin_tau_min: float = _env("XWIN_TAU_MIN", 30.0, float)
    xwin_tau_max: float = _env("XWIN_TAU_MAX", 180.0, float)
    xwin_min_cost: float = _env("XWIN_MIN_COST", 0.90, float)
    xwin_max_cost: float = _env("XWIN_MAX_COST", 0.99, float)
    xwin_clip: float = _env("XWIN_CLIP", 250.0, float)
    xwin_min_size: float = _env("XWIN_MIN_SIZE", 20.0, float)
    xwin_book_max_age_s: float = _env("XWIN_BOOK_MAX_AGE_S", 10.0, float)
    xwin_survival_s: float = 0.5        # paper-only entry survival gate
    xwin_leg_retry_s: float = _env("XWIN_LEG_RETRY_S", 2.0, float)
    xwin_max_chase: float = _env("XWIN_MAX_CHASE", 0.02, float)
    oracle_max_staleness_s: float = 5.0    # oracle feed silence -> degraded, no trading
    feed_max_silence_s: float = 10.0       # spot feed silence pauses the snipe
    # --- resolution rule (CHANGED BY THE VENUE 2026-08-07 00:00 UTC) ---
    # 5m/15m now settle on a rolling Chainlink TWAP: "TWAP at close >= TWAP
    # at open". Length scales with the window (30s / 60s). 0 = the old
    # spot-vs-spot rule, which still governs the 1h family (Binance/UMA).
    oracle_twap_s: int = _env(
        "ORACLE_TWAP_S",
        {"5m": 30, "15m": 60}.get(_env("FAMILY", "5m"), 0), int)
    # a TWAP averaged over a gappy grid is a different number: refuse below
    # this share of the seconds (the recorder saw 89-93% density on a box
    # that was ALSO running seven bots; the bot's own feed backfills)
    oracle_twap_min_coverage: float = _env("ORACLE_TWAP_MIN_COVERAGE", 0.9, float)

    # --- ops ---
    data_dir: str = _env("BOT_DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
    log_level: str = _env("LOG_LEVEL", "INFO")
    discovery_lookahead: int = 3           # subscribe this many upcoming windows
    status_every_s: int = 60

    # --- live trading (provision via EnvironmentFile on the trading server only;
    # three locks: mode=live+BANKROLL, LIVE_CONFIRM typed by a human, LIVE_SHADOW=0) ---
    live_shadow: bool = _env("LIVE_SHADOW", "1") == "1"       # DEFAULT ON: no real orders
    live_confirm: str = _env("LIVE_CONFIRM", "")
    live_strategies: tuple = tuple(
        s.strip() for s in _env("LIVE_STRATEGIES", "snipe").split(",") if s.strip())
    bankroll: float = _env("BANKROLL", 0.0, float)            # dollars; required in live
    live_per_trade_frac: float = _env("LIVE_PER_TRADE_FRAC", 0.10, float)
    live_daily_stop_frac: float = _env("LIVE_DAILY_STOP_FRAC", 0.20, float)
    # cumulative (lifetime) live drawdown -> sticky halt; catches a dead edge
    # bleeding one daily-stop at a time, which the daily/trailing breakers
    # individually never see (audit 2026-07-30 finding #7)
    live_max_drawdown_frac: float = _env("LIVE_MAX_DRAWDOWN_FRAC", 0.5, float)
    live_max_trades_day: int = _env("LIVE_MAX_TRADES_DAY", 40, int)
    pm_signature_type: int = _env("PM_SIGNATURE_TYPE", 1, int)  # 1=email/Magic, 2=browser proxy
    pm_private_key: str = _env("PM_PRIVATE_KEY", "")
    pm_api_key: str = _env("PM_API_KEY", "")
    pm_api_secret: str = _env("PM_API_SECRET", "")
    pm_api_passphrase: str = _env("PM_API_PASSPHRASE", "")
    pm_funder: str = _env("PM_FUNDER", "")


def slug_for(cfg, wts: int) -> str:
    if cfg.slug_style == "et_hourly":
        from datetime import datetime
        from zoneinfo import ZoneInfo
        t = datetime.fromtimestamp(wts, tz=ZoneInfo("America/New_York"))
        hr = t.strftime("%I%p").lstrip("0").lower()
        return f"{cfg.slug_prefix}-{t.strftime('%B').lower()}-{t.day}-{t.year}-{hr}-et"
    return f"{cfg.slug_prefix}-{wts}"


def et_slug_ambiguous(cfg, wts: int) -> bool:
    """DST fall-back: the repeated ET hour makes two epoch-hours share one
    slug (e.g. Nov 1 2026, 05:00Z and 06:00Z are both '1am-et'). Binding
    either risks the wrong strike and settlement — skip both (2 windows/yr)."""
    if cfg.slug_style != "et_hourly":
        return False
    s = slug_for(cfg, wts)
    return s == slug_for(cfg, wts - 3600) or s == slug_for(cfg, wts + 3600)


CFG = Config()
