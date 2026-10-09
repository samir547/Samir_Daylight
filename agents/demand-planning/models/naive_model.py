"""
Naive trailing-average modelling for the two Naive routes.

One flat forecast level per FIT KEY: the mean monthly quantity over the last N
calendar months up to TRAIN_END, broadcast across every month of the forward
window and across every month of the test window. N is 3 or 12, and which one
an item gets was decided upstream by `preprocessing/model_routing.py` gate 7 —
this module only reads the route label off the routing frame and looks its
window up in `config.NAIVE_WINDOW_MONTHS`.
Nothing is re-decided here and nothing is fitted; a trailing mean is arithmetic.

TWO FRAMES, ONE LEVEL — (test_df, forecast_df)
──────────────────────────────────────────────
`run_naive_forecasts()` returns a tuple: a test-window comparison frame and the
forward forecast, both built from the SAME per-fit-key trailing mean, computed
once. The test frame is what carries these rows into `evaluation/benchmark.py`,
so a naive-routed item can finally be ranked against the BDM's manual call and
against the prior-year baseline — the extension the earlier forward-only pass
deferred.

The two frames carry IDENTICAL yhat/yhat_lower/yhat_upper for a given fit key.
That is not a shortcut taken to avoid a second computation: a trailing mean
anchored at TRAIN_END has no notion of "test" versus "forward". It is one flat
number either way, and the test rows are that same number placed against months
the level was not computed from. What the test frame adds is an `actual` column
— the fit key's own observed quantity for that month, summed out of the same
pooled frame the level came from and LEFT-joined, so a month with no transaction
stays NaN exactly as `prophet_model.forecast_series()` leaves its test_cmp, and
is therefore unscorable rather than silently scored as a zero.

Still no metrics_df analogue: `model_metrics.csv` stays Prophet-only. Per-series
fit statistics describe a fit, and there is no fit here. The evidence that chose
a trailing average over every Croston/TSB variant, and chose the two windows,
remains the out-of-band rolling-origin CV in `analysis/croston_experiment.py`;
the in-pipeline test comparison added here is a benchmark comparator, not a
replacement for that.

THE BAND IS NOT A PREDICTION INTERVAL
─────────────────────────────────────
`yhat_lower` / `yhat_upper` are `yhat` multiplied by two fixed ratios from
`config.NAIVE_BAND_RATIOS`. They are empirical p10/p90 quantiles of realised
error across this population's CV folds, applied as constants — not something
this model computed for this series, and not comparable to Prophet's interval,
which is derived per series from its own posterior. See that config entry for
the fold counts behind each pair and for which of the two is the weaker
evidence.
"""
from __future__ import annotations

import logging

import pandas as pd

import config
from models import prophet_model
# Imported as a module, not `from models.prophet_model import TEST_START_TS, ...`:
# a value-import would freeze these six Timestamps at THIS module's import time,
# in this module's own namespace, decoupled from prophet_model's. Reading them
# as prophet_model.TEST_START_TS etc. means this file always sees whatever
# prophet_model.refresh_window_constants() most recently set — see that
# function's docstring for why that matters under --auto-window.

logger = logging.getLogger(__name__)


# ── The level ─────────────────────────────────────────────────────────────────

def _trailing_mean(series: pd.DataFrame, anchor: pd.Timestamp,
                   window_months: int) -> float:
    """
    Mean monthly quantity over the last `window_months` calendar months up to
    and including `anchor`, ZEROS INCLUDED.

    Zero-inclusive means the divisor is the NOMINAL window, always — a month
    inside the window with no sales counts as a 0, and so does a month with no
    row at all, including where the series is shorter than the window. A series
    that sold in one of the last twelve months has a rate of (total / 12), not
    (total / 1).

    This is the same convention in its third place in this codebase, stated in
    full here rather than cross-referenced away:
      * `analysis.croston_experiment._predict()`'s BASELINE branch divides
        `history[-months:].sum()` by `months`, on a zero-filled monthly grid;
      * `preprocessing.demand_classification.decline_ratio()` takes `.mean()`
        over a zero-filled grid slice, for the same reason.
    The level delivered here has to be the same arithmetic the CV that chose
    these windows was scored on, and the same arithmetic the trend cut that
    picks between them was measured with. Averaging over present rows only
    would report every dormant item as flat and inflate its level.
    """
    window_start = anchor - pd.DateOffset(months=window_months - 1)
    s = series[["ds", "y"]].copy()
    s["ds"] = pd.to_datetime(s["ds"])
    in_window = s[(s["ds"] >= window_start) & (s["ds"] <= anchor)]
    return float(in_window["y"].astype(float).sum()) / window_months


# ── Multi-series orchestration ────────────────────────────────────────────────

def run_naive_forecasts(pooled_data: pd.DataFrame,
                        routing: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Test-window comparison and forward forecast for every fit key `routing`
    sends to a Naive route, in that order.

    Args:
        pooled_data — `family_pool.pool_for_training(...).data`, i.e. THE SAME
                      pooled frame Prophet fits on. Not interchangeable with
                      `prepared`: a pooled family's route was decided by
                      `demand_classification` on its COMBINED series, so the
                      average delivered for it has to come from that same
                      combined series. Computing it on the member codes' own
                      histories would contradict the reasoning that produced the
                      route — and would then be split back out by
                      `family_pool.split_forecast` as though it had not been.
                      It is also where the test frame's `actual` comes from, at
                      that same combined grain: what a pooled Prophet series
                      carries into its own test_cmp before `split_forecast`'s
                      per-code actual-backfill runs on it.
        routing     — `model_routing.route_scope()`'s output. Only rows whose
                      `route` is a key of `config.NAIVE_WINDOW_MONTHS` produce
                      anything. Prophet and Blocked rows are silently skipped:
                      this module is not responsible for either, and a Blocked
                      row getting no forecast is the routing working, not a gap.

    Returns (test_df, forecast_df):
      * test_df     — one row per (fit key, TEST_START..TEST_END month) with ds,
                      item_code, yhat, yhat_lower, yhat_upper, `actual`, `model`.
      * forecast_df — one row per (fit key, FORECAST_START..FORECAST_END month),
                      the same columns minus `actual`.

    `item_code` carries the FIT KEY in both, exactly as `run_forecasts()` leaves
    it for `family_pool.split_forecast`, and `model` carries the route label -
    which is what lets Power BI tell these rows from Prophet's downstream, the
    purpose the architecture doc's `model_version` column already anticipates.
    The level is identical across the two frames; see the module docstring for
    why that is the honest answer, and for what the band is and is not.
    """
    base_cols = ["ds", "item_code", "yhat", "yhat_lower", "yhat_upper", "model"]
    empty_fc = pd.DataFrame(columns=base_cols)
    empty_test = pd.DataFrame(columns=base_cols + ["actual"])
    if routing.empty or pooled_data.empty:
        return empty_test, empty_fc

    naive = routing[routing["route"].isin(config.NAIVE_WINDOW_MONTHS)]
    if naive.empty:
        logger.info("Naive routes: nothing routed to a trailing average.")
        return empty_test, empty_fc

    # Dedup to FIT KEY grain: every current code of a pooled family carries the
    # same family_key and the same route (the family is classified once), so
    # routing's item_code grain would otherwise average the same series
    # repeatedly and emit duplicate rows for it. `split_forecast` is what
    # expands a fit key back out to its current codes — later, and once.
    fit_keys = (naive[["family_key", "route"]].drop_duplicates()
                     .sort_values("family_key"))

    df = pooled_data.copy()
    df["item_code"] = df["item_code"].astype(str).str.strip()
    by_key = {key: grp for key, grp in df.groupby("item_code")}

    # Defensive, not load-bearing under the current call order: run_forecasts()
    # already refreshes these before this function is normally called. Kept
    # here so this function is correct standalone, same reasoning as the call
    # inside run_forecasts() itself.
    prophet_model.refresh_window_constants()

    forecast_dates = pd.date_range(prophet_model.FORECAST_START_TS,
                                   prophet_model.FORECAST_END_TS, freq="MS")
    test_dates     = pd.date_range(prophet_model.TEST_START_TS,
                                   prophet_model.TEST_END_TS, freq="MS")
    mix = fit_keys["route"].value_counts().to_dict()
    logger.info(
        "Forecasting %s naive fit key(s) [%s] over %s month(s) — trailing mean "
        "anchored at %s, plus the same level over the %s-month test window "
        "(%s..%s) for the benchmark",
        f"{len(fit_keys):,}",
        " | ".join(f"{r} {mix.get(r, 0)} @ {n}mo"
                   for r, n in config.NAIVE_WINDOW_MONTHS.items()),
        len(forecast_dates), config.TRAIN_END,
        len(test_dates), config.TEST_START, config.TEST_END,
    )

    fc_rows: list[pd.DataFrame] = []
    test_rows: list[pd.DataFrame] = []
    skipped: list[str] = []
    for fit_key, route in fit_keys.itertuples(index=False):
        fit_key = str(fit_key).strip()
        series = by_key.get(fit_key)
        if series is None or series.empty:
            # Logged and skipped, never raised — the same shape as
            # run_forecasts()'s SKIP line. A routed key with no rows in the
            # pooled frame is a finding to chase, not a reason to lose the other
            # forty-odd forecasts on the way past it.
            logger.info("  SKIP %r (routed %s, no rows in pooled_data)",
                        fit_key, route)
            skipped.append(fit_key)
            continue

        # ONE level per fit key, computed once and used by BOTH frames. The test
        # rows are not a second, separately-derived prediction: a trailing mean
        # anchored at TRAIN_END is the same flat number whichever window it is
        # laid against.
        level = _trailing_mean(series, prophet_model.TRAIN_END_TS,
                               config.NAIVE_WINDOW_MONTHS[route])
        lo_ratio, hi_ratio = config.NAIVE_BAND_RATIOS[route]

        # Clipped at 0 and rounded to 2dp, the same as forecast_series() ships
        # Prophet's three columns — a negative quantity is never delivered, and
        # the two frames are concatenated into one CSV.
        #
        # The band multiplies the ROUNDED, clipped `yhat`, not the raw level, so
        # that every published row is reproducible from the row itself: anyone
        # checking a band by hand reads yhat out of the CSV and multiplies by the
        # config ratio. Scaling the raw level instead would put the two off by a
        # cent's worth of rounding and make a correct row look wrong.
        yhat = round(max(level, 0.0), 2)
        lower, upper = round(yhat * lo_ratio, 2), round(yhat * hi_ratio, 2)

        fc_rows.append(pd.DataFrame({
            "ds": forecast_dates,
            "item_code": fit_key,
            "yhat": yhat,
            "yhat_lower": lower,
            "yhat_upper": upper,
            "model": route,
        }))

        # The fit key's OWN actual per test month, summed over the pooled frame's
        # rows for that key. LEFT-joined onto the full test grid, so a month the
        # key sold nothing in stays NaN rather than becoming a 0 — the same shape
        # forecast_series() leaves its test_cmp in, and for the same reason: a
        # missing transaction is an unscorable month, not a measured zero, and
        # benchmark._row_mape drops it either way.
        own = series[["ds", "y"]].copy()
        own["ds"] = pd.to_datetime(own["ds"])
        own = (own[(own["ds"] >= prophet_model.TEST_START_TS)
                   & (own["ds"] <= prophet_model.TEST_END_TS)]
               .groupby("ds", as_index=False)["y"].sum()
               .rename(columns={"y": "actual"}))
        test_rows.append(pd.DataFrame({
            "ds": test_dates,
            "item_code": fit_key,
            "yhat": yhat,
            "yhat_lower": lower,
            "yhat_upper": upper,
            "model": route,
        }).merge(own, on="ds", how="left"))

    if skipped:
        logger.info(
            "Naive routes: %s fit key(s) routed to a trailing average but absent "
            "from pooled_data, so no forecast was produced for them: %s",
            f"{len(skipped):,}", sorted(skipped)[:10],
        )

    out_fc = pd.concat(fc_rows, ignore_index=True) if fc_rows else empty_fc
    out_test = pd.concat(test_rows, ignore_index=True) if test_rows else empty_test
    logger.info(
        "Naive routes: %s forecast row(s) and %s test row(s) from %s fit key(s), "
        "%s skipped.",
        f"{len(out_fc):,}", f"{len(out_test):,}",
        f"{len(fit_keys) - len(skipped):,}", f"{len(skipped):,}",
    )
    return out_test, out_fc
