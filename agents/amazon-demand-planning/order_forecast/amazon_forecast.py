"""Amazon's own weekly demand forecast, used as a cross-check (not a model input yet).

Why a cross-check and not a back-test: Amazon only returns its LATEST forecast, so we hold one version
per week from the day the ingest started. Every stored version looks forward from a date after our
last closed month, so none has actuals to score against yet. What we can do today is compare our
forecast for the coming months with Amazon's for the same ASINs and months and see where they agree.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .baselines import METHODS
from .metrics import wape
from .series import to_wide
from .snapshot import Snapshot

FORECAST_SQL = """SELECT r.forecast_generation_date, w.asin, w.week_start, w.week_end, w.weeks_ahead,
       w.mean_units, w.p70_units, w.p80_units, w.p90_units
FROM dbo.amazon_forecast_weekly w
JOIN dbo.amazon_forecast_run r ON r.forecast_run_id = w.forecast_run_id
WHERE r.marketplace = ? AND r.selling_program = 'RETAIL'
ORDER BY r.forecast_generation_date, w.asin, w.week_start"""

FILE = "amazon_forecast.csv"


def pull_forecast(snapshot_dir: Path, marketplace: str) -> Path:
    from . import db
    conn = db.connect()
    try:
        cols, rows = db.fetch(conn, FORECAST_SQL, (marketplace,))
    finally:
        conn.close()
    df = pd.DataFrame(rows, columns=cols)
    path = Path(snapshot_dir) / FILE
    df.to_csv(path, index=False)
    return path


def monthly(weekly: pd.DataFrame, generation_date=None) -> pd.DataFrame:
    """Weeks to months by the month of each week's Saturday (same rule as the ingest views).
    Only months whose Saturdays are all present are kept, so a part-covered last month is dropped."""
    w = weekly.copy()
    w["forecast_generation_date"] = pd.to_datetime(w["forecast_generation_date"])
    gen = pd.to_datetime(generation_date) if generation_date else w["forecast_generation_date"].max()
    w = w[w["forecast_generation_date"] == gen].copy()
    w["week_end"] = pd.to_datetime(w["week_end"])
    w["month_start"] = w["week_end"].dt.to_period("M").dt.to_timestamp()
    m = w.groupby(["asin", "month_start"], as_index=False).agg(amazon_mean=("mean_units", "sum"),
                                                               amazon_p90=("p90_units", "sum"), weeks=("week_end", "nunique"))
    m["generation_date"] = gen
    # a month is complete when every Saturday of that calendar month lies inside the forecast range
    first, last = w["week_end"].min(), w["week_end"].max()
    def complete(mo: pd.Timestamp) -> bool:
        sats = pd.date_range(mo, mo + pd.offsets.MonthEnd(0), freq="W-SAT")
        return len(sats) > 0 and sats.min() >= first and sats.max() <= last
    keep = [mo for mo in m["month_start"].unique() if complete(pd.Timestamp(mo))]
    return m[m["month_start"].isin(keep)].reset_index(drop=True)


def compare(snap: Snapshot, weekly: pd.DataFrame, target: str = "ordered_units", horizon: int = 6,
            min_history: int = 12, generation_date=None) -> dict:
    """Forecast from the end of our data with every baseline and set it beside Amazon's forecast."""
    am = monthly(weekly, generation_date)
    months = snap.months
    wide = to_wide(snap.sales, "asin", target, months, mask_before_first=True)
    first_future = months[-1] + pd.offsets.MonthBegin(1)
    future = [first_future + pd.offsets.MonthBegin(i) for i in range(horizon)]
    future = [f for f in future if f in set(am["month_start"])]
    if not future:
        raise SystemExit("Amazon's forecast does not cover the months after our last closed month.")
    rows = []
    for asin in wide.index:
        h = wide.loc[asin].to_numpy(float)
        h = h[~np.isnan(h)]
        if len(h) < min_history:
            continue
        for name, fn in METHODS.items():
            p = np.maximum(fn(h, horizon), 0.0)
            for i, mo in enumerate(future):
                rows.append((name, asin, mo, float(p[i])))
    ours = pd.DataFrame(rows, columns=["method", "asin", "month_start", "predicted"])
    j = ours.merge(am[["asin", "month_start", "amazon_mean", "amazon_p90"]], on=["asin", "month_start"], how="left")
    miss = j["amazon_mean"].isna()
    both = j[~miss]
    asin_level = pd.DataFrame([{"method": m, "n": len(g), "amazon_units": g["amazon_mean"].sum(),
                                "our_units": g["predicted"].sum(),
                                "wape_vs_amazon": wape(g["amazon_mean"], g["predicted"]),
                                "total_ratio": g["predicted"].sum() / g["amazon_mean"].sum()}
                               for m, g in both.groupby("method")]).sort_values("wape_vs_amazon")
    tot = both.groupby(["method", "month_start"], as_index=False)[["predicted", "amazon_mean"]].sum()
    tot["ratio"] = tot["predicted"] / tot["amazon_mean"]
    return {"asin_level": asin_level, "monthly_total": tot, "months": future, "generation_date": am["generation_date"].iloc[0],
            "asins_ours": int(ours["asin"].nunique()), "asins_matched": int(both["asin"].nunique()), "detail": j}
