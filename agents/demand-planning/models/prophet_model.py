"""
Prophet modelling.

One model is trained per unique config.GROUP_COLS combination (Item).
Fitted models are returned alongside predictions so downstream plotting can
reuse them instead of refitting.
"""
from __future__ import annotations

import logging
import warnings
from typing import Optional

import pandas as pd
from prophet import Prophet
from tqdm import tqdm

import config
from evaluation.metrics import calc_metrics

# Suppress noisy Stan / cmdstanpy output
logging.getLogger("cmdstanpy").setLevel(logging.WARNING)
logging.getLogger("prophet").setLevel(logging.WARNING)
warnings.filterwarnings("ignore")

logger = logging.getLogger(__name__)


def _ym_to_ts(ym: str) -> pd.Timestamp:
    return pd.Timestamp(ym + "-01")


TRAIN_START_TS    = _ym_to_ts(config.TRAIN_START)
TRAIN_END_TS      = _ym_to_ts(config.TRAIN_END)
TEST_START_TS     = _ym_to_ts(config.TEST_START)
TEST_END_TS       = _ym_to_ts(config.TEST_END)
FORECAST_START_TS = _ym_to_ts(config.FORECAST_START)
FORECAST_END_TS   = _ym_to_ts(config.FORECAST_END)


def refresh_window_constants() -> None:
    """Re-derive the six *_TS constants above from the current config.* values.

    TRAIN_START_TS..FORECAST_END_TS are Timestamps, parsed from config.py's
    "YYYY-MM" strings ONCE, when this module is first imported. That is exactly
    right for a static config, but main.py's --auto-window flag (see
    apply_auto_window()) overrides config.TRAIN_END / TEST_START / TEST_END /
    FORECAST_START / FORECAST_END in-memory for the run, AFTER this module has
    already been imported — Python's `import` runs at main.py's top level,
    before argparse or any override logic executes. Without this function the
    six Timestamps above would keep training/testing/forecasting against the
    STALE static dates even though every other part of the pipeline (which
    reads config.TEST_END etc. live, at call time, not at import time) picks
    the override up correctly.

    Idempotent and cheap (six string-to-Timestamp parses) — called
    automatically at the top of run_forecasts() and of
    models.naive_model.run_naive_forecasts(), so correctness does not depend on
    every caller remembering to call it, or on the two being called in a
    particular order.
    """
    global TRAIN_START_TS, TRAIN_END_TS, TEST_START_TS, TEST_END_TS
    global FORECAST_START_TS, FORECAST_END_TS
    TRAIN_START_TS    = _ym_to_ts(config.TRAIN_START)
    TRAIN_END_TS      = _ym_to_ts(config.TRAIN_END)
    TEST_START_TS     = _ym_to_ts(config.TEST_START)
    TEST_END_TS       = _ym_to_ts(config.TEST_END)
    FORECAST_START_TS = _ym_to_ts(config.FORECAST_START)
    FORECAST_END_TS   = _ym_to_ts(config.FORECAST_END)


# ── Segment-level parameters ──────────────────────────────────────────────────

def series_params(fit_key: str, known_changeover: frozenset[str] = frozenset()) -> dict:
    """
    `config.PROPHET_PARAMS` with `changepoint_prior_scale` set for this series'
    SEGMENT.

    A strict two-way branch, and deliberately so. `known_changeover` is a set of
    fit keys (from `family_pool.known_changeover_keys`), not a per-key mapping of
    values: every member of the segment gets exactly
    `config.CHANGEPOINT_PRIOR_KNOWN_CHANGEOVER` and everything else gets exactly
    the PROPHET_PARAMS default. There is no per-item_code tuning here and none
    should be added — a lookup table of individually-fitted values per product
    was explicitly ruled out, because it optimises against the same folds it is
    scored on and does not generalise to next month's data.

    See `config.CHANGEPOINT_PRIOR_KNOWN_CHANGEOVER` for the evidence behind both
    the split and the value.
    """
    params = dict(config.PROPHET_PARAMS)
    if fit_key in known_changeover:
        params["changepoint_prior_scale"] = config.CHANGEPOINT_PRIOR_KNOWN_CHANGEOVER
    return params


# ── Single-series forecast ────────────────────────────────────────────────────

def forecast_series(
    series: pd.DataFrame,   # columns: ds (Timestamp), y (float)
    params: Optional[dict] = None,
) -> Optional[tuple[pd.DataFrame, pd.DataFrame, dict, Prophet]]:
    """
    Train Prophet on the training window, evaluate on the test window, and
    produce the forward forecast.

    `params` defaults to `config.PROPHET_PARAMS` (the standard segment); callers
    pass `series_params(fit_key, known_changeover)` to get the segment-adjusted
    set.

    Returns (test_comparison_df, forward_forecast_df, metrics_dict, model)
    or None if the series has too little training data.
    """
    train = series[(series["ds"] >= TRAIN_START_TS) & (series["ds"] <= TRAIN_END_TS)].copy()
    test  = series[(series["ds"] >= TEST_START_TS)  & (series["ds"] <= TEST_END_TS)].copy()

    if len(train) < config.MIN_TRAIN_MONTHS:
        return None

    model = Prophet(**(params if params is not None else config.PROPHET_PARAMS))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model.fit(train)

    # Predict over the combined test + forecast window
    future_dates = pd.date_range(TEST_START_TS, FORECAST_END_TS, freq="MS")
    raw_pred = model.predict(pd.DataFrame({"ds": future_dates}))[
        ["ds", "yhat", "yhat_lower", "yhat_upper"]
    ].copy()

    # Quantities cannot be negative
    for col in ("yhat", "yhat_lower", "yhat_upper"):
        raw_pred[col] = raw_pred[col].clip(lower=0).round(2)

    # ── Test comparison ───────────────────────────────────────────────────────
    # test_cmp covers every month in the test window (TEST_START..TEST_END), not
    # just the months with a recorded actual. raw_pred already has yhat for the
    # full window, so there's no reason to drop it just because that month's
    # actual is missing (e.g. a sparse/lumpy item with no transaction that
    # month) -- Prophet still computed a prediction for it. Scoring is
    # unaffected: valid_rows still requires a real actual, exactly as before, so
    # n_test_months / MAPE / WAPE / bias do not change for any series.
    nan_metrics = {"mae": float("nan"), "rmse": float("nan"), "mape_pct": float("nan")}
    test_window_pred = raw_pred[
        (raw_pred["ds"] >= TEST_START_TS) & (raw_pred["ds"] <= TEST_END_TS)
    ]
    if len(test_window_pred) > 0:
        test_cmp = test_window_pred.merge(
            test[["ds", "y"]], on="ds", how="left"
        ).rename(columns={"y": "actual"})
        valid_rows = test_cmp.dropna(subset=["actual", "yhat"])
        metrics = (
            calc_metrics(valid_rows["actual"], valid_rows["yhat"])
            if len(valid_rows) > 0 else dict(nan_metrics)
        )
    else:
        test_cmp = pd.DataFrame(columns=["ds", "actual", "yhat", "yhat_lower", "yhat_upper"])
        metrics  = dict(nan_metrics)

    metrics["n_train_months"] = len(train)
    metrics["n_test_months"]  = len(test)

    # ── Forward forecast (config.FORECAST_START → FORECAST_END) ───────────────
    forward = raw_pred[raw_pred["ds"] >= FORECAST_START_TS].copy()

    return test_cmp, forward, metrics, model


# ── Multi-series orchestration ────────────────────────────────────────────────

def run_forecasts(
    df: pd.DataFrame,
    known_changeover: frozenset[str] = frozenset(),
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    """
    df must contain: ds (Timestamp, month-start), y (float), + config.GROUP_COLS.

    `known_changeover` is the set of fit keys in the known-changeover segment
    (`family_pool.known_changeover_keys`); those series are fitted with the
    looser `config.CHANGEPOINT_PRIOR_KNOWN_CHANGEOVER`. Defaulting it to the
    empty set means every series gets the standard prior — i.e. an unaware
    caller gets the pre-segmentation behaviour rather than a silent surprise.

    Returns:
        test_df      – actuals vs predictions for the held-out test window
        forecast_df  – forward predictions for the forecast window
        metrics_df   – per-series MAE / RMSE / MAPE summary
        models       – {group-key tuple: fitted Prophet model}
    """
    # Defensive, not load-bearing under the current call order: main.py applies
    # any --auto-window override before this is called. Kept here so this
    # function is correct on its own, regardless of caller or future refactor.
    refresh_window_constants()

    test_rows:     list[pd.DataFrame] = []
    forecast_rows: list[pd.DataFrame] = []
    metric_rows:   list[dict]         = []
    models:        dict               = {}

    group_iter = list(df.groupby(config.GROUP_COLS, sort=True))
    n_segment = sum(
        1 for keys, _ in group_iter
        if (keys[0] if isinstance(keys, tuple) else keys) in known_changeover
    )
    logger.info("Training %s Prophet model(s) [grouped by %s]",
                f"{len(group_iter):,}", config.GROUP_COLS)
    logger.info(
        "Changepoint segments: %s series known-changeover "
        "(changepoint_prior_scale=%s) | %s standard (%s)",
        f"{n_segment:,}", config.CHANGEPOINT_PRIOR_KNOWN_CHANGEOVER,
        f"{len(group_iter) - n_segment:,}",
        config.PROPHET_PARAMS["changepoint_prior_scale"],
    )

    skipped = 0
    for keys, grp in tqdm(group_iter, unit="series"):
        if not isinstance(keys, tuple):
            keys = (keys,)

        label  = " | ".join(str(k) for k in keys)
        series = grp[["ds", "y"]].dropna().sort_values("ds").reset_index(drop=True)

        # GROUP_COLS is ["item_code"], which for a pooled family carries the
        # family_key — the same grain known_changeover_keys() returns.
        result = forecast_series(series, series_params(keys[0], known_changeover))
        if result is None:
            tqdm.write(
                f"  SKIP {label!r} "
                f"({len(series[series['ds'] <= TRAIN_END_TS])} train months, "
                f"need {config.MIN_TRAIN_MONTHS})"
            )
            skipped += 1
            continue

        test_cmp, forward, metrics, model = result
        models[keys] = model

        # Which segment this series was fitted as, on the row itself — so
        # "was this item loosened?" is answerable from model_metrics.csv alone.
        metrics["changepoint_prior_scale"] = float(model.changepoint_prior_scale)
        metrics["changepoint_segment"] = (
            "known_changeover" if keys[0] in known_changeover else "standard"
        )

        # Attach group key columns
        for col, val in zip(config.GROUP_COLS, keys):
            test_cmp[col] = val
            forward[col]  = val
            metrics[col]  = val

        # Which model produced the row, on the row itself. Both frames are
        # concatenated with naive-routed rows downstream (main.py step 5c), and
        # from there the benchmark reads this column to name each item's
        # forecaster instead of assuming Prophet made every prediction.
        test_cmp["model"] = "Prophet"
        forward["model"]  = "Prophet"

        test_rows.append(test_cmp)
        forecast_rows.append(forward)
        metric_rows.append(metrics)

    if skipped:
        logger.info("%s series skipped (insufficient training data).", f"{skipped:,}")

    test_df     = pd.concat(test_rows,     ignore_index=True) if test_rows     else pd.DataFrame()
    forecast_df = pd.concat(forecast_rows, ignore_index=True) if forecast_rows else pd.DataFrame()
    metrics_df  = pd.DataFrame(metric_rows)

    return test_df, forecast_df, metrics_df, models
