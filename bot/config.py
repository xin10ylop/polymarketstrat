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


@dataclass
class Config:
    mode: str = _env("BOT_MODE", "paper")            # paper | live
    family: str = "5m"
    window_secs: int = 300
    slug_prefix: str = "btc-updown-5m"

    # --- endpoints ---
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    clob_ws: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    coinbase_ws: str = "wss://ws-feed.exchange.coinbase.com"
    binance_ws: str = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
    spot_feed: str = _env("SPOT_FEED", "coinbase")   # coinbase | binance (binance is US-geoblocked)
    # Resolution feed: Polymarket's published Chainlink BTC/USD data stream —
    # the exact feed named in each market's resolutionSource. NEVER the on-chain
    # Polygon aggregator (33s rounds -> ~8.6% miscalled windows historically).
    pm_live_ws: str = "wss://ws-live-data.polymarket.com"
    pm_price_symbol: str = "btc/usd"

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
    snipe_signal_lag_s: float = 1.0        # act on spot data at least 1s old (validated latency)
    snipe_fv_min: float = 0.995
    snipe_ask_max: float = 0.97
    snipe_min_ask_size: float = 12.0
    snipe_max_clip: int = 250              # hard cap: EV collapses above (adverse selection)
    snipe_vol_floor: float = 1e-6
    basis_window_s: int = 60               # rolling median window for oracle/spot basis
    vol_window_s: int = 300                # realized vol estimator window
    book_max_age_s: float = 3.0            # never trust a book older than this

    # --- risk / kill-switches ---
    max_daily_loss: float = _env("MAX_DAILY_LOSS", 25.0, float)   # $ paper, halt for the day
    snipe_trailing_n: int = 30             # settled fills in the trailing window
    snipe_trailing_pnl_min: float = -8.0   # halt snipe if trailing-N pnl below this ($)
    max_unmarked_fills: int = 5            # halt if this many old fills lack settlement
    oracle_max_staleness_s: float = 5.0    # oracle feed silence -> degraded, no trading
    feed_max_silence_s: float = 10.0       # spot feed silence pauses the snipe

    # --- ops ---
    data_dir: str = _env("BOT_DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
    log_level: str = _env("LOG_LEVEL", "INFO")
    discovery_lookahead: int = 3           # subscribe this many upcoming windows
    status_every_s: int = 60

    # --- live trading (unused in paper mode; provision via env, never commit) ---
    pm_private_key: str = _env("PM_PRIVATE_KEY", "")
    pm_api_key: str = _env("PM_API_KEY", "")
    pm_api_secret: str = _env("PM_API_SECRET", "")
    pm_api_passphrase: str = _env("PM_API_PASSPHRASE", "")
    pm_funder: str = _env("PM_FUNDER", "")


CFG = Config()
