"""Build data/windows_full.parquet: every BTC up/down window across all families.

Sources: vault windows.parquet (through 2026-05-12) + Telonex markets catalog
(btc_updown_markets.parquet, copied to data/tlx/) for the tail and the 1h family.
result: 0 = Up won, 1 = Down won.
"""
import os
import re
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from strategy_lib.data import DATA, FAMILY_DURATION, taker_fee_rate

SCRATCH = "/tmp/claude-0/-home-user-polymarketstrat/5805d42b-5a3a-57ae-9378-55ff20b77134/scratchpad"


def main():
    w = pd.read_parquet(os.path.join(DATA, "windows.parquet"))
    mk_path = os.path.join(DATA, "tlx", "btc_updown_markets.parquet")
    if not os.path.exists(mk_path):
        os.makedirs(os.path.dirname(mk_path), exist_ok=True)
        import shutil
        shutil.copy(os.path.join(SCRATCH, "btc_updown_markets.parquet"), mk_path)
    mk = pd.read_parquet(mk_path)

    mk = mk[mk.status == "resolved"].copy()
    mk["family"] = mk.fam.replace({"1h_et": "1h"})

    # windows for updown families come straight from slug epoch
    epoch = mk.slug.str.extract(r'-(\d{10})$')[0]
    mk["wts"] = pd.to_numeric(epoch, errors="coerce")
    # 1h family: end_date_us is close; verify duration
    is1h = mk.family == "1h"
    mk.loc[is1h, "wts"] = (mk.loc[is1h, "end_date_us"] // 1_000_000) - 3600

    mk = mk.dropna(subset=["wts"])
    mk["wts"] = mk.wts.astype("int64")
    mk["duration"] = mk.family.map(FAMILY_DURATION).astype("int32")
    mk["result"] = pd.to_numeric(mk.result_id, errors="coerce")
    mk = mk.dropna(subset=["result"])
    mk["result"] = mk.result.astype("int8")

    out = mk[["slug", "family", "wts", "duration", "market_id", "asset_id_0", "asset_id_1",
              "result", "settled_at_us", "end_date_us"]].copy()
    out["close_ts"] = out.wts + out.duration
    out["date"] = pd.to_datetime(out.wts, unit="s", utc=True).dt.strftime("%Y-%m-%d")

    # sanity check 1h duration against end-start where both present
    chk = mk[is1h & (mk.start_date_us > 0)]
    if len(chk):
        dur = ((chk.end_date_us - chk.start_date_us) / 1e6)
        print("1h start->end duration quantiles:", dur.quantile([0.05, 0.5, 0.95]).tolist())

    # merge vault extras (chainlink open/close, volume, vault fee_rate, vault result)
    v = w[["slug", "open_chainlink", "close_chainlink", "volume_shares", "volume_usdc",
           "fee_rate", "result_id"]].copy()
    v["result_vault"] = pd.to_numeric(v.result_id, errors="coerce")
    v = v.drop(columns=["result_id"])
    out = out.merge(v, on="slug", how="left")

    # cross-check result consistency where both sources have it
    both = out.dropna(subset=["result_vault"])
    mism = (both.result != both.result_vault).mean()
    print(f"result mismatch vault vs telonex: {mism:.5f} over {len(both)}")

    out["fee_rate_hist"] = [taker_fee_rate(d, f) for d, f in zip(out.date, out.family)]
    out.loc[out.fee_rate.isna(), "fee_rate"] = out.loc[out.fee_rate.isna(), "fee_rate_hist"]

    out = out.sort_values(["family", "wts"]).reset_index(drop=True)
    print(out.groupby("family").agg(n=("slug", "count"), dmin=("date", "min"), dmax=("date", "max")))
    out.to_parquet(os.path.join(DATA, "windows_full.parquet"), index=False)
    print("wrote windows_full.parquet", len(out))


if __name__ == "__main__":
    main()
