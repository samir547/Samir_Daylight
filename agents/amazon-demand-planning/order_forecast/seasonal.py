"""Level x seasonal-factor forecast, hand-built because 3 years of monthly data is too little for
a per-ASIN seasonal model but enough to learn one shared seasonal shape.

At each forecast origin, using only months before it:
  1. Seasonal factor per calendar month, from the TOTAL across all ASINs: each month divided by its own
     centred 12-month average (removes trend), averaged over the years available, scaled to mean 1.
  2. Level per ASIN: the average of its last 12 months after dividing each by that month's factor.
  3. Forecast = level x factor of the target month.
`shrink` pulls the factors towards 1 (0 = no seasonality, 1 = full), because each month has only
two or three observations.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def seasonal_factors(total: pd.Series, shrink: float = 1.0) -> np.ndarray:
    """Return 12 factors indexed by calendar month - 1. `total` is a monthly series (DatetimeIndex, MS)."""
    t = total.astype(float)
    if len(t) < 24:
        return np.ones(12)
    ma = t.rolling(12, center=True).mean().rolling(2).mean().shift(-1)      # 2x12 centred moving average
    ratio = (t / ma).replace([np.inf, -np.inf], np.nan).dropna()
    if ratio.empty:
        return np.ones(12)
    f = ratio.groupby(ratio.index.month).mean().reindex(range(1, 13))
    f = f.fillna(1.0).to_numpy()
    f = f / f.mean()
    return 1.0 + shrink * (f - 1.0)


def forecast_seasonal(wide: pd.DataFrame, origins: list[int], horizon: int, min_history: int,
                      shrink: float = 1.0, name: str = "seasonal") -> pd.DataFrame:
    months, values = wide.columns, wide.to_numpy(dtype=float)
    out = []
    for t in origins:
        total = pd.Series(np.nansum(values[:, :t], axis=0), index=months[:t])
        f = seasonal_factors(total, shrink)
        for i, asin in enumerate(wide.index):
            hist = values[i, :t]
            ok = ~np.isnan(hist)
            if ok.sum() < min_history:
                continue
            idx = np.where(ok)[0][-12:]
            fac = f[np.array([months[k].month - 1 for k in idx])]
            level = float(np.mean(hist[idx] / np.maximum(fac, 0.05)))
            for h in range(horizon):
                tm = months[t + h]
                out.append((name, months[t - 1], tm, h + 1, asin, max(level * f[tm.month - 1], 0.0)))
    return pd.DataFrame(out, columns=["method", "train_end", "target_month", "step", "asin", "predicted"])


VARIANTS = {"seasonal": 1.0, "seasonal_half": 0.5}
