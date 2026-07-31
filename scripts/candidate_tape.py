"""Record EVERY qualifying snipe candidate per window (not just first-touch),
with loose gates, so any strategy variant (hour filters, fv/price/size
thresholds, eval-window changes) can be evaluated post-hoc EXACTLY.

    python3 candidates.py <coin>

Loose recording gates: fv extreme at >=0.99 level, px <= 0.98, sz > 0.
Baseline strategy = first candidate per window passing: fv>=0.995, px<=0.97,
12<=sz<=500, survival gate. Output: cand_<coin>.parquet
"""
import glob
import math
import os
import sys

import numpy as np
import pandas as pd

COIN = sys.argv[1].lower()
ROOT = "/home/user/polymarketstrat"
KDIR = f"{ROOT}/data/binance/klines_1s_{COIN}"
ONE_S = f"{ROOT}/data/binance/{COIN}_1s.parquet"
SUF = "" if COIN == "btc" else f"_{COIN}"
RAW = "raw" if COIN == "btc" else f"raw_{COIN}"


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def build_1s():
    days = sorted(glob.glob(f"{KDIR}/*.parquet"))
    df = pd.concat([pd.read_parquet(f, columns=["open_time", "close"]) for f in days],
                   ignore_index=True)
    unit = 1000 if df.open_time.iloc[0] < 10**14 else 1_000_000
    df["ts"] = (df.open_time // unit).astype("int64")
    df = df.drop(columns=["open_time"]).sort_values("ts").drop_duplicates("ts")
    full = pd.DataFrame({"ts": np.arange(df.ts.iloc[0], df.ts.iloc[-1] + 1, dtype="int64")})
    df = full.merge(df, on="ts", how="left")
    df["close"] = df.close.ffill()
    r1 = pd.Series(np.diff(np.log(df.close.to_numpy()), prepend=np.nan))
    df["vol_300"] = r1.rolling(300, min_periods=150).std().to_numpy().astype("float32")
    df.to_parquet(ONE_S, index=False)
    print(f"built {ONE_S}: {len(df)}s")


if os.path.exists(ONE_S):
    b0 = pd.read_parquet(ONE_S, columns=["ts"])
    if len(b0) < len(glob.glob(f"{KDIR}/*.parquet")) * 86400 - 3600:
        os.remove(ONE_S)
if not os.path.exists(ONE_S):
    build_1s()

w = pd.read_parquet(f"{ROOT}/data/fresh/windows_fresh{SUF}.parquet")
b = pd.read_parquet(ONE_S).set_index("ts")
bc, bv = b.close, b.vol_300
rows = []
nwin = 0
for r in w.itertuples():
    f = f"{ROOT}/data/fresh/{RAW}/quotes/{r.date}/{r.slug}.parquet"
    if not os.path.exists(f):
        continue
    try:
        q = pd.read_parquet(f, columns=["timestamp_us", "bid_price", "bid_size",
                                        "ask_price", "ask_size"])
    except Exception:
        continue
    if not len(q):
        continue
    nwin += 1
    for c in ["bid_price", "bid_size", "ask_price", "ask_size"]:
        q[c] = pd.to_numeric(q[c], errors="coerce")
    q["rel"] = q.timestamp_us / 1e6 - r.wts
    K = bc.get(r.wts, np.nan)
    if not np.isfinite(K) or K <= 0:
        continue
    hour = (r.wts // 3600) % 24
    dow = pd.Timestamp(r.wts, unit="s").dayofweek
    for toff in np.arange(294.0, 298.6, 0.5):
        t_sig = r.wts + toff - 1
        S, vol = bc.get(int(t_sig), np.nan), bv.get(int(t_sig), np.nan)
        if not (np.isfinite(S) and np.isfinite(vol) and vol > 0):
            continue
        z = math.log(S / K) / (vol * math.sqrt(300 - toff))
        fv = ncdf(z)
        side = "up" if fv >= 0.99 else ("down" if fv <= 0.01 else None)
        if side is None:
            continue
        qq = q[q.rel <= toff]
        if not len(qq):
            continue
        last = qq.iloc[-1]
        if side == "up":
            px, sz = last.ask_price, last.ask_size
        else:
            px, sz = (1 - last.bid_price), last.bid_size
        if not (np.isfinite(px) and np.isfinite(sz)) or px > 0.98 or sz <= 0:
            continue
        qg = q[(q.rel > toff) & (q.rel <= toff + 0.5)]
        gate = True
        if len(qg):
            lg = qg.iloc[-1]
            gpx = lg.ask_price if side == "up" else (1 - lg.bid_price)
            gsz = lg.ask_size if side == "up" else lg.bid_size
            gate = bool(np.isfinite(gpx) and np.isfinite(gsz)
                        and gpx <= 0.97 and gsz >= 12)
        win = 1.0 if (side == "up") == (r.result == 0) else 0.0
        rows.append(dict(wts=r.wts, date=r.date, hour=hour, dow=dow, toff=toff,
                         side=side, fvx=max(fv, 1 - fv), px=px, sz=sz,
                         gate=gate, win=win))

df = pd.DataFrame(rows)
out = f"/home/user/polymarketstrat/data/tapes/cand_{COIN}.parquet"
df.to_parquet(out, index=False)
print(f"{COIN}: {nwin} windows scanned, {len(df)} candidate touches -> {out}")
