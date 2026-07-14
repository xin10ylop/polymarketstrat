"""Data loading utilities. All prices are the Up token (token 0)."""
import os
from datetime import date, timedelta

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA = os.path.join(ROOT, "data")

FAMILY_DURATION = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}

# Taker fee rate timeline for the short crypto families (15m/5m/1h).
# 4h stayed free until 2026-03-06. Maker fee is always 0.
FEE_EPOCHS = [
    ("2020-01-01", 0.0),
    ("2026-01-05", 0.0624),
    ("2026-03-30", 0.072),
    ("2026-05-07", 0.07),
]
FEE_EPOCHS_4H = [
    ("2020-01-01", 0.0),
    ("2026-03-06", 0.0624),
    ("2026-03-30", 0.072),
    ("2026-05-07", 0.07),
]
CURRENT_TAKER_RATE = 0.07


def taker_fee_rate(day: str, family: str) -> float:
    epochs = FEE_EPOCHS_4H if family == "4h" else FEE_EPOCHS
    rate = 0.0
    for d, r in epochs:
        if day >= d:
            rate = r
    return rate


def taker_fee(price, rate=CURRENT_TAKER_RATE):
    """Fee in $ per share for a taker fill at `price`."""
    return rate * price * (1.0 - price)


def daterange(a: str, b: str):
    d = date.fromisoformat(a)
    end = date.fromisoformat(b)
    while d <= end:
        yield d.isoformat()
        d += timedelta(days=1)


def family_days(family: str, kind: str = "quotes"):
    p = os.path.join(DATA, "daily", family, kind)
    return sorted(f[:-8] for f in os.listdir(p) if f.endswith(".parquet"))


def load_daily(family: str, kind: str, day: str) -> pd.DataFrame:
    return pd.read_parquet(os.path.join(DATA, "daily", family, kind, f"{day}.parquet"))


def load_windows() -> pd.DataFrame:
    return pd.read_parquet(os.path.join(DATA, "windows_full.parquet"))


def load_binance_second_series() -> pd.DataFrame:
    """Global 1-second BTC series with rolling vol features (built by build_binance_features.py)."""
    return pd.read_parquet(os.path.join(DATA, "binance", "btc_1s.parquet"))
