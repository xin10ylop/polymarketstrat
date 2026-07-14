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
    polygon_rpcs: tuple = (
        "https://polygon-rpc.com",
        "https://polygon-bor-rpc.publicnode.com",
        "https://polygon.drpc.org",
        "https://1rpc.io/matic",
    )
    chainlink_btc_usd: str = "0xc907E116054Ad103354f2D350FD2514433D57F6f"  # Polygon aggregator proxy
    oracle_poll_ms: int = 300
    # sandbox/dev fallback when no RPC is reachable: use spot feed as pseudo-oracle (DEGRADED)
    oracle_allow_spot_fallback: bool = _env("ORACLE_SPOT_FALLBACK", "0") == "1"

    # --- fees (verified May-Jul 2026: taker 0.07*p*(1-p), maker 0). CLOB metadata
    # now shows base_fee=1000 for both sides; until a real fill proves otherwise we
    # model the verified curve and log metadata drift at startup. ---
    taker_fee_mult: float = _env("TAKER_FEE_MULT", 0.07, float)
    maker_fee_mult: float = _env("MAKER_FEE_MULT", 0.0, float)

    # --- toll strategy (S3): post-close bid on the determined winner ---
    toll_enabled: bool = True
    toll_place_delay_s: float = 2.0        # place at T+2s after window close
    toll_cancel_after_s: float = 22.0      # cancel at T+22s (settlement ~T+23s)
    toll_price_fine: float = 0.992         # when 0.001 tick regime is active
    toll_price_coarse: float = 0.99        # when tick regime is 0.01
    toll_min_clip: int = 50                # shares
    toll_max_clip: int = 1000
    toll_start_clip: int = 250
    toll_float_budget: float = _env("TOLL_FLOAT", 1000.0, float)  # max $ parked per window
    # feedback controller: raise clip while marginal fills keep coming, shrink on misses
    toll_target_fill_share: float = 0.6    # aim to fill >=60% of posted size
    toll_controller_alpha: float = 0.2     # EMA step per window

    # --- snipe strategy (S2): basis-corrected terminal taker ---
    snipe_enabled: bool = True
    snipe_eval_from_s: float = -6.0        # start evaluating at T-6s
    snipe_signal_lag_s: float = 1.0        # act on spot data at least 1s old (validated latency)
    snipe_fv_min: float = 0.995
    snipe_ask_max: float = 0.97
    snipe_min_ask_size: float = 12.0
    snipe_max_clip: int = 250              # hard cap: EV collapses above (adverse selection)
    snipe_vol_floor: float = 1e-6
    basis_window_s: int = 60               # rolling median window for oracle/spot basis
    vol_window_s: int = 300                # realized vol estimator window

    # --- risk / kill-switches ---
    max_daily_loss: float = _env("MAX_DAILY_LOSS", 25.0, float)   # $ paper, halt for the day
    snipe_trailing_n: int = 50
    snipe_min_winrate: float = 0.60
    oracle_max_staleness_s: float = 3.0    # halt toll if oracle read older than this at close
    feed_max_silence_s: float = 10.0       # halt if spot feed silent this long

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
