"""Score predictions made outside this package (Azure AutoML) next to the baselines.

Everything is restricted to the series the external model actually predicted, so each
method is judged on the same ASINs, the same window and the same answer keys.
"""
from __future__ import annotations

import pandas as pd

from .backtest import coverage, forecast_series, score_predictions
from .baselines import METHODS
from .series import to_wide
from .snapshot import Snapshot


def external_predictions(raw: pd.DataFrame, snap: Snapshot, horizon: int, label: str) -> pd.DataFrame:
    need = {"series_id", "ds", "y_predicted"}
    if not need <= set(raw.columns):
        raise SystemExit(f"predictions file is missing {sorted(need - set(raw.columns))}; it has {list(raw.columns)}")
    months = snap.months
    train_end = months[len(months) - horizon - 1]
    d = pd.DataFrame({"method": label, "train_end": train_end,
                      "target_month": pd.to_datetime(raw["ds"]),
                      "asin": raw["series_id"].astype(str).str.strip(),
                      "predicted": pd.to_numeric(raw["y_predicted"], errors="coerce")}).dropna(subset=["predicted"])
    d["predicted"] = d["predicted"].clip(lower=0.0)
    d["step"] = (d["target_month"].dt.year - train_end.year) * 12 + d["target_month"].dt.month - train_end.month
    d = d[(d["step"] >= 1) & (d["step"] <= horizon)]
    return d[["method", "train_end", "target_month", "step", "asin", "predicted"]]


def compare(snap: Snapshot, ext: pd.DataFrame, target: str, horizon: int, min_history: int) -> dict:
    months = snap.months
    sell = to_wide(snap.sales, "asin", target, months, mask_before_first=True)
    base = forecast_series(sell, [len(months) - horizon], horizon, METHODS, min_history)
    common = set(ext["asin"]) & set(base["asin"])
    pred = pd.concat([base[base["asin"].isin(common)], ext[ext["asin"].isin(common)]], ignore_index=True)
    return {"scores": score_predictions(pred, snap, target), "predictions": pred,
            "series_compared": len(common), "baseline_series": int(base["asin"].nunique()),
            "missing_from_external": sorted(set(base["asin"]) - set(ext["asin"])),
            "coverage": coverage(pred, sell.fillna(0.0)) if len(pred) else float("nan")}
