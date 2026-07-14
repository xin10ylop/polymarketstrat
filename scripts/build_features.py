"""Per-window feature extraction: snapshots, path grid, trade aggregates.

Usage: python3 scripts/build_features.py <family>   (5m | 15m | 4h | 1h)

Outputs (in data/features/):
  {family}_snap.parquet   one row per window: BBO at key offsets, book depth (vault fams)
  {family}_grid.parquet   long: (wts, off) -> bid/ask/sizes on a regular grid
  {family}_trades.parquet one row per window: trade aggregates over key intervals
"""
import os
import sys
import glob
from multiprocessing import Pool

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from strategy_lib.data import DATA, FAMILY_DURATION

PRE = [-1800, -600, -300, -120, -60, -30, -15, -10, -5, -2, -1]
POST_FIX = [1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 90, 120]
FRAC = [0.25, 0.5, 0.75]
END = [-60, -30, -15, -10, -5, -2, -1]        # relative to close
POSTCLOSE = [2, 5, 10, 15, 20, 25, 30, 40]    # after close
GRID_STEP = {"5m": 5, "15m": 15, "1h": 30, "4h": 120}
BOOK_OFFS = [-30, -10, -5, -1, 1, 5, 10, 30]  # bookcurve sample offsets

TRADE_INTERVALS = [
    ("pre3600", -3600, 0), ("pre300", -300, 0), ("pre60", -60, 0), ("pre10", -10, 0),
    ("post10", 0, 10), ("post30", 0, 30), ("post60", 0, 60),
    ("mid", 0.4, 0.6), ("end30", -30.0001, 0.0001), ("end10", -10.0001, 0.0001),
    ("postclose", 0.0001, 45),
]


def snap_offsets(T):
    offs = list(PRE)
    offs += [o for o in POST_FIX if o < T - 60]
    offs += [int(round(f * T)) for f in FRAC]
    offs += [T + o for o in END]
    offs += [T + o for o in POSTCLOSE]
    return sorted(set(offs))


def extract_day(args):
    family, day, wts_list, T = args
    try:
        if family == "1h":
            q = load_1h_day("quotes", day)
            t = load_1h_day("trades", day)
        else:
            q = pd.read_parquet(os.path.join(DATA, "daily", family, "quotes", f"{day}.parquet"))
            t = pd.read_parquet(os.path.join(DATA, "daily", family, "trades", f"{day}.parquet"))
    except FileNotFoundError:
        return None
    if q is None or len(q) == 0:
        return None
    wts_arr = np.array(sorted(set(wts_list) & set(q.wts.unique())), dtype="int64")
    if len(wts_arr) == 0:
        return None

    q = q.sort_values("timestamp_us")
    snap = _snapshots(q, wts_arr, T)
    grid = _grid(q, wts_arr, T, GRID_STEP[family])
    tr = _trade_aggs(t, wts_arr, T) if t is not None and len(t) else None

    book = None
    if family != "1h":
        try:
            b = pd.read_parquet(os.path.join(DATA, "daily", family, "bookcurves", f"{day}.parquet"),
                                columns=["timestamp_us", "wts", "bid_depth_5c", "ask_depth_5c",
                                         "buy_avgpx_200", "sell_avgpx_200",
                                         "buy_avgpx_1000", "sell_avgpx_1000",
                                         "bid_p0", "ask_p0", "bid_s0", "ask_s0"])
            b = b.sort_values("timestamp_us")
            book = _book_snaps(b, wts_arr, T)
        except FileNotFoundError:
            pass
    return snap, grid, tr, book


def load_1h_day(kind, day):
    # windows opening on `day` have pre-open events on day-1 and settle-zone
    # events on day+1, stored under those event-date files
    d = pd.Timestamp(day)
    days = [(d + pd.Timedelta(days=k)).strftime("%Y-%m-%d") for k in (-1, 0, 1)]
    dfs = []
    for dd in days:
        p = os.path.join(DATA, "daily", "1h", kind, f"{dd}.parquet")
        if os.path.exists(p):
            dfs.append(pd.read_parquet(p))
    if not dfs:
        return None
    df = pd.concat(dfs, ignore_index=True)
    if kind == "quotes":
        return df[["timestamp_us", "wts", "bid_price", "bid_size", "ask_price", "ask_size"]]
    return df[["timestamp_us", "wts", "price", "size", "side"]]


def _snapshots(q, wts_arr, T):
    offs = snap_offsets(T)
    targets = pd.DataFrame({
        "wts": np.repeat(wts_arr, len(offs)),
        "off": np.tile(offs, len(wts_arr)),
    })
    targets["ts_us"] = (targets.wts + targets.off) * 1_000_000
    targets = targets.sort_values("ts_us")
    qq = q.rename(columns={"timestamp_us": "ts_us"})
    m = pd.merge_asof(targets, qq[["ts_us", "wts", "bid_price", "bid_size", "ask_price", "ask_size"]],
                      on="ts_us", by="wts", direction="backward")
    wide = m.pivot(index="wts", columns="off",
                   values=["bid_price", "ask_price", "bid_size", "ask_size"])
    wide.columns = [f"{v}@{o}" for v, o in wide.columns]
    return wide.reset_index()


def _grid(q, wts_arr, T, step):
    offs = np.arange(-60, T + 41, step, dtype="int64")
    targets = pd.DataFrame({
        "wts": np.repeat(wts_arr, len(offs)),
        "off": np.tile(offs, len(wts_arr)),
    })
    targets["ts_us"] = (targets.wts + targets.off) * 1_000_000
    targets = targets.sort_values("ts_us")
    qq = q.rename(columns={"timestamp_us": "ts_us"})
    m = pd.merge_asof(targets, qq[["ts_us", "wts", "bid_price", "bid_size", "ask_price", "ask_size"]],
                      on="ts_us", by="wts", direction="backward")
    return m.sort_values(["wts", "off"])


def _book_snaps(b, wts_arr, T):
    targets = pd.DataFrame({
        "wts": np.repeat(wts_arr, len(BOOK_OFFS)),
        "off": np.tile(BOOK_OFFS, len(wts_arr)),
    })
    targets["ts_us"] = (targets.wts + targets.off) * 1_000_000
    targets = targets.sort_values("ts_us")
    bb = b.rename(columns={"timestamp_us": "ts_us"})
    m = pd.merge_asof(targets, bb, on="ts_us", by="wts", direction="backward")
    vals = [c for c in bb.columns if c not in ("ts_us", "wts")]
    wide = m.pivot(index="wts", columns="off", values=vals)
    wide.columns = [f"{v}@{o}" for v, o in wide.columns]
    return wide.reset_index()


def _trade_aggs(t, wts_arr, T):
    t = t[t.wts.isin(wts_arr)].copy()
    if not len(t):
        return None
    t["rel"] = t.timestamp_us / 1e6 - t.wts
    t["signed"] = np.where(t.side == "buy", t["size"], -t["size"])
    t["notional"] = t.price * t["size"]
    rows = {}
    for name, lo, hi in TRADE_INTERVALS:
        if name == "mid":
            lo_s, hi_s = lo * T, hi * T
        elif name in ("end30", "end10"):
            lo_s, hi_s = T + lo, T + hi
        elif name == "postclose":
            lo_s, hi_s = T + lo, T + hi
        else:
            lo_s, hi_s = lo, hi
        sub = t[(t.rel >= lo_s) & (t.rel < hi_s)]
        g = sub.groupby("wts").agg(
            n=("size", "size"), vol=("size", "sum"), signed=("signed", "sum"),
            notional=("notional", "sum"), last_px=("price", "last"))
        g[f"vwap"] = g.notional / g.vol
        g = g.drop(columns=["notional"])
        g.columns = [f"{name}_{c}" for c in g.columns]
        rows[name] = g
    out = pd.DataFrame(index=pd.Index(wts_arr, name="wts"))
    for g in rows.values():
        out = out.join(g, how="left")
    return out.reset_index()


_WMAP_1H = None


def main(family):
    global _WMAP_1H
    T = FAMILY_DURATION[family]
    w = pd.read_parquet(os.path.join(DATA, "windows_full.parquet"))
    w = w[w.family == family]
    if family == "1h":
        _WMAP_1H = dict(zip(w.slug, w.wts))
        days = sorted(f[:-8] for f in os.listdir(os.path.join(DATA, "daily", "1h", "quotes"))
                      if f.endswith(".parquet"))
    else:
        days = sorted(f[:-8] for f in os.listdir(os.path.join(DATA, "daily", family, "quotes"))
                      if f.endswith(".parquet"))
    by_day = {}
    for wts in w.wts:
        d = pd.Timestamp(wts, unit="s", tz="UTC").strftime("%Y-%m-%d")
        by_day.setdefault(d, []).append(wts)
    tasks = [(family, d, by_day.get(d, []), T) for d in days if d in by_day]
    print(f"{family}: {len(tasks)} days")
    snaps, grids, trades, books = [], [], [], []
    with Pool(4) as pool:
        for i, res in enumerate(pool.imap_unordered(extract_day, tasks)):
            if res is None:
                continue
            s, g, tr, b = res
            snaps.append(s); grids.append(g)
            if tr is not None: trades.append(tr)
            if b is not None: books.append(b)
            if (i + 1) % 25 == 0:
                print(f"{i+1}/{len(tasks)}", flush=True)
    os.makedirs(os.path.join(DATA, "features"), exist_ok=True)
    pd.concat(snaps, ignore_index=True).to_parquet(
        os.path.join(DATA, "features", f"{family}_snap.parquet"), index=False)
    pd.concat(grids, ignore_index=True).to_parquet(
        os.path.join(DATA, "features", f"{family}_grid.parquet"), index=False)
    if trades:
        pd.concat(trades, ignore_index=True).to_parquet(
            os.path.join(DATA, "features", f"{family}_trades.parquet"), index=False)
    if books:
        pd.concat(books, ignore_index=True).to_parquet(
            os.path.join(DATA, "features", f"{family}_book.parquet"), index=False)
    print(f"{family} done: {sum(len(s) for s in snaps)} windows")


if __name__ == "__main__":
    main(sys.argv[1])
