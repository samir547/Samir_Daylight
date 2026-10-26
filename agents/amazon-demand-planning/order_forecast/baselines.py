"""Simple forecasters. Each takes a history (1-D array, oldest first, no NaN)
and a horizon, and returns `horizon` forecasts for the next months.

These are the yardsticks any other model (Azure AutoML included) has to beat.
They never see anything after the forecast origin.
"""
from __future__ import annotations

import numpy as np


def naive_last(hist: np.ndarray, horizon: int) -> np.ndarray:
    return np.repeat(hist[-1], horizon)


def _mean_k(k: int):
    def f(hist: np.ndarray, horizon: int) -> np.ndarray:
        return np.repeat(hist[-k:].mean(), horizon)
    f.__name__ = f"mean{k}"
    return f


def prior_year(hist: np.ndarray, horizon: int) -> np.ndarray:
    """Same month last year; the 12-month mean where that month does not exist."""
    n = len(hist)
    fallback = hist[-12:].mean()
    out = np.empty(horizon)
    for h in range(1, horizon + 1):
        idx = n - 1 + h - 12
        out[h - 1] = hist[idx] if 0 <= idx < n else fallback
    return out


def ses(hist: np.ndarray, horizon: int) -> np.ndarray:
    """Simple exponential smoothing; alpha picked from a grid by one-step in-sample error."""
    if len(hist) < 3:
        return naive_last(hist, horizon)
    best_level, best_sse = hist[-1], np.inf
    for alpha in (0.1, 0.2, 0.3, 0.5, 0.7, 0.9):
        level, sse = hist[0], 0.0
        for y in hist[1:]:
            sse += (y - level) ** 2
            level = alpha * y + (1 - alpha) * level
        if sse < best_sse:
            best_sse, best_level = sse, level
    return np.repeat(best_level, horizon)


METHODS = {
    "naive_last": naive_last,
    "mean3": _mean_k(3),
    "mean6": _mean_k(6),
    "mean12": _mean_k(12),
    "prior_year": prior_year,
    "ses": ses,
}
