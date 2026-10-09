"""
Database connection and data loading from Azure SQL.

Reads from the pre-built staging tables (forecast_training_data, BDM) rather
than reconstructing series from raw invoices.
"""
from __future__ import annotations

import logging
import time
import urllib.parse

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

import config
from data import queries

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 5

# Columns we expect from forecast_training_data; loading aborts if any are missing.
_TRAINING_REQUIRED_COLS = [
    "item_code", "bdm_name", "bdm_code", "region",
    "year_month", "monthly_qty", "channel",
]
_BDM_REQUIRED_COLS = [
    "item_code", "bdm_code", "region", "rating", "forecast_year",
    "month_name", "bdm_forecast_qty",
]
_SUCCESSOR_REQUIRED_COLS = [
    "old_item_code", "new_item_code", "source", "status", "estimated_changeover",
]

# Columns each write-back table must have (see queries.CREATE_FORECAST_*).
# write_outputs() checks the LIVE tables against these before writing anything,
# so a table left over at an older grain (e.g. one still carrying bdm_name and
# lacking `family`) fails with a clear message instead of a cryptic insert error.
_EXPECTED_OUTPUT_COLUMNS = {
    "forecast_output": [
        "item_code", "family", "year_month", "yhat", "yhat_lower", "yhat_upper",
        "model", "run_kind", "run_date",
    ],
    "forecast_accuracy": [
        "item_code", "family", "model", "mae", "rmse", "mape_pct",
        "n_train_months", "n_test_months", "run_date",
    ],
}


def _get_engine():
    # DB_USER / DB_PASSWORD have no defaults in config.py — fail fast and
    # clearly if .env (or the process environment) did not supply them.
    missing = [n for n in ("DB_NAME", "DB_USER", "DB_PASSWORD")
               if not getattr(config, n)]
    if missing:
        raise ValueError(
            f"Missing required database setting(s): {', '.join(missing)}.\n"
            "Set them in prophet_forecast/.env (see .env.example) or as "
            "environment variables."
        )
    conn_str = (
        f"DRIVER={{{config.DB_DRIVER}}};"
        f"SERVER={config.DB_SERVER},{config.DB_PORT};"
        f"DATABASE={config.DB_NAME};"
        f"UID={config.DB_USER};"
        f"PWD={config.DB_PASSWORD};"
        "Encrypt=yes;"
        "TrustServerCertificate=no;"
        "Connection Timeout=30;"
    )
    params = urllib.parse.quote_plus(conn_str)
    return create_engine(
        f"mssql+pyodbc:///?odbc_connect={params}",
        fast_executemany=True,
    )


def _read_sql(query: str, label: str, params: dict | None = None) -> pd.DataFrame:
    """Run a query with simple connection-level retry/backoff.

    `params` are bound as SQLAlchemy named parameters (`:name` in the SQL),
    never interpolated into the query string.
    """
    engine = _get_engine()
    last_err: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            with engine.connect() as conn:
                return pd.read_sql(text(query), conn, params=params)
        except OperationalError as err:
            last_err = err
            wait = _RETRY_BACKOFF_SECONDS * attempt
            logger.warning(
                "DB connection failed loading %s (attempt %d/%d): %s — retrying in %ds",
                label, attempt, _MAX_RETRIES, err, wait,
            )
            time.sleep(wait)
    raise RuntimeError(f"Failed to load {label} after {_MAX_RETRIES} attempts") from last_err


def _validate(df: pd.DataFrame, required: list[str], label: str) -> None:
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"{label}: missing expected columns {missing}. Got {list(df.columns)}")
    if df.empty:
        raise ValueError(f"{label}: query returned no rows.")


# ── Public loaders ────────────────────────────────────────────────────────────

def load_training_data() -> pd.DataFrame:
    """Load the clean monthly training series."""
    logger.info("Connecting to %s / %s …", config.DB_SERVER, config.DB_NAME)
    df = _read_sql(queries.TRAINING_DATA, "forecast_training_data")
    _validate(df, _TRAINING_REQUIRED_COLS, "forecast_training_data")
    logger.info(
        "Loaded %s training rows | %s → %s | %s items",
        f"{len(df):,}", df["year_month"].min(), df["year_month"].max(),
        f"{df['item_code'].nunique():,}",
    )
    return df


def load_bdm_forecasts() -> pd.DataFrame:
    """Load BDM manual forecasts, one row per (item, bdm, month, year).

    No year filter, and no year parameter bound: [dbo].[BDM] carries more than
    one planning year at a time (2026 and 2027 both present as of 2026-08), and
    every row keeps its own `forecast_year` — evaluation.benchmark's
    _bdm_month_col() builds its join key from that column per row, so
    multi-year data no longer needs to be filtered down to one year before it's
    safe to use.
    """
    df = _read_sql(queries.BDM_FORECASTS, "BDM")
    _validate(df, _BDM_REQUIRED_COLS, "BDM")
    years = sorted(
        {int(y) for y in pd.to_numeric(df["forecast_year"], errors="coerce").dropna()}
    )
    if not years:
        raise ValueError("BDM: no valid forecast_year values found in the pulled data.")
    logger.info(
        "Loaded %s BDM forecast rows spanning year(s) %s | %s items | %s ratings",
        f"{len(df):,}", years, f"{df['item_code'].nunique():,}",
        sorted(df["rating"].dropna().unique().tolist()),
    )
    return df


def load_active_products() -> pd.DataFrame:
    """Load the active-product allow-list (one column: item_code).

    The recency cut-off is config.ACTIVE_SINCE, bound as :active_since. It is a
    static value (not rolled by --auto-window) — bump it in config.py by hand.
    """
    df = _read_sql(queries.ACTIVE_PRODUCTS, "active_products",
                   params={"active_since": config.ACTIVE_SINCE})
    _validate(df, ["item_code"], "active_products")
    logger.info("Loaded %s active products (trading since %s)",
                f"{len(df):,}", config.ACTIVE_SINCE)
    return df


def load_master_product() -> pd.DataFrame:
    """Load item_code → family (Product Family) reporting tags."""
    df = _read_sql(queries.MASTER_PRODUCT, "master_product")
    _validate(df, ["item_code", "family"], "master_product")
    # Belt-and-braces: collapse any residual duplicate item_code to one row so the
    # downstream family join is guaranteed 1:1 and cannot fan out output rows.
    df["item_code"] = df["item_code"].astype(str).str.strip()
    df = df.drop_duplicates(subset=["item_code"], keep="first")
    logger.info(
        "Loaded %s master-product family tags | %s distinct families",
        f"{len(df):,}", f"{df['family'].nunique():,}",
    )
    return df


def load_successor_map() -> pd.DataFrame:
    """Load the old → new product-code successor relation, with review context.

    One row per old→new pairing, carrying the parent review row's `status` and
    `estimated_changeover`. Consumed by preprocessing.family_pool to pool a
    retired code's history with its successors' for training.
    """
    df = _read_sql(queries.SUCCESSOR_MAP, "product_successor_map")
    _validate(df, _SUCCESSOR_REQUIRED_COLS, "product_successor_map")
    for col in ("old_item_code", "new_item_code"):
        df[col] = df[col].astype(str).str.strip()
    logger.info(
        "Loaded %s successor pairings | %s retired codes → %s successor codes | statuses %s",
        f"{len(df):,}",
        f"{df['old_item_code'].nunique():,}", f"{df['new_item_code'].nunique():,}",
        sorted(df["status"].dropna().unique().tolist()),
    )
    return df


# ── Optional write-back ───────────────────────────────────────────────────────

def _check_output_schema(conn) -> None:
    """Fail clearly if a write-back table is missing a column the writer needs.

    The CREATE statements are IF-NOT-EXISTS, so a table left over from an older
    grain (bdm_name, no `family`) is silently kept and would only blow up later
    inside the insert. Missing columns are an error; extra columns are only
    logged (someone may have added one on purpose, and an INSERT that names its
    columns is unaffected by them).
    """
    problems: list[str] = []
    for table, expected in _EXPECTED_OUTPUT_COLUMNS.items():
        rows = conn.execute(
            text("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                 "WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = :t"),
            {"t": table},
        ).fetchall()
        actual = {str(r[0]).lower() for r in rows}
        missing = [c for c in expected if c.lower() not in actual]
        extra = sorted(actual - {c.lower() for c in expected})
        if missing:
            problems.append(
                f"dbo.{table} is missing column(s) {missing}"
                + (f" and still has {extra}" if extra else "")
            )
        elif extra:
            logger.warning("dbo.%s has extra column(s) not written by this "
                           "pipeline: %s", table, extra)
    if problems:
        raise RuntimeError(
            "Write-back table schema does not match the item-grain layout: "
            + "; ".join(problems)
            + ". If these are tables from the old item × BDM grain, drop "
              "dbo.forecast_output / dbo.forecast_accuracy once and re-run "
              "(see the note above queries.CREATE_FORECAST_OUTPUT). Nothing "
              "was written."
        )


def write_outputs(forecast_df: pd.DataFrame, metrics_df: pd.DataFrame) -> None:
    """Create target tables if needed and append the run's results.

    Both tables are append-only — every --write-db run adds a new set of rows
    rather than replacing the previous run's. `run_date` is what makes that
    usable: it is computed ONCE per call and stamped on every row written in
    this invocation, to both tables, so a consumer can always recover "the
    latest forecast" with `WHERE run_date = (SELECT MAX(run_date) FROM ...)`,
    and so a forecast_output row and the forecast_accuracy row that produced it
    share the same run_date and can be joined/correlated after the fact.
    Naive UTC, not local time or tz-aware — SQL Server DATETIME2 has no offset
    of its own, and a tz-aware pandas Timestamp round-trips awkwardly through
    pyodbc.

    ALL of it — table creation, the schema check, and both inserts — runs in ONE
    transaction. Either the whole run lands in both tables or none of it does;
    a failure part-way can no longer leave a forecast_output batch with no
    matching forecast_accuracy rows (or vice versa), which would make
    vw_forecast_output_latest ("MAX(run_date)") serve a half-written run.
    """
    engine = _get_engine()
    run_date = pd.Timestamp.utcnow().tz_localize(None)

    with engine.begin() as conn:
        conn.execute(text(queries.CREATE_FORECAST_OUTPUT))
        conn.execute(text(queries.CREATE_FORECAST_ACCURACY))
        _check_output_schema(conn)

        if not forecast_df.empty:
            out = forecast_df.copy()
            if "ds" in out.columns:
                out["year_month"] = pd.to_datetime(out["ds"]).dt.strftime("%Y-%m")
            out["run_date"] = run_date
            keep = [c for c in ["item_code", "family", "year_month",
                                "yhat", "yhat_lower", "yhat_upper", "model", "run_kind",
                                "run_date"]
                    if c in out.columns]
            out[keep].to_sql("forecast_output", conn, if_exists="append", index=False)
            logger.info("Wrote %s rows to dbo.forecast_output (run_date=%s)",
                        f"{len(out):,}", run_date)

        if not metrics_df.empty:
            # model_metrics.csv carries no `model` column of its own — it is
            # Prophet-only by construction (naive routes have no fit to report;
            # see main.py step 5c). Stamped here, at the write boundary, rather
            # than upstream, so forecast_accuracy stays self-describing even
            # though the in-memory frame doesn't need the column for anything else.
            acc = metrics_df.copy()
            if "model" not in acc.columns:
                acc["model"] = "Prophet"
            acc["run_date"] = run_date
            keep = [c for c in ["item_code", "family", "model", "mae", "rmse", "mape_pct",
                                "n_train_months", "n_test_months", "run_date"]
                    if c in acc.columns]
            acc[keep].to_sql("forecast_accuracy", conn, if_exists="append", index=False)
            logger.info("Wrote %s rows to dbo.forecast_accuracy (run_date=%s)",
                        f"{len(acc):,}", run_date)
