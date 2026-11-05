"""
Score delivered forecast batches against the latest actuals.

Production runs have no held-out test window, so this is how their accuracy is
tracked: every batch in dbo.forecast_output is compared with what actually sold,
and the result replaces dbo.forecast_delivered_accuracy. WAPE and bias are worked
out in dbo.vw_forecast_delivered_accuracy, so this table holds plain numbers.

WHAT IS SCORED
──────────────
  * One batch per first forecast month. If a month was published twice (--force),
    the newest batch is the one that was left in front of the business.
  * Every month of a batch that has actuals. A month that is partial or has not
    ended is kept with month_complete = 0 and the view ignores it. The next
    cycle replaces it.
  * Actuals exclude Amazon, like the forecast. A forecast item with no sales in a
    month scored an actual of 0: the training table has no zero rows.

The whole table is rebuilt each cycle from the latest actuals, so a late invoice
or a revised month flows through to every batch that covered that month.

RETIRED CODES
─────────────
A pooled family's forecast lands on its current codes, but some sales can still
arrive on a retired code. Those sales have no forecast row, so they are added as
rows with is_retired_code = 1, yhat 0 and model 'retired'. Item-level scoring
leaves them out; family-level scoring includes them, so the family total is
compared with all of the family's demand.

TRAINING BASIS AND HORIZON
──────────────────────────
A development batch was trained six months before its anchor (horizon 7-12) and a
production batch was trained to the anchor (horizon 1-6). forecast_output does not
record which, but a development run also writes dbo.forecast_accuracy under the
same run_date and a production run never does, so that tells them apart.

Run:  python -m evaluation.delivered_accuracy            # score and write
      python -m evaluation.delivered_accuracy --dry-run  # CSV only, no DB write
"""
from __future__ import annotations

import argparse
import logging
import sys

import numpy as np
import pandas as pd

import config
from analysis.month_completeness import current_month, month_completeness
from data import loader
from preprocessing import family_pool

logger = logging.getLogger(__name__)

BASIS_DEVELOPMENT = "development"
BASIS_PRODUCTION = "production"
RETIRED_MODEL = "retired"

# Months between the last training month and the first forecast month, minus one.
DEVELOPMENT_HORIZON_OFFSET = -config.OFFSETS_FROM_TEST_END["TRAIN_END"]

COLUMNS = [
    "run_date", "training_basis", "first_forecast_month", "item_code", "family_key",
    "family", "model", "year_month", "horizon", "is_retired_code", "yhat",
    "yhat_lower", "yhat_upper", "actual", "month_complete", "actuals_as_of",
]


def _month_index(ym: pd.Series) -> pd.Series:
    return ym.str[:4].astype(int) * 12 + ym.str[5:7].astype(int)


def item_actuals(raw: pd.DataFrame) -> pd.DataFrame:
    """item_code x year_month sales, Amazon removed to match the forecast."""
    df = raw.copy()
    df["item_code"] = df["item_code"].astype(str).str.strip()
    df["year_month"] = df["year_month"].astype(str).str.strip()
    df["actual"] = pd.to_numeric(df["monthly_qty"], errors="coerce").fillna(0.0)
    amazon = {str(c).strip() for c in config.AMAZON_CHANNELS}
    df = df[~df["channel"].astype(str).str.strip().isin(amazon)]
    return df.groupby(["item_code", "year_month"], as_index=False)["actual"].sum()


def delivered_batches(forecast: pd.DataFrame) -> pd.DataFrame:
    """Keep the newest batch for each first forecast month, with that month added."""
    df = forecast.copy()
    df["item_code"] = df["item_code"].astype(str).str.strip()
    df["year_month"] = df["year_month"].astype(str).str.strip()
    df["run_date"] = pd.to_datetime(df["run_date"])
    df["first_forecast_month"] = df.groupby("run_date")["year_month"].transform("min")
    newest = df.groupby("first_forecast_month")["run_date"].transform("max")
    return df[df["run_date"] == newest].copy()


def _retired_rows(batches: pd.DataFrame, actuals: pd.DataFrame,
                  families: family_pool.SuccessorFamilies) -> pd.DataFrame:
    """Sales on retired codes during each batch's months, for its pooled families."""
    retired = {
        code: key
        for key, members in families.members_by_family.items()
        for code in set(members) - set(families.current_codes_by_family[key])
    }
    if not retired:
        return pd.DataFrame()

    sold = actuals[actuals["item_code"].isin(retired) & (actuals["actual"] != 0)].copy()
    sold["family_key"] = sold["item_code"].map(retired)

    # One row per batch, family and month the batch forecast; the retired code's
    # sales join on to that. A family the batch did not forecast gets no row.
    cover = batches[["run_date", "training_basis", "first_forecast_month", "family_key",
                     "family", "year_month", "horizon", "month_complete"]]
    cover = cover.drop_duplicates(["run_date", "family_key", "year_month"])
    rows = cover.merge(sold, on=["family_key", "year_month"], how="inner")

    # A retired code that this batch forecast directly already has its own row.
    forecast_keys = batches[["run_date", "item_code"]].drop_duplicates().assign(_seen=True)
    rows = rows.merge(forecast_keys, on=["run_date", "item_code"], how="left")
    rows = rows[rows["_seen"].isna()].drop(columns="_seen")

    rows["model"] = RETIRED_MODEL
    rows["is_retired_code"] = True
    rows["yhat"] = 0.0
    rows["yhat_lower"] = np.nan
    rows["yhat_upper"] = np.nan
    return rows


def score_batches(
    forecast: pd.DataFrame,
    raw: pd.DataFrame,
    successor_map: pd.DataFrame,
    development_run_dates: pd.Series,
    as_of: pd.Timestamp,
    today: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """One row per delivered batch, item and month that has actuals.

    `forecast` is dbo.forecast_output's columns, `raw` the training extract with
    all channels, and `development_run_dates` the run_dates present in
    dbo.forecast_accuracy. Touches no database, so it can be tested on frames.
    """
    batches = delivered_batches(forecast)
    actuals = item_actuals(raw)
    families = family_pool.build_successor_families(successor_map)

    verdicts = month_completeness(raw).set_index("month")["is_complete"]
    this_month = current_month(today)

    # Months the extract has not reached have no actuals yet, so they are not scored.
    batches = batches[batches["year_month"].isin(verdicts.index)].copy()
    if batches.empty:
        return pd.DataFrame(columns=COLUMNS)

    batches["month_complete"] = (
        batches["year_month"].map(verdicts).astype(bool)
        & (batches["year_month"] < this_month)
    )
    is_dev = batches["run_date"].isin(pd.to_datetime(development_run_dates))
    batches["training_basis"] = np.where(is_dev, BASIS_DEVELOPMENT, BASIS_PRODUCTION)
    batches["horizon"] = (
        _month_index(batches["year_month"]) - _month_index(batches["first_forecast_month"])
        + 1 + np.where(is_dev, DEVELOPMENT_HORIZON_OFFSET, 0)
    )
    batches["family_key"] = batches["item_code"].map(families.family_key)
    batches["is_retired_code"] = False

    scored = batches.merge(actuals, on=["item_code", "year_month"], how="left")
    scored["actual"] = scored["actual"].fillna(0.0)

    retired = _retired_rows(batches, actuals, families)
    if not retired.empty:
        scored = pd.concat([scored, retired], ignore_index=True)

    scored["actuals_as_of"] = as_of
    return (scored[COLUMNS]
            .sort_values(["run_date", "year_month", "item_code"])
            .reset_index(drop=True))


def summarise(scored: pd.DataFrame) -> pd.DataFrame:
    """WAPE and bias per batch, complete months, item level. For the log only."""
    done = scored[scored["month_complete"] & ~scored["is_retired_code"]].copy()
    if done.empty:
        return pd.DataFrame()
    done["abs_error"] = (done["yhat"] - done["actual"]).abs()
    out = done.groupby(["first_forecast_month", "training_basis"], as_index=False).agg(
        months=("year_month", "nunique"), actual=("actual", "sum"),
        forecast=("yhat", "sum"), abs_error=("abs_error", "sum"),
    )
    out["wape_pct"] = (out["abs_error"] / out["actual"] * 100).round(1)
    out["bias_pct"] = ((out["forecast"] - out["actual"]) / out["actual"] * 100).round(1)
    return out.drop(columns="abs_error")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    ap = argparse.ArgumentParser(description="Score delivered forecast batches")
    ap.add_argument("--dry-run", action="store_true",
                    help="Write delivered_accuracy.csv only; leave the table unchanged")
    args = ap.parse_args(argv)

    forecast = loader.load_delivered_forecasts()
    if forecast.empty:
        logger.info("No delivered batches to score yet.")
        return 0

    as_of = pd.Timestamp.now(tz="UTC").tz_localize(None)
    scored = score_batches(
        forecast, loader.load_training_data(), loader.load_successor_map(),
        loader.load_development_run_dates(), as_of,
    )
    if scored.empty:
        logger.info("Delivered batches have no months with actuals yet.")
        return 0

    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = config.OUTPUT_DIR / "delivered_accuracy.csv"
    scored.to_csv(path, index=False)
    logger.info("Delivered accuracy → %s  (%s rows)\n%s", path, f"{len(scored):,}",
                summarise(scored).to_string(index=False))

    if not args.dry_run:
        loader.replace_delivered_accuracy(scored[COLUMNS])
    return 0


if __name__ == "__main__":
    sys.exit(main())
