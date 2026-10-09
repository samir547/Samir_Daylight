"""
POC scope filtering.

Narrows the full 49k-row training set down to the series we can actually
forecast for the pilot:

  1. Products on the active-product allow-list.
  2. Exclude Amazon channel rows (sell-in replenishment, not consumer demand).
  3. Series (item_code) with enough history (MIN_TRAIN_MONTHS of data strictly
     before the test window) to fit a seasonal model.
  4. Optionally (--pilot) only A-rated items, for a fast validation run.

Note on Amazon exclusion (step 2):
  Rows where channel = 'Amazon' in forecast_training_data represent bulk
  replenishment orders placed by Amazon on the client — not end-consumer
  purchases. The channel value is sourced from the Territory table and covers
  all Amazon BDM codes (AKB, AKB1, AKB2, AMUK) automatically. Filtering on
  channel rather than bdm_code is self-maintaining: new Amazon BDMs inherit
  channel = 'Amazon' from Territory without requiring a config change.
  Phase 2 will replace this signal with Vendor Central sell-out data
  (Shipped Units from the Sales_ASIN Vendor Central report).
"""
from __future__ import annotations

import logging

import pandas as pd

import config

logger = logging.getLogger(__name__)


def _train_end_ts() -> pd.Timestamp:
    return pd.Timestamp(config.TRAIN_END + "-01")


def filter_active_products(
    df: pd.DataFrame, active_products: pd.DataFrame
) -> pd.DataFrame:
    """Keep only rows whose item_code is on the active-product allow-list."""
    active = set(active_products["item_code"].astype(str).str.strip())
    out = df[df["item_code"].astype(str).str.strip().isin(active)].copy()
    logger.info(
        "Active-product filter: %s → %s rows (%s items)",
        f"{len(df):,}", f"{len(out):,}", f"{out['item_code'].nunique():,}",
    )
    return out


def filter_amazon_channel(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Remove rows where channel is in config.AMAZON_CHANNELS.

    Must be called on raw (before prepare()) because the channel column is
    dropped by prepare()'s groupby aggregation and is not available downstream.

    Amazon rows represent sell-in replenishment to Amazon fulfilment centres,
    not consumer demand. Prophet cannot learn reliable seasonality from them.
    Phase 2 will replace with Vendor Central sell-out data.

    Returns:
        (kept_df, excluded_df) — kept rows for training, excluded rows saved
        to output/ for inspection.
    """
    if "channel" not in df.columns:
        logger.warning(
            "filter_amazon_channel: 'channel' column not found in raw — "
            "skipping Amazon exclusion. Check that forecast_training_data "
            "includes the channel column."
        )
        return df.copy(), pd.DataFrame()

    exclude = set(str(c).strip() for c in config.AMAZON_CHANNELS)
    is_amazon = df["channel"].astype(str).str.strip().isin(exclude)

    excluded = df[is_amazon].copy()
    kept = df[~is_amazon].copy()

    excl_bdms = sorted(excluded["bdm_name"].dropna().unique().tolist()) if not excluded.empty else []
    excl_codes = sorted(excluded["bdm_code"].dropna().unique().tolist()) if not excluded.empty else []

    logger.info(
        "Amazon channel filter (channel in %s): %s rows removed | "
        "%s rows kept",
        sorted(exclude),
        f"{len(excluded):,}",
        f"{len(kept):,}",
    )
    if excl_bdms:
        logger.info("Excluded BDM names: %s", excl_bdms)
    if excl_codes:
        logger.info("Excluded BDM codes: %s", excl_codes)

    return kept, excluded


def detect_channel_mismatch(
    raw: pd.DataFrame, anchor_month: str
) -> tuple[set[str], pd.DataFrame]:
    """
    Flag items whose real demand has moved almost entirely into a channel this
    pipeline excludes from training (Amazon), leaving too little signal in the
    trained (non-Amazon) channel to produce a meaningful forecast.

    Must be called on raw (all channels) BEFORE filter_amazon_channel — the
    channel split is not available after that filter runs.

    An item is flagged if, in the trailing 12 months ending at anchor_month:
      - Amazon channel share of total volume >= config.CHANNEL_MISMATCH_MIN_AMAZON_SHARE
      - non-Amazon volume  < config.CHANNEL_MISMATCH_MAX_NON_AMAZON_UNITS (too
        thin to fit a seasonal model on)
      - total volume (all channels) >= config.CHANNEL_MISMATCH_MIN_TOTAL_UNITS
        (confirms this is a channel mismatch, not a genuinely low-volume or
        declining product — those should still get a low forecast, not be
        suppressed)

    Discovered from U35108 (2026-08): 99.6% Amazon share, 11 non-Amazon
    units/year against 2,971 total. See config.py for the full rationale.

    Returns:
        (excluded_item_codes, diagnostic_df) — diagnostic_df covers only the
        flagged items (mirrors the excluded_amazon_rows.csv convention) and is
        written to output/excluded_channel_mismatch.csv by main.py for audit.
    """
    if "channel" not in raw.columns:
        logger.warning(
            "detect_channel_mismatch: 'channel' column not found — skipping."
        )
        return set(), pd.DataFrame()

    anchor = pd.Timestamp(anchor_month + "-01")
    window_start = anchor - pd.DateOffset(months=11)

    df = raw.copy()
    df["ds"] = pd.to_datetime(df["year_month"] + "-01", errors="coerce")
    df["monthly_qty"] = pd.to_numeric(df["monthly_qty"], errors="coerce")
    recent = df[(df["ds"] >= window_start) & (df["ds"] <= anchor)].copy()

    amazon_set = set(str(c).strip() for c in config.AMAZON_CHANNELS)
    recent["is_amazon"] = recent["channel"].astype(str).str.strip().isin(amazon_set)

    totals = recent.groupby("item_code")["monthly_qty"].sum().rename("total_recent_12mo")
    amazon = (
        recent[recent["is_amazon"]]
        .groupby("item_code")["monthly_qty"].sum()
        .rename("amazon_recent_12mo")
    )
    diag = pd.concat([totals, amazon], axis=1).fillna(0.0).reset_index()
    diag["non_amazon_recent_12mo"] = diag["total_recent_12mo"] - diag["amazon_recent_12mo"]
    diag["amazon_share_recent"] = (
        diag["amazon_recent_12mo"] / diag["total_recent_12mo"]
    ).where(diag["total_recent_12mo"] > 0)

    flagged = diag[
        (diag["amazon_share_recent"] >= config.CHANNEL_MISMATCH_MIN_AMAZON_SHARE)
        & (diag["non_amazon_recent_12mo"] < config.CHANNEL_MISMATCH_MAX_NON_AMAZON_UNITS)
        & (diag["total_recent_12mo"] >= config.CHANNEL_MISMATCH_MIN_TOTAL_UNITS)
    ].copy()
    flagged["reason"] = (
        f"Amazon share >= {config.CHANNEL_MISMATCH_MIN_AMAZON_SHARE:.0%} of trailing "
        "12mo volume; non-Amazon remainder too thin to forecast reliably"
    )

    excluded_codes = set(flagged["item_code"].astype(str).str.strip())
    logger.info(
        "Channel-mismatch filter (trailing 12mo to %s): %d item(s) flagged: %s",
        anchor_month, len(excluded_codes), sorted(excluded_codes),
    )
    return excluded_codes, flagged


def forecastable_series(df: pd.DataFrame) -> pd.DataFrame:
    """
    Keep only series with >= MIN_TRAIN_MONTHS of history before the test window.

    `df` must carry a `ds` Timestamp column and config.GROUP_COLS.
    """
    train_end = _train_end_ts()
    train_mask = df["ds"] <= train_end
    counts = (
        df[train_mask]
        .groupby(config.GROUP_COLS)["ds"]
        .nunique()
        .rename("n_train_months")
        .reset_index()
    )
    keep = counts[counts["n_train_months"] >= config.MIN_TRAIN_MONTHS][config.GROUP_COLS]
    out = df.merge(keep, on=config.GROUP_COLS, how="inner")
    logger.info(
        "Forecastable filter (>=%d train months): %s → %s series (%s rows)",
        config.MIN_TRAIN_MONTHS,
        f"{counts.shape[0]:,}", f"{keep.shape[0]:,}", f"{len(out):,}",
    )
    return out


def filter_pilot(df: pd.DataFrame, bdm_df: pd.DataFrame) -> pd.DataFrame:
    """Restrict to A-rated items only (used by the --pilot flag)."""
    a_items = set(
        bdm_df.loc[
            bdm_df["rating"].astype(str).str.strip() == config.PILOT_RATING,
            "item_code",
        ].astype(str).str.strip()
    )
    out = df[df["item_code"].astype(str).str.strip().isin(a_items)].copy()
    n_series = out.groupby(config.GROUP_COLS).ngroups if not out.empty else 0
    logger.info(
        "Pilot filter (rating=%s): %s → %s rows (%s series)",
        config.PILOT_RATING, f"{len(df):,}", f"{len(out):,}", f"{n_series:,}",
    )
    return out


def apply_scope(
    df: pd.DataFrame,
    active_products: pd.DataFrame,
    bdm_df: pd.DataFrame,
    pilot: bool = False,
    channel_mismatch_excluded: set[str] | None = None,
) -> pd.DataFrame:
    """Run the full scope pipeline and log the funnel."""
    n_start = len(df)
    df = filter_active_products(df, active_products)

    if channel_mismatch_excluded:
        before = df["item_code"].nunique()
        df = df[
            ~df["item_code"].astype(str).str.strip().isin(channel_mismatch_excluded)
        ].copy()
        logger.info(
            "Channel-mismatch filter: excluded %d item(s), %s → %s items",
            len(channel_mismatch_excluded), before, df["item_code"].nunique(),
        )

    df = forecastable_series(df)
    if pilot:
        df = filter_pilot(df, bdm_df)

    n_series = df.groupby(config.GROUP_COLS).ngroups if not df.empty else 0
    logger.info(
        "Scope: %s rows → %s active items → %s forecastable series (%s rows)%s",
        f"{n_start:,}",
        f"{df['item_code'].nunique():,}",
        f"{n_series:,}",
        f"{len(df):,}",
        "  [pilot]" if pilot else "",
    )
    return df
