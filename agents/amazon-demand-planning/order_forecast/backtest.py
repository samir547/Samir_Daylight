"""Rolling-origin back-test: forecast from each origin using only earlier months,
then score against three answer keys.

  sellout   the series being forecast (ordered or shipped units, per ASIN)
  receipts  Amazon's net_received_units per ASIN: what Amazon took in. Never a model input.
  invoices  Daylight's gross invoiced units per marketplace-month. Never a model input.
            Compared in total only, so no SKU map is needed.

Windows are non-overlapping by default (stride = horizon).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .baselines import METHODS
from .metrics import bias, wape
from .series import to_wide
from .snapshot import Snapshot


def make_origins(n_months: int, min_train: int, horizon: int, stride: int) -> list[int]:
    """Index t: train on months[:t], forecast months[t:t+horizon]."""
    return list(range(min_train, n_months - horizon + 1, stride))


def forecast_series(wide: pd.DataFrame, origins: list[int], horizon: int,
                    methods: dict, min_history: int) -> pd.DataFrame:
    months, values = wide.columns, wide.to_numpy(dtype=float)
    out = []
    for t in origins:
        for i, asin in enumerate(wide.index):
            hist = values[i, :t]
            hist = hist[~np.isnan(hist)]            # NaN only before the series started
            if len(hist) < min_history:
                continue
            for name, fn in methods.items():
                pred = np.maximum(fn(hist, horizon), 0.0)
                for h in range(horizon):
                    out.append((name, months[t - 1], months[t + h], h + 1, asin, float(pred[h])))
    return pd.DataFrame(out, columns=["method", "train_end", "target_month", "step", "asin", "predicted"])


def _long(wide: pd.DataFrame, name: str) -> pd.DataFrame:
    w = wide.copy()
    w.index.name = "asin"
    long = w.reset_index().melt(id_vars="asin", var_name="target_month", value_name=name)
    long["target_month"] = pd.to_datetime(long["target_month"])
    return long


def _summarise(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    rows = []
    for keys, g in df.groupby(by):
        keys = keys if isinstance(keys, tuple) else (keys,)
        rows.append({**dict(zip(by, keys)), "n": len(g), "actual_units": g["actual"].sum(),
                     "wape": wape(g["actual"], g["predicted"]), "bias": bias(g["actual"], g["predicted"])})
    return pd.DataFrame(rows)


def score_asin_level(pred: pd.DataFrame, answer_wide: pd.DataFrame, answer_key: str) -> pd.DataFrame:
    act = _long(answer_wide, "actual")
    j = pred.merge(act, on=["asin", "target_month"], how="left")
    j["actual"] = j["actual"].fillna(0.0)
    s = _summarise(j, ["method"])
    s.insert(0, "answer_key", answer_key)
    s.insert(1, "level", "asin-month")
    return s


def score_total_level(pred: pd.DataFrame, monthly_totals: pd.Series, answer_key: str) -> pd.DataFrame:
    tot = pred.groupby(["method", "train_end", "target_month"], as_index=False)["predicted"].sum()
    tot["actual"] = tot["target_month"].map(monthly_totals).fillna(0.0)
    s = _summarise(tot, ["method"])
    s.insert(0, "answer_key", answer_key)
    s.insert(1, "level", "marketplace-month")
    return s


def coverage(pred: pd.DataFrame, sellout_wide: pd.DataFrame) -> float:
    """Share of the target-period sell-out volume that belongs to series the back-test could score."""
    any_method = pred[pred["method"] == pred["method"].iloc[0]]
    scored = any_method.merge(_long(sellout_wide, "actual"), on=["asin", "target_month"], how="left")["actual"].sum()
    allv = 0.0
    for _, g in any_method.groupby("train_end"):
        allv += sellout_wide[g["target_month"].unique()].sum().sum()
    return float(scored / allv) if allv else float("nan")


def score_predictions(pred: pd.DataFrame, snap: Snapshot, target: str) -> pd.DataFrame:
    """Score any predictions frame (method, train_end, target_month, step, asin, predicted)
    against the three answer keys. Used for the baselines and for external models alike."""
    months = snap.months
    sell = to_wide(snap.sales, "asin", target, months, mask_before_first=True).fillna(0.0)
    receipts = to_wide(snap.inventory, "asin", "net_received_units", months, mask_before_first=False)
    inv_total = snap.invoices.groupby("month_start")["gross_units"].sum()
    return pd.concat([
        score_asin_level(pred, sell, "sellout"),
        score_asin_level(pred, receipts, "receipts"),
        score_total_level(pred, sell.sum(), "sellout"),
        score_total_level(pred, receipts.sum(), "receipts"),
        score_total_level(pred, inv_total, "invoices"),
    ], ignore_index=True)


def run_backtest(snap: Snapshot, target: str = "ordered_units", horizon: int = 6, min_train: int = 24,
                 stride: int | None = None, min_history: int = 12, methods: dict | None = None) -> dict:
    methods = methods or METHODS
    months = snap.months
    stride = stride or horizon
    origins = make_origins(len(months), min_train, horizon, stride)
    if not origins:
        raise ValueError(f"{len(months)} months is too few for min_train={min_train}, horizon={horizon}")
    sell = to_wide(snap.sales, "asin", target, months, mask_before_first=True)
    pred = forecast_series(sell, origins, horizon, methods, min_history)
    scores = score_predictions(pred, snap, target)
    return {"predictions": pred, "scores": scores, "origins": [months[t - 1] for t in origins],
            "coverage": coverage(pred, sell), "series": int(len(sell)), "months": len(months)}
