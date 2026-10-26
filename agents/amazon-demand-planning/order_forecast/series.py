"""Long monthly tables -> wide series matrices (rows = ASIN, columns = months)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def month_range(start, end) -> pd.DatetimeIndex:
    return pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="MS")


def to_wide(long: pd.DataFrame, key: str, value: str, months: pd.DatetimeIndex,
            mask_before_first: bool) -> pd.DataFrame:
    """One row per key, one column per month in `months`.

    Months with no row after the key first appears are 0 (nothing reported).
    If mask_before_first, months before the key first appears are NaN (the
    series did not exist yet); training and scoring skip NaN. Answer keys use
    mask_before_first=False so a missing month counts as 0 units.
    """
    df = long[[key, "month_start", value]].copy()
    df[value] = pd.to_numeric(df[value], errors="coerce").fillna(0.0)
    wide = df.pivot_table(index=key, columns="month_start", values=value, aggfunc="sum")
    wide = wide.reindex(columns=months)
    arr = np.nan_to_num(wide.to_numpy(dtype=float), nan=0.0)
    if mask_before_first:
        first = df.groupby(key)["month_start"].min().reindex(wide.index)
        mask = months.values[None, :] < first.values[:, None]
        arr[mask] = np.nan
    return pd.DataFrame(arr, index=wide.index, columns=months)
