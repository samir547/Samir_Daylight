"""
BDM benchmark comparison.

Compares whichever model actually forecast a given item — Prophet for the
Smooth/Erratic series it fits, Naive-3mo / Naive-12mo for the Lumpy and
Intermittent ones routed to a trailing average — against:
  * the BDM manual forecast (from the BDM sheet), and
  * a prior-year same-month baseline,
on the overlap between the held-out test window and whatever the BDM sheet
actually covers (config.BENCHMARK_START..BENCHMARK_END — static in config.py by
default, or rolled automatically by `--auto-window`; see
main.apply_auto_window()).

The comparison is three-way per row, not per file: `test_df` now arrives with a
`model` column naming its forecaster, so `model_forecast` / `model_mape` mean
"that item's model", and `winner` can come back as any of the model labels, the
BDM, or the baseline.

RENAME (2026-08) — "naive" baseline is now "prior_year"
───────────────────────────────────────────────────────
The same-month-last-year baseline used to be called `naive` throughout this
module (naive_forecast, naive_mape, _naive_lookup). It has been renamed to
`prior_year` everywhere, deliberately and not silently: "Naive" is now also the
name of a real routed MODEL family (Naive-3mo / Naive-12mo from
`models/naive_model.py`), and a `winner` column that could read "naive" for the
baseline and "Naive-3mo" for a model would be unreadable. Nothing about the
baseline's arithmetic changed — only its name. Downstream readers of
`benchmark_comparison.csv` (and any saved Power BI column mapping) need the new
column names.

GRAIN CHANGE (2026-07) — comparison basis re-aggregated to item level
─────────────────────────────────────────────────────────────────────
Prophet now forecasts at item_code grain (BDM dropped from the modelling
grain). The BDM manual-forecast sheet is still authored per item_code × bdm_code
(each BDM forecasts their own territory). To keep a like-for-like comparison at
the NEW grain, the BDM manual forecast is SUMMED across all bdm_codes for an
item within each month before it is compared to Prophet's item-level forecast.
The prior-year baseline is likewise item-level (its source, prepared_df, is already
aggregated to item_code by prepare()).

This re-aggregation is a deliberate choice, not a silent one — see
`family-tagging-grain-change.md`, step 2. Rationale: the sum of the per-BDM
manual forecasts is the sales team's total demand call for the item, which is
the correct comparator for an item-level model. The alternative (comparing the
item model against a single arbitrary BDM's sheet) would be wrong. bdm_code is
therefore no longer a join key; per-BDM win-rate breakdowns are gone because the
model no longer produces a per-BDM number.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

import config

logger = logging.getLogger(__name__)

_MONTH_NUM = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def bdm_coverage_months(bdm_df: pd.DataFrame) -> list[str]:
    """Sorted list of 'YYYY-MM' months actually present in bdm_df.

    Keyed off each row's own (forecast_year, month_name) via `_bdm_month_col()`
    — the single source of truth for turning the BDM sheet's wide year/month
    columns into a comparable month string. Used by main.apply_auto_window() to
    roll BENCHMARK_START/END to the overlap between the rolled TEST window and
    whatever the sheet actually covers, and by check_bdm_coverage() to spot
    gaps in that (rolled or static) window.
    """
    return sorted(_bdm_month_col(bdm_df)["month"].unique().tolist())


def check_bdm_coverage(bdm_df: pd.DataFrame) -> None:
    """Warn when the benchmark window outruns the BDM sheet's actual coverage.

    Replaces the old BDM_FORECAST_YEAR alignment check now that bdm_df can hold
    more than one planning year at once and `_bdm_month_col()` keys off each
    row's own forecast_year rather than a single hand-maintained constant —
    there is no more "wrong year" failure mode to check for. What CAN still go
    wrong is a BENCHMARK_START..BENCHMARK_END month with no real BDM rows behind
    it at all (a gap in the sheet, or a window rolled past what has been
    entered so far). That doesn't raise — the merge in build_comparison() just
    leaves bdm_forecast NaN for that month and it quietly falls out of every
    BDM-side aggregate — so this says so loudly instead, per missing month,
    rather than leaving it to be noticed later as an unexplained gap in the
    numbers.
    """
    covered = set(bdm_coverage_months(bdm_df))
    window_months = (
        pd.period_range(config.BENCHMARK_START, config.BENCHMARK_END, freq="M")
        .strftime("%Y-%m").tolist()
    )
    missing = [m for m in window_months if m not in covered]
    if missing:
        logger.error(
            "BDM benchmark window %s..%s has no BDM data at all for %s — those "
            "month(s)' BDM comparison will come back empty (NaN forecast, "
            "excluded from the BDM side of WAPE/bias/win-rate). Sheet coverage "
            "found: %s..%s.",
            config.BENCHMARK_START, config.BENCHMARK_END, missing,
            min(covered) if covered else "none", max(covered) if covered else "none",
        )


def _row_mape(actual: float, forecast: float) -> float:
    """Single-point absolute percentage error (%); NaN when actual is 0/NaN."""
    if pd.isna(actual) or pd.isna(forecast) or actual == 0:
        return float("nan")
    return abs(actual - forecast) / actual * 100


def _bdm_month_col(bdm_df: pd.DataFrame) -> pd.DataFrame:
    """Map each row's own (forecast_year, month_name) to a 'YYYY-MM' string.

    Built from the row's own `forecast_year` column, not a global constant —
    this is what makes 2026-Feb and 2027-Feb distinguishable now that bdm_df
    can hold more than one planning year at once (queries.BDM_FORECASTS /
    loader.load_bdm_forecasts() no longer filter to a single year). Rows with
    an unparseable month or year are dropped rather than mis-keyed.
    """
    out = bdm_df.copy()
    out["month_num"] = out["month_name"].map(_MONTH_NUM)
    out["forecast_year"] = pd.to_numeric(out["forecast_year"], errors="coerce")
    out = out.dropna(subset=["month_num", "forecast_year"])
    out["month"] = (
        out["forecast_year"].astype(int).astype(str)
        + "-" + out["month_num"].astype(int).astype(str).str.zfill(2)
    )
    return out


def _prior_year_lookup(prepared_df: pd.DataFrame) -> dict:
    """{(item_code, 'YYYY-MM'): actual_y} for prior-year baseline lookups.

    prepared_df is already at item_code × month grain (prepare() aggregates
    across BDMs), so this is the item-level actual for the same-month-last-year
    baseline.
    """
    tmp = prepared_df.copy()
    tmp["month"] = pd.to_datetime(tmp["ds"]).dt.strftime("%Y-%m")
    return {
        (r.item_code, r.month): r.y
        for r in tmp.itertuples(index=False)
    }


def build_comparison(
    test_df: pd.DataFrame,
    bdm_df: pd.DataFrame,
    prepared_df: pd.DataFrame,
    series_meta: pd.DataFrame,
) -> pd.DataFrame:
    """
    Build the per-item-month benchmark table (item grain).

    Columns: item_code, region, rating, month, model, actual, model_forecast,
    bdm_forecast, prior_year_forecast, model_mape, bdm_mape, prior_year_mape,
    winner.

    `model_forecast` is whatever model forecast that item, named by the row's own
    `model` column (Prophet / Naive-3mo / Naive-12mo) — test_df is no longer a
    Prophet-only frame, so nothing here assumes one forecaster.

    The BDM manual forecast is summed across all bdm_codes for each item before
    comparison (see module docstring). series_meta supplies region for reporting.
    """
    check_bdm_coverage(bdm_df)

    if test_df.empty:
        logger.warning("Benchmark: empty test_df, nothing to compare.")
        return pd.DataFrame()

    df = test_df.copy()
    df["month"] = pd.to_datetime(df["ds"]).dt.strftime("%Y-%m")

    # Restrict to the BDM-overlap window.
    df = df[(df["month"] >= config.BENCHMARK_START) & (df["month"] <= config.BENCHMARK_END)]
    if df.empty:
        logger.warning("Benchmark: no test rows in %s..%s.",
                       config.BENCHMARK_START, config.BENCHMARK_END)
        return pd.DataFrame()

    # `model` names the forecaster per row. Defaulted rather than required, so a
    # test frame assembled without it (an older caller, or a Prophet-only run
    # reproduced by hand) still compares — it just labels everything "Prophet",
    # which is what such a frame contains.
    if "model" not in df.columns:
        df["model"] = "Prophet"
    df["model"] = df["model"].fillna("Prophet")

    df = df.rename(columns={"yhat": "model_forecast"})
    df = df[["item_code", "month", "model", "actual", "model_forecast"]]
    df["item_code"] = df["item_code"].astype(str).str.strip()

    # item_code → region (for the reporting breakdown only)
    df = df.merge(series_meta, on=config.GROUP_COLS, how="left")

    # BDM manual forecast, summed across bdm_codes to item_code × month.
    # rating is per (item, bdm) in the sheet; keep the first non-null per item.
    bdm = _bdm_month_col(bdm_df)
    bdm["item_code"] = bdm["item_code"].astype(str).str.strip()
    bdm_item = (
        bdm.groupby(["item_code", "month"], as_index=False)
        .agg(bdm_forecast=("bdm_forecast_qty", "sum"))
    )
    rating_by_item = (
        bdm.dropna(subset=["rating"])
        .groupby("item_code", as_index=False)
        .agg(rating=("rating", "first"))
    )
    df = df.merge(bdm_item, on=["item_code", "month"], how="left")
    df = df.merge(rating_by_item, on="item_code", how="left")

    # Prior-year baseline: item's own actual for the same month, prior year.
    prior_year = _prior_year_lookup(prepared_df)

    def _prior_year_month(month: str) -> str:
        y, m = month.split("-")
        return f"{int(y) - 1}-{m}"

    df["prior_year_forecast"] = [
        prior_year.get((it, _prior_year_month(mo)), np.nan)
        for it, mo in zip(df["item_code"], df["month"])
    ]

    # Per-row MAPEs.
    df["model_mape"]      = [_row_mape(a, f) for a, f in zip(df["actual"], df["model_forecast"])]
    df["bdm_mape"]        = [_row_mape(a, f) for a, f in zip(df["actual"], df["bdm_forecast"])]
    df["prior_year_mape"] = [_row_mape(a, f) for a, f in zip(df["actual"], df["prior_year_forecast"])]

    df["winner"] = df.apply(_winner, axis=1)

    cols = ["item_code", "region", "rating", "month", "model",
            "actual", "model_forecast", "bdm_forecast", "prior_year_forecast",
            "model_mape", "bdm_mape", "prior_year_mape", "winner"]
    df = df[[c for c in cols if c in df.columns]]
    return df.reset_index(drop=True)


def _winner(row: pd.Series) -> str:
    """Lowest available MAPE among the row's own model / bdm / prior_year wins.

    The model candidate is named by the row's `model` value, so a winner reads
    "Prophet", "Naive-3mo" or "Naive-12mo" — never a hardcoded "prophet" over a
    row Prophet did not forecast.
    """
    candidates = {
        str(row.get("model") or "Prophet"): row.get("model_mape"),
        "bdm": row.get("bdm_mape"),
        "prior_year": row.get("prior_year_mape"),
    }
    candidates = {k: v for k, v in candidates.items() if pd.notna(v)}
    if not candidates:
        return "none"
    return min(candidates, key=candidates.get)


# ── Summaries ─────────────────────────────────────────────────────────────────

def summarize(df: pd.DataFrame) -> None:
    """Log aggregate win rate, aggregate accuracy vs actual sales, and a
    breakdown by rating and region.

    Two deliberately different views, not the same number twice:
      - WIN RATE answers "how often does our forecast beat BDM/prior-year for a
        given item-month". A row-count metric — blind to magnitude.
      - ACCURACY (WAPE/bias) answers "how far off is each approach from what
        actually happened, summed across every scored row". Catches the case a
        pure win-rate hides: winning most rows by a hair while losing the rest
        by a mile still nets out badly, and only the aggregate WAPE shows that.

    Both aggregate Prophet / Naive-3mo / Naive-12mo into one "model" bucket —
    the per-row `winner`/`model` columns in benchmark_comparison.csv still name
    the specific route for a drill-down; only the console summary aggregates.
    """
    if df.empty:
        logger.warning("Benchmark: no comparison rows to summarize.")
        return

    scored = df[df["winner"] != "none"]
    n = len(scored)
    if n == 0:
        logger.warning("Benchmark: no scorable series-months (all actuals zero?).")
        return

    baselines = {"bdm", "prior_year"}

    # ── Win rate, aggregated to Model / BDM / Prior Year ─────────────────────
    win_bucket = scored["winner"].where(scored["winner"].isin(baselines), "model")
    counts = win_bucket.value_counts()
    logger.info(
        "Benchmark over %s series-months (%s..%s): %s",
        f"{n:,}", config.BENCHMARK_START, config.BENCHMARK_END,
        ", ".join(f"{label} wins {100 * cnt / n:.1f}% ({cnt:,})"
                  for label, cnt in counts.items()),
    )

    # ── Aggregate accuracy vs actual sales ─────────────────────────────────────
    # WAPE/bias are computed from summed errors over every row with a valid
    # forecast + actual, INCLUDING actual=0 months — that's correct for a
    # ratio-of-sums metric, unlike MAPE which is undefined at actual=0 and is
    # therefore averaged separately, over only the rows its own per-row
    # _row_mape() already scored (NaN-on-zero-actual is baked in there).
    rows = []
    for label, fcol, mape_col in [
        ("Model", "model_forecast", "model_mape"),
        ("BDM", "bdm_forecast", "bdm_mape"),
        ("Prior Year", "prior_year_forecast", "prior_year_mape"),
    ]:
        sub = scored.dropna(subset=[fcol, "actual"])
        if sub.empty:
            continue
        err = sub[fcol] - sub["actual"]
        denom = sub["actual"].abs().sum()
        wape = float(err.abs().sum() / denom * 100) if denom else float("nan")
        bias = float(err.sum() / denom * 100) if denom else float("nan")
        mape = float(sub[mape_col].dropna().mean())
        rows.append({
            "approach": label, "n": len(sub),
            "mape_pct": round(mape, 1) if pd.notna(mape) else np.nan,
            "wape_pct": round(wape, 1) if pd.notna(wape) else np.nan,
            "bias_pct": round(bias, 1) if pd.notna(bias) else np.nan,
        })
    acc = pd.DataFrame(rows).set_index("approach")
    logger.info(
        "Accuracy vs actual sales (%s..%s) — WAPE is volume-weighted and the "
        "more robust read; MAPE is pulled up by low-volume months; bias is "
        "signed (+ = over-forecast, - = under-forecast):\n%s",
        config.BENCHMARK_START, config.BENCHMARK_END, acc.to_string(),
    )

    # ── Win-rate by rating / region (already Model-vs-baseline, unchanged) ─────
    model_won = (~scored["winner"].isin(baselines)).astype(int)
    for dim in ("rating", "region"):
        if dim not in scored.columns:
            continue
        grp = (
            scored.assign(_p=model_won)
            .groupby(dim)
            .agg(model_win_pct=("_p", "mean"), n=("_p", "size"))
        )
        grp["model_win_pct"] = (grp["model_win_pct"] * 100).round(1)
        logger.info("Model win-rate (vs BDM and prior-year) by %s:\n%s",
                    dim, grp.to_string())
