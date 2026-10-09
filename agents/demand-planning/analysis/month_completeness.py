"""
"Is this month's extract complete?" — shared by Phase 1 scoring and Phase 3
window proposal.

WHY MAX(year_month) IS NOT ENOUGH
─────────────────────────────────
`forecast_training_data` is refreshed by an upstream pull, and its most recent
month is routinely a *partial* month: the pull ran part-way through, so the
month is present but under-filled. 2026-07 in the current extract carries 622
transactions against ~1,400–1,600 in the months either side of it. Rolling
`TRAIN_END` onto a month like that would train every series with a fabricated
50% cliff at its most recent point — precisely where Prophet's trend fit is
most sensitive.

WHY A TRAILING-MEDIAN THRESHOLD DOES NOT WORK HERE
──────────────────────────────────────────────────
The obvious check — "transactions vs. the median of the prior 6 months" — was
tried first and rejected. This business has a hard seasonal trough in Apr–Jun:
genuine, fully-loaded months routinely sit at 52–70% of their own trailing
median, and a threshold loose enough to admit them (<0.52) no longer excludes
anything. Measured over 2018-01..2026-07 that rule flagged 11 complete months
alongside the one real partial. It conflates seasonality with truncation.

THE CHECK THAT DOES WORK
────────────────────────
Compare each month to the SAME CALENDAR MONTH a year earlier — which cancels
seasonality outright — and then divide out the business's overall growth so a
year of genuine expansion or decline does not shift the verdict:

    seasonal_txn_ratio =  (txns[m] / txns[m-12])
                          ────────────────────────────────────────────
                          (txns over the 12 months before m)
                          / (txns over the 12 months before that)

Both terms of the denominator are windows strictly *before* the candidate, so
the check is causal: it returns the same verdict whenever it is run, and cannot
be rescued or broken by data that arrives later.

Over the full history this separates cleanly. The worst genuine month scores
0.70; the known partial month scores 0.46. `MIN_SEASONAL_TXN_RATIO = 0.60` sits
in that gap, and flags exactly one month in eight and a half years.

Two loose structural backstops catch a truncation severe enough to lose whole
SKUs (which the ratio alone could in principle miss on a low-transaction SKU
mix). Neither fires on any genuine month in the history — they are there for a
failure mode that has not happened yet, not to do the day-to-day work.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Primary gate: seasonally- and growth-adjusted transaction ratio. Calibrated on
# 2018-01..2026-07 — worst genuine month 0.70, known partial pull 0.46.
MIN_SEASONAL_TXN_RATIO = 0.60

# Structural backstops (see module docstring). Observed genuine minima across the
# same history: item_ratio 0.855, regular_cover 0.847. Both sit clear of these.
MIN_ITEM_RATIO = 0.70       # distinct items vs. median of the prior 6 months
MIN_REGULAR_COVER = 0.75    # share of prior-6-month regulars appearing this month

BASELINE_MONTHS = 6         # window for the two structural backstops
AMAZON_CHANNEL = "Amazon"


def _monthly_facts(raw: pd.DataFrame) -> pd.DataFrame:
    """Amazon-filtered raw rows with normalised month / numeric columns.

    Amazon rows are excluded so the check measures the same population the
    pipeline actually trains on (scope.filter_amazon_channel drops them before
    prepare()); a lumpy Amazon replenishment landing in one month would
    otherwise mask or manufacture a completeness signal.
    """
    df = raw.copy()
    df["item_code"] = df["item_code"].astype(str).str.strip()
    df["month"] = df["year_month"].astype(str).str.strip()
    for col in ("monthly_qty", "transaction_count"):
        df[col] = pd.to_numeric(df.get(col), errors="coerce").fillna(0.0)
    if "channel" in df.columns:
        df = df[df["channel"].astype(str).str.strip() != AMAZON_CHANNEL]
    return df


def _seasonal_txn_ratio(txns: pd.Series) -> pd.DataFrame:
    """Same-month-last-year transaction ratio, divided by the growth level.

    `txns` must be indexed by contiguous 'YYYY-MM' months in ascending order.
    Every window used is strictly before the candidate month.
    """
    t12 = txns.rolling(12).sum()
    prior_12 = t12.shift(1)         # months m-12 .. m-1
    prior_24_13 = t12.shift(13)     # months m-24 .. m-13
    level = prior_12 / prior_24_13.replace(0, np.nan)
    yoy = txns / txns.shift(12).replace(0, np.nan)
    return pd.DataFrame({
        "txns_last_year": txns.shift(12),
        "yoy_txn_ratio": yoy,
        "growth_level": level,
        "seasonal_txn_ratio": yoy / level.replace(0, np.nan),
    })


def month_completeness(raw: pd.DataFrame) -> pd.DataFrame:
    """
    One row per month with each completeness signal and a verdict.

    `raw` is the un-prepared extract (output/raw_data.csv, or
    loader.load_training_data()) — it must still carry `transaction_count` and
    `channel`, both of which prepare() drops.

    Months without enough history to compute the primary signal (the first ~25)
    are reported as complete with reason 'insufficient baseline'. They are
    decades away from any TRAIN_END candidate; the alternative — calling them
    incomplete — would be a false accusation, not a safeguard.
    """
    df = _monthly_facts(raw)

    agg = (
        df.groupby("month")
        .agg(rows=("item_code", "size"),
             items=("item_code", "nunique"),
             txns=("transaction_count", "sum"),
             qty=("monthly_qty", "sum"))
        .sort_index()
    )
    if agg.empty:
        return pd.DataFrame(columns=["month", "is_complete", "reason"])

    ratios = _seasonal_txn_ratio(agg["txns"])
    present = df.groupby("month")["item_code"].agg(set)
    months = list(agg.index)

    recs = []
    for i, m in enumerate(months):
        prior = months[max(0, i - BASELINE_MONTHS):i]

        item_ratio, cover, n_reg = np.nan, np.nan, 0
        if len(prior) == BASELINE_MONTHS:
            base_items = agg.loc[prior, "items"].median()
            item_ratio = agg.loc[m, "items"] / base_items if base_items else np.nan

            # Regulars: traded in >=5 of the prior 6 months.
            counts: dict[str, int] = {}
            for pm in prior:
                for code in present[pm]:
                    counts[code] = counts.get(code, 0) + 1
            regulars = {c for c, n in counts.items() if n >= 5}
            n_reg = len(regulars)
            if regulars:
                cover = len(regulars & present[m]) / len(regulars)

        seasonal = ratios.loc[m, "seasonal_txn_ratio"]

        if pd.isna(seasonal):
            recs.append({
                "month": m, "seasonal_txn_ratio": np.nan,
                "yoy_txn_ratio": round(ratios.loc[m, "yoy_txn_ratio"], 3)
                if pd.notna(ratios.loc[m, "yoy_txn_ratio"]) else np.nan,
                "item_ratio": round(item_ratio, 3) if pd.notna(item_ratio) else np.nan,
                "regular_cover": round(cover, 3) if pd.notna(cover) else np.nan,
                "n_regulars": n_reg, "is_complete": True,
                "reason": "insufficient baseline (needs 24 prior months) — not assessed",
            })
            continue

        fails = []
        if seasonal < MIN_SEASONAL_TXN_RATIO:
            fails.append(
                f"seasonally-adjusted transactions {seasonal:.0%} of expected "
                f"(<{MIN_SEASONAL_TXN_RATIO:.0%})"
            )
        if pd.notna(item_ratio) and item_ratio < MIN_ITEM_RATIO:
            fails.append(f"distinct items {item_ratio:.0%} of trailing median "
                         f"(<{MIN_ITEM_RATIO:.0%})")
        if pd.notna(cover) and cover < MIN_REGULAR_COVER:
            fails.append(f"regular-item cover {cover:.0%} (<{MIN_REGULAR_COVER:.0%})")

        recs.append({
            "month": m,
            "seasonal_txn_ratio": round(seasonal, 3),
            "yoy_txn_ratio": round(ratios.loc[m, "yoy_txn_ratio"], 3),
            "item_ratio": round(item_ratio, 3) if pd.notna(item_ratio) else np.nan,
            "regular_cover": round(cover, 3) if pd.notna(cover) else np.nan,
            "n_regulars": n_reg,
            "is_complete": not fails,
            "reason": "complete" if not fails else "; ".join(fails),
        })

    return pd.DataFrame(recs).merge(agg.reset_index(), on="month", how="left")[
        ["month", "rows", "items", "txns", "qty", "yoy_txn_ratio",
         "seasonal_txn_ratio", "item_ratio", "regular_cover", "n_regulars",
         "is_complete", "reason"]
    ]


def latest_complete_month(raw: pd.DataFrame) -> tuple[str, pd.DataFrame]:
    """
    The most recent month that passes the completeness check, plus the evidence.

    Returns the newest passing month, so a run of trailing partial months is
    skipped rather than only the last one.
    """
    ev = month_completeness(raw)
    complete = ev[ev["is_complete"]]
    if complete.empty:
        raise ValueError("No month in the extract passes the completeness check.")
    return complete["month"].max(), ev
