"""
Stand-in forecasts, in EXACTLY the schema an Azure ML arm must hand the scorer:

    series_id (fit key) | ds (month start) | yhat | model

Three deliberately simple models, computed from the snapshot's training
parquet only (never from test actuals):

    standin_naive3          trailing 3-month mean of the zero-filled series
    standin_naive12         trailing 12-month mean of the zero-filled series
    standin_seasonal_naive  the same calendar month one year earlier

Purpose: prove the whole loop (snapshot -> predictions -> split back to items ->
scoring -> write-back shape) before any cloud spend, and give every future arm a
floor to beat. standin_naive3/12 are an INDEPENDENT re-implementation of the
production naive route (models/naive_model.py: zero-inclusive trailing mean over
the nominal window, anchored at TRAIN_END, clipped at 0, rounded to 2dp) — the
contract check compares them against the production run's own naive rows.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from azureml_benchmark import common


def _trailing_mean(y: pd.Series, window: int) -> float:
    return float(y.tail(window).sum()) / window     # zero-filled grid, nominal divisor


def standin_predictions(snapshot_dir: Path) -> pd.DataFrame:
    snapshot_dir = Path(snapshot_dir)
    man = common.read_manifest(snapshot_dir)
    w = man["windows"]
    train = pd.read_parquet(snapshot_dir / "train" / "train.parquet")
    test_months = pd.date_range(common.month_ts(w["TEST_START"]),
                                common.month_ts(w["TEST_END"]), freq="MS")
    rows = []
    for key, g in train.groupby("series_id", sort=True):
        s = g.sort_values("ds").set_index("ds")["y"]
        for model, window in (("standin_naive3", 3), ("standin_naive12", 12)):
            level = round(max(_trailing_mean(s, window), 0.0), 2)
            rows += [(key, d, level, model) for d in test_months]
        for d in test_months:
            ly = d - pd.DateOffset(months=12)
            val = float(s.get(ly, 0.0))
            rows.append((key, d, round(max(val, 0.0), 2), "standin_seasonal_naive"))
    out = pd.DataFrame(rows, columns=["series_id", "ds", "yhat", "model"])
    out["ds"] = pd.to_datetime(out["ds"])
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("snapshot_dir", type=Path)
    ap.add_argument("--out", type=Path, required=True, help="predictions parquet to write")
    a = ap.parse_args()
    p = standin_predictions(a.snapshot_dir)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    p.to_parquet(a.out, index=False)
    print(f"{len(p):,} predictions, {p.series_id.nunique()} series, models {sorted(p.model.unique())} -> {a.out}")
