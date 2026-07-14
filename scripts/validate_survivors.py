"""Out-of-sample validation of surviving strategies on TEST data (2026-05-16 .. 2026-07-07).

S1: 5m favorite harvest -- maker join-bid at +60s when favorite mid in [0.80, 0.93], settle.
S2: T-5s oracle snipe   -- taker at standing ask when lookahead-free binance model >= 0.995.
S3: post-close toll     -- maker bid 0.992 on determined winner at T+2s, settle.

Each with sensitivity variants. Train numbers recomputed with identical code for comparison.
"""
import os
import sys

import numpy as np
import pandas as pd
from scipy.stats import norm

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from strategy_lib.backtest import TapeBacktester

TEST_START = "2026-05-16"
OUT = []


def log(strategy, variant, period, res_df, extra="", dump=False):
    f = res_df[res_df.filled] if "filled" in res_df else res_df
    if len(f) == 0:
        OUT.append(dict(strategy=strategy, variant=variant, period=period, fills=0))
        return
    if dump and period == "test":
        cols = [c for c in ["wts", "date", "pnl", "entry_t", "token"] if c in f.columns]
        f[cols].to_csv(f"results/trades_{strategy}_{period}.csv", index=False)
    days = res_df.date.nunique()
    row = dict(strategy=strategy, variant=variant, period=period,
               orders=len(res_df), fills=len(f),
               fill_rate=round(len(f) / len(res_df), 3),
               ev_c=round(f.pnl.mean() * 100, 3),
               t_stat=round(f.pnl.mean() / (f.pnl.std() / np.sqrt(len(f))), 2) if len(f) > 1 else np.nan,
               win=round((f.pnl > 0).mean(), 3),
               tpd=round(len(f) / days, 1), days=days, extra=extra)
    monthly = f.groupby(f.date.str[:7]).pnl.mean().round(4)
    row["monthly"] = "|".join(f"{k}:{v*100:.1f}c" for k, v in monthly.items())
    OUT.append(row)
    print(row, flush=True)


def s1_favorite_harvest():
    m = pd.read_parquet("data/features/5m_master.parquet")
    mid60 = (m["bid_price@60"] + m["ask_price@60"]) / 2
    m["fav_up"] = mid60 >= 0.5
    m["f"] = np.where(m.fav_up, mid60, 1 - mid60)
    m["tok"] = np.where(m.fav_up, "up", "down")
    m["fav_bid"] = np.where(m.fav_up, m["bid_price@60"], 1 - m["ask_price@60"])
    sel_all = m[(m.f >= 0.80) & (m.f <= 0.93) & m.fav_bid.notna()]
    for qm, variant in [(2.0, "base qm=2"), (5.0, "strict qm=5")]:
        bt = TapeBacktester("5m", queue_mult=qm)
        orders = pd.DataFrame({"wts": sel_all.wts, "date": sel_all.date, "token": sel_all.tok,
                               "limit_px": sel_all.fav_bid.round(3), "t_place": 60.0, "t_cancel": 105.0,
                               "exit_target": None, "exit_deadline": None,
                               "exit_mode": "settle", "shares": 12.0})
        for period, sub in [("train", orders[orders.date < TEST_START]),
                            ("test", orders[orders.date >= TEST_START])]:
            if variant != "base qm=2" and period == "train":
                continue
            res = bt.run(sub.drop(columns=["date"])).merge(sub[["wts", "date"]], on="wts")
            log("S1_fav_harvest", variant, period, res, dump=(variant == "base qm=2"))
    # band + offset sensitivity on test only (qm=2)
    bt = TapeBacktester("5m", queue_mult=2.0)
    for off in [45, 90]:
        mid = (m[f"bid_price@{off}"] + m[f"ask_price@{off}"]) / 2
        fav_up = mid >= 0.5
        f = np.where(fav_up, mid, 1 - mid)
        fav_bid = np.where(fav_up, m[f"bid_price@{off}"], 1 - m[f"ask_price@{off}"])
        sel = m[(f >= 0.80) & (f <= 0.93) & pd.notna(fav_bid) & (m.date >= TEST_START)]
        selb = pd.Series(fav_bid, index=m.index).loc[sel.index]
        selt = pd.Series(np.where(fav_up, "up", "down"), index=m.index).loc[sel.index]
        orders = pd.DataFrame({"wts": sel.wts, "date": sel.date, "token": selt,
                               "limit_px": selb.round(3), "t_place": float(off),
                               "t_cancel": off + 45.0, "exit_target": None,
                               "exit_deadline": None, "exit_mode": "settle", "shares": 12.0})
        res = bt.run(orders.drop(columns=["date"])).merge(orders[["wts", "date"]], on="wts")
        log("S1_fav_harvest", f"entry@{off}s", "test", res)


def s2_snipe(fam="5m", off=295):
    g = pd.read_parquet(f"data/features/{fam}_grid_model.parquet",
                        columns=["wts", "off", "bid_price", "ask_price", "bid_size", "ask_size",
                                 "result", "date", "ts_s", "K", "duration"])
    g = g[(g.off == off) & g.ask_price.notna()]
    b = pd.read_parquet("data/binance/btc_1s.parquet", columns=["ts", "close", "vol_300"])
    for shift, variant in [(1, "signal@T-5 (1s-lag binance)"), (2, "signal@T-6 (2s stale)")]:
        bb = b.rename(columns={"close": "S_lag", "vol_300": "vol_lag"}).copy()
        bb["ts"] = bb.ts + shift
        gg = g.merge(bb, left_on="ts_s", right_on="ts", how="left")
        gg = gg[gg.S_lag.notna()]
        tau = gg.duration - gg.off
        gg["fv_lag"] = norm.cdf(np.log(gg.S_lag / gg.K) / (np.maximum(gg.vol_lag, 1e-6) * np.sqrt(tau)))
        up = gg.result == 0
        bu = (gg.fv_lag >= 0.995) & (gg.ask_price <= 0.97) & (gg.ask_size >= 12)
        bd = (gg.fv_lag <= 0.005) & ((1 - gg.bid_price) <= 0.97) & (gg.bid_size >= 12)
        pnl_u = up[bu].astype(float) - gg.ask_price[bu] - 0.07 * gg.ask_price[bu] * (1 - gg.ask_price[bu])
        pnl_d = (~up[bd]).astype(float) - (1 - gg.bid_price[bd]) - 0.07 * (1 - gg.bid_price[bd]) * gg.bid_price[bd]
        res = pd.DataFrame({"pnl": pd.concat([pnl_u, pnl_d]),
                            "date": pd.concat([gg.date[bu], gg.date[bd]]),
                            "wts": pd.concat([gg.wts[bu], gg.wts[bd]])})
        res["filled"] = True
        for period, sub in [("train", res[res.date < TEST_START]),
                            ("test", res[res.date >= TEST_START])]:
            log(f"S2_snipe_{fam}", variant, period, sub, extra="ask<=.97 sz>=12",
                dump=(shift == 1))
        if shift == 1:
            # deeper-disagreement variant
            bu2 = (gg.fv_lag >= 0.995) & (gg.ask_price <= 0.80) & (gg.ask_size >= 12)
            bd2 = (gg.fv_lag <= 0.005) & ((1 - gg.bid_price) <= 0.80) & (gg.bid_size >= 12)
            p2 = pd.concat([up[bu2].astype(float) - gg.ask_price[bu2] - 0.07 * gg.ask_price[bu2] * (1 - gg.ask_price[bu2]),
                            (~up[bd2]).astype(float) - (1 - gg.bid_price[bd2]) - 0.07 * (1 - gg.bid_price[bd2]) * gg.bid_price[bd2]])
            r2 = pd.DataFrame({"pnl": p2, "date": pd.concat([gg.date[bu2], gg.date[bd2]])})
            r2["filled"] = True
            for period, sub in [("train", r2[r2.date < TEST_START]), ("test", r2[r2.date >= TEST_START])]:
                log(f"S2_snipe_{fam}", "deep ask<=.80", period, sub)


def s3_toll():
    w = pd.read_parquet("data/windows_full.parquet")
    for fam, T, lim in [("5m", 300, 0.992), ("15m", 900, 0.992)]:
        wf = w[(w.family == fam) & (w.date >= "2026-02-12")]
        orders = pd.DataFrame({"wts": wf.wts, "date": wf.date,
                               "token": np.where(wf.result == 0, "up", "down"),
                               "limit_px": lim, "t_place": T + 2.0, "t_cancel": T + 22.0,
                               "exit_target": None, "exit_deadline": None,
                               "exit_mode": "settle", "shares": 15.0})
        bt = TapeBacktester(fam, queue_mult=2.0)
        for period, sub in [("train", orders[orders.date < TEST_START]),
                            ("test", orders[orders.date >= TEST_START])]:
            if fam == "15m" and period == "train":
                sub = sub[sub.date >= "2026-04-01"]  # sample for runtime
            res = bt.run(sub.drop(columns=["date"])).merge(sub[["wts", "date"]], on="wts")
            log("S3_toll", f"{fam} lim={lim}", period, res)
    # sensitivity on 5m test: qm=5 and t_place=T+4
    wf = w[(w.family == "5m") & (w.date >= TEST_START)]
    for qm, tp, variant in [(5.0, 302.0, "qm=5"), (2.0, 304.0, "place@T+4")]:
        orders = pd.DataFrame({"wts": wf.wts, "date": wf.date,
                               "token": np.where(wf.result == 0, "up", "down"),
                               "limit_px": 0.992, "t_place": tp, "t_cancel": 322.0,
                               "exit_target": None, "exit_deadline": None,
                               "exit_mode": "settle", "shares": 15.0})
        bt = TapeBacktester("5m", queue_mult=qm)
        res = bt.run(orders.drop(columns=["date"])).merge(orders[["wts", "date"]], on="wts")
        log("S3_toll", variant, "test", res)


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "5m"):
        s2_snipe("5m", 295)
        s1_favorite_harvest()
        s3_toll()
    if which in ("all", "15m"):
        s2_snipe("15m", 885)
    df = pd.DataFrame(OUT)
    df.to_csv(f"results/validation_{which}.csv", index=False)
    print(df.drop(columns=["monthly"], errors="ignore").to_string())
