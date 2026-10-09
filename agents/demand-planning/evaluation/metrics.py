"""
Forecast accuracy metrics: MAE, RMSE, MAPE, WAPE, Bias.

MAPE is undefined where the actual is zero, so those rows are excluded from the
MAPE average (other metrics still use the full vector).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error, mean_squared_error


def mape(actual: pd.Series, predicted: pd.Series) -> float:
    """Mean Absolute Percentage Error (%), excluding zero-actual rows."""
    actual = np.asarray(actual, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    nonzero = actual != 0
    if not nonzero.any():
        return float("nan")
    return float(np.mean(np.abs((actual[nonzero] - predicted[nonzero]) / actual[nonzero])) * 100)


def wape(actual: pd.Series, predicted: pd.Series) -> float:
    """Weighted Absolute Percentage Error (%) = Σ|a-p| / Σ|a|."""
    actual = np.asarray(actual, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    denom = np.sum(np.abs(actual))
    if denom == 0:
        return float("nan")
    return float(np.sum(np.abs(actual - predicted)) / denom * 100)


def bias(actual: pd.Series, predicted: pd.Series) -> float:
    """Forecast bias (%) = Σ(p-a) / Σa. Positive = over-forecast."""
    actual = np.asarray(actual, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    denom = np.sum(actual)
    if denom == 0:
        return float("nan")
    return float(np.sum(predicted - actual) / denom * 100)


def calc_metrics(actual: pd.Series, predicted: pd.Series) -> dict:
    """Full metric bundle for a single series' test window."""
    mae  = float(mean_absolute_error(actual, predicted))
    rmse = float(np.sqrt(mean_squared_error(actual, predicted)))
    return {
        "mae":      round(mae, 2),
        "rmse":     round(rmse, 2),
        "mape_pct": round(mape(actual, predicted), 2),
        "wape_pct": round(wape(actual, predicted), 2),
        "bias_pct": round(bias(actual, predicted), 2),
    }
