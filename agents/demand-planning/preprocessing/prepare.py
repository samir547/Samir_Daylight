"""
Data preparation: type coercion, date conversion, and aggregation to the
modelling grain (config.GROUP_COLS × month).
"""
from __future__ import annotations

import logging

import pandas as pd

import config

logger = logging.getLogger(__name__)


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """
    Coerce types, build a month-start `ds` Timestamp, and aggregate monthly_qty
    to `y` at the modelling grain (config.GROUP_COLS x month).

    bdm_code and region are NOT carried through — they are dropped by the groupby.
    The benchmark join uses series_metadata(raw), which is called in main() before
    this function and captures bdm_code/region directly from raw.

    Returns a frame with columns: config.GROUP_COLS + [ds, y].
    """
    df = df.copy()
    df["monthly_qty"] = pd.to_numeric(df["monthly_qty"], errors="coerce").fillna(0.0)
    df["ds"] = pd.to_datetime(df["year_month"].astype(str) + "-01", errors="coerce")

    bad = df["ds"].isna().sum()
    if bad:
        logger.warning("Dropping %s rows with unparseable year_month", f"{bad:,}")
    df = df.dropna(subset=["ds"])

    # Keep only the train + test window; the staging table runs slightly beyond.
    train_start = pd.Timestamp(config.TRAIN_START + "-01")
    test_end = pd.Timestamp(config.TEST_END + "-01")
    df = df[(df["ds"] >= train_start) & (df["ds"] <= test_end)]

    agg_cols = config.GROUP_COLS + ["ds"]
    out = (
        df.groupby(agg_cols, as_index=False)["monthly_qty"]
        .sum()
        .rename(columns={"monthly_qty": "y"})
    )

    logger.info(
        "Prepared %s rows across %s series | %s → %s",
        f"{len(out):,}",
        f"{out.groupby(config.GROUP_COLS).ngroups:,}",
        out["ds"].min().date(), out["ds"].max().date(),
    )
    return out


def series_metadata(df: pd.DataFrame) -> pd.DataFrame:
    """
    Build an item_code → region lookup from the raw rows, for reporting
    breakdowns in the benchmark.

    Grain note: the modelling grain is now item_code only (BDM dropped), so the
    benchmark compares an item's summed-across-BDMs Prophet forecast against the
    item's summed-across-BDMs BDM manual forecast. bdm_code is therefore no
    longer a join key and is not carried here. region is taken as `first`; a
    handful of cross-region items collapse to one region for the breakdown only
    (it never enters the forecast or the MAPE maths).
    """
    cols = config.GROUP_COLS + ["region"]
    meta = (
        df[cols]
        .dropna(subset=config.GROUP_COLS)
        .groupby(config.GROUP_COLS, as_index=False)
        .agg({"region": "first"})
    )
    return meta
