"""WAPE and bias, pooled over rows (sum of errors over sum of actuals)."""
from __future__ import annotations

import numpy as np


def wape(actual, predicted) -> float:
    a, p = np.asarray(actual, float), np.asarray(predicted, float)
    denom = np.abs(a).sum()
    return float("nan") if denom == 0 else float(np.abs(p - a).sum() / denom)


def bias(actual, predicted) -> float:
    """Positive = over-forecast."""
    a, p = np.asarray(actual, float), np.asarray(predicted, float)
    denom = a.sum()
    return float("nan") if denom == 0 else float((p - a).sum() / denom)
