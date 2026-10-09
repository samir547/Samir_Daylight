"""
Trend-rigidity audit of the badly-wrong tail (Phase 1 — diagnostic only).

THE HYPOTHESIS UNDER TEST
─────────────────────────
A share of the series with `mape_pct > 100` in `output/model_metrics.csv` may be
failing not because of `seasonality_mode`, but because Prophet's *trend* never
moved: `config.PROPHET_PARAMS['changepoint_prior_scale'] = 0.05` regularises the
per-changepoint rate adjustments so hard that a real, sustained level shift in
the recent data is absorbed as noise. The fitted trend then sits at the old
level while actual sales run somewhere else entirely, and every forecast off
that trend is wrong by construction.

This module MEASURES that; it does not fix it. It writes three new CSVs and
touches nothing the pipeline owns. No config value is changed, no existing
output is rewritten, nothing goes to the database, and neither the test-window
scoring nor the forward forecast is invoked — only the fit step and a `predict`
over the *training* months, which is what "where did the trend actually sit?"
requires.

WHY IT REUSES THE PIPELINE INSTEAD OF REBUILDING THE SERIES
───────────────────────────────────────────────────────────
The manual spot-check that motivated this audit was run on a hand-rolled series
per item code, and it got `EN1560` wrong twice over: `EN1560` is pooled with the
retired code `EN1530` into a 93-month successor family, so the per-item series
Prophet never saw looked like a trend *crash* rather than the rigid trend the
pooled series actually shows; and the cached `raw_data.csv` snapshot it read
does not have the Amazon-channel exclusion applied, which `forecast_training_data`
(the real source) does.

So this module calls the pipeline's own functions, in `main.main()`'s own order,
to rebuild the exact frame `run_forecasts()` is handed:

    main.load_all  →  scope.filter_amazon_channel  →  prepare.prepare
                   →  family_pool.build_successor_families
                   →  family_pool.pool_for_training
                   →  scope.apply_scope

and fits with `config.PROPHET_PARAMS` imported, never retyped, so the audit
cannot silently drift from the model it is auditing. The unit of fitting is
therefore the *family series* — pooled families are fitted once and their result
attributed to each of their target item codes, exactly as the pipeline does.

TWO JUDGMENT CALLS, DELIBERATELY VISIBLE
────────────────────────────────────────
Neither threshold below comes from the codebase; both are carried over from the
manual spot-check and are stated in the console summary and in this docstring
rather than buried inside a boolean column. Anyone reading the output should be
able to re-cut it at a different threshold from the two detail files.

Run:  python -m analysis.trend_audit          (reads cached CSVs in output/)
      python -m analysis.trend_audit --db     (reload raw inputs from Azure SQL)
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prophet import Prophet  # noqa: E402

import config  # noqa: E402
import main as pipeline  # noqa: E402  — reused for load_all()'s CSV-cache logic
from models.prophet_model import TRAIN_END_TS, TRAIN_START_TS  # noqa: E402
from preprocessing import family_pool, prepare, scope  # noqa: E402

# ── Judgment calls (see module docstring) ─────────────────────────────────────

# Which series count as "badly wrong". From the last run's analysis.
MAPE_TAIL_THRESHOLD = 100.0

# |delta| above which a changepoint counts as one Prophet actually *used*.
# NOT a codebase value — this is the threshold the manual DN1380 / D31875
# spot-check used when it reported "zero changepoints", reused here so the
# numbers are comparable. Prophet's deltas are rate adjustments in scaled
# units, so this is small by construction.
DELTA_SIGNIFICANCE = 0.01

# How far the fitted trend at TRAIN_END may sit from the last-6-month actual
# average before it counts as disconnected. 30% is a starting point sized off
# the spot-check magnitudes (DN1380: 273.9 vs a 189-258 recent range; the
# 142.7-vs-49.3 case is far past any plausible threshold).
TREND_DISCONNECT_PCT = 30.0

RECENT_WINDOW_MONTHS = 6
LONG_WINDOW_MONTHS = 12


# ── Rebuild the exact frame the pipeline fits on ──────────────────────────────

def build_fit_input(cached: bool = True):
    """
    Reproduce `main.main()` steps 1–4 and return what step 5 would have been fed.

    Returns (scoped, families, eligible):
        scoped    — the family-keyed frame `run_forecasts()` receives, i.e. the
                    real training series, Amazon-excluded, active-scoped,
                    >=MIN_TRAIN_MONTHS, successor-pooled.
        families  — resolved SuccessorFamilies, for family_key lookup.
        eligible  — the family_keys that were actually pooled in this run.

    `pilot=False` because the run that produced `model_metrics.csv` is the full
    run (187 fitted series, not the A-rated subset).
    """
    raw, bdm_df, active, _master, successor = pipeline.load_all(cached)

    # Amazon rows must go before prepare(): `channel` does not survive its
    # groupby. The excluded frame is deliberately dropped on the floor here —
    # writing it is main()'s job, not this audit's.
    raw, _excluded = scope.filter_amazon_channel(raw)

    prepared = prepare.prepare(raw)
    families = family_pool.build_successor_families(successor)
    pooled = family_pool.pool_for_training(prepared, families, active, bdm_df)
    scoped = scope.apply_scope(
        pooled.data, pooled.active_products, pooled.bdm_forecasts, pilot=False
    )
    return scoped, families, pooled.eligible


def load_targets() -> pd.DataFrame:
    """The badly-wrong tail from the last run: mape_pct > MAPE_TAIL_THRESHOLD."""
    metrics = pd.read_csv(config.OUTPUT_DIR / "model_metrics.csv")
    metrics["item_code"] = metrics["item_code"].astype(str).str.strip()
    metrics["mape_pct"] = pd.to_numeric(metrics["mape_pct"], errors="coerce")

    tail = metrics[metrics["mape_pct"] > MAPE_TAIL_THRESHOLD].copy()
    cols = [c for c in ["item_code", "mape_pct", "split_method", "n_train_months"]
            if c in tail.columns]
    return tail[cols].sort_values("mape_pct", ascending=False).reset_index(drop=True)


# ── Fit + extract ─────────────────────────────────────────────────────────────

def _window_average(train: pd.DataFrame, months: int) -> float:
    """
    Mean monthly `y` over the last `months` CALENDAR months ending at TRAIN_END.

    Divided by the calendar month count, not by the number of rows present:
    `prepare()` emits no zero-quantity rows, so a month with no sales is simply
    absent, and averaging over present rows only would report a stale figure
    from whenever the item last sold rather than what it is selling now. A
    dormant series correctly averages toward zero.
    """
    start = TRAIN_END_TS - pd.DateOffset(months=months - 1)
    window = train[(train["ds"] >= start) & (train["ds"] <= TRAIN_END_TS)]
    return float(window["y"].sum()) / months


def fit_and_extract(series: pd.DataFrame) -> dict:
    """
    Fit Prophet on one training series and pull out what the audit needs.

    Returns a dict with the changepoint table, the per-month fitted trend, and
    the scalar summary values. The fit uses `config.PROPHET_PARAMS` verbatim —
    same model, same priors, same seasonality as the production run.
    """
    train = (
        series[(series["ds"] >= TRAIN_START_TS) & (series["ds"] <= TRAIN_END_TS)]
        [["ds", "y"]].dropna().sort_values("ds").reset_index(drop=True)
    )

    model = Prophet(**config.PROPHET_PARAMS)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model.fit(train)

    # MAP estimation (no mcmc_samples in PROPHET_PARAMS) gives delta shape
    # (1, n_changepoints); the mean is a no-op there and stays correct if the
    # config ever switches to sampling.
    deltas = np.asarray(model.params["delta"], dtype=float)
    if deltas.ndim > 1:
        deltas = deltas.mean(axis=0)
    cps = pd.to_datetime(pd.Series(model.changepoints).reset_index(drop=True))

    changepoints = pd.DataFrame({
        "changepoint_date": cps,
        "delta": deltas[:len(cps)],
    })
    changepoints["above_threshold"] = changepoints["delta"].abs() > DELTA_SIGNIFICANCE

    # Predict over the training months. TRAIN_END is forced into the grid even
    # when the series has no sales row that month, so the headline trend value
    # is always read at the same date for every series.
    grid = pd.DataFrame({"ds": sorted(set(train["ds"]) | {TRAIN_END_TS})})
    trend = model.predict(grid)[["ds", "trend"]]
    trend = trend.merge(
        train.rename(columns={"y": "actual_y"}), on="ds", how="left"
    ).sort_values("ds").reset_index(drop=True)

    trend_at_end = float(trend.loc[trend["ds"] == TRAIN_END_TS, "trend"].iloc[0])
    avg_6 = _window_average(train, RECENT_WINDOW_MONTHS)
    avg_12 = _window_average(train, LONG_WINDOW_MONTHS)

    if avg_6 != 0:
        pct_diff = (trend_at_end - avg_6) / abs(avg_6) * 100.0
        disconnected = abs(pct_diff) > TREND_DISCONNECT_PCT
    else:
        # No recent sales at all: a percentage gap is undefined, but a non-zero
        # trend over a dead series is the disconnect in its purest form.
        pct_diff = float("nan")
        disconnected = abs(trend_at_end) > 0

    return {
        "n_train_months": len(train),
        "n_significant_changepoints": int(changepoints["above_threshold"].sum()),
        "n_changepoints_considered": len(changepoints),
        "fitted_trend_at_train_end": round(trend_at_end, 2),
        "actual_avg_last_6mo": round(avg_6, 2),
        "actual_avg_last_12mo": round(avg_12, 2),
        "pct_diff_trend_vs_6mo_actual": round(pct_diff, 1) if pd.notna(pct_diff) else np.nan,
        "trend_disconnected": bool(disconnected),
        "train_end_has_actual": bool((train["ds"] == TRAIN_END_TS).any()),
        "_changepoints": changepoints,
        "_trend": trend,
    }


def audit(targets: pd.DataFrame, scoped: pd.DataFrame,
          families: family_pool.SuccessorFamilies,
          eligible: frozenset[str]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, list[str]]:
    """
    Fit every target series and assemble the three output frames.

    Pooled families are fitted ONCE and the result attributed to each of their
    target item codes — the pipeline fits one model per family, so reporting a
    separate fit per member would be inventing models that never existed.
    """
    by_key = {k: g for k, g in scoped.groupby("item_code")}

    summary_rows: list[dict] = []
    cp_frames: list[pd.DataFrame] = []
    trend_frames: list[pd.DataFrame] = []
    missing: list[str] = []
    fits: dict[str, dict] = {}

    for row in targets.itertuples(index=False):
        item = row.item_code
        key = families.family_key(item)
        is_pooled = key in eligible
        fit_key = key if is_pooled else item

        if fit_key not in by_key:
            missing.append(f"{item} (fit key {fit_key})")
            continue

        if fit_key not in fits:
            print(f"  fitting {fit_key}{' [pooled]' if is_pooled else ''} …")
            fits[fit_key] = fit_and_extract(by_key[fit_key])
        res = fits[fit_key]

        summary_rows.append({
            "item_code": item,
            "family_key": fit_key,
            "is_pooled": is_pooled,
            "split_method": getattr(row, "split_method", family_pool.SPLIT_NA),
            "n_train_months": res["n_train_months"],
            "current_mape_pct": row.mape_pct,
            "n_significant_changepoints": res["n_significant_changepoints"],
            "fitted_trend_at_train_end": res["fitted_trend_at_train_end"],
            "actual_avg_last_6mo": res["actual_avg_last_6mo"],
            "actual_avg_last_12mo": res["actual_avg_last_12mo"],
            "pct_diff_trend_vs_6mo_actual": res["pct_diff_trend_vs_6mo_actual"],
            "trend_disconnected": res["trend_disconnected"],
        })

        cp = res["_changepoints"].copy()
        cp.insert(0, "family_key", fit_key)
        cp.insert(0, "item_code", item)
        cp_frames.append(cp)

        tr = res["_trend"].rename(columns={"trend": "fitted_trend"}).copy()
        tr["fitted_trend"] = tr["fitted_trend"].round(2)
        tr.insert(0, "family_key", fit_key)
        tr.insert(0, "item_code", item)
        trend_frames.append(tr[["item_code", "family_key", "ds", "fitted_trend", "actual_y"]])

    summary = pd.DataFrame(summary_rows)
    changepoints = (pd.concat(cp_frames, ignore_index=True)
                    if cp_frames else pd.DataFrame(
                        columns=["item_code", "family_key", "changepoint_date",
                                 "delta", "above_threshold"]))
    trends = (pd.concat(trend_frames, ignore_index=True)
              if trend_frames else pd.DataFrame(
                  columns=["item_code", "family_key", "ds", "fitted_trend", "actual_y"]))
    return summary, changepoints, trends, missing


# ── Reporting ─────────────────────────────────────────────────────────────────

def print_summary(summary: pd.DataFrame, changepoints: pd.DataFrame,
                  missing: list[str]) -> None:
    sep = "-" * 76
    n = len(summary)

    print(f"\n{sep}\nTHRESHOLDS USED (all three are judgment calls, not codebase values)\n{sep}")
    print(f"  badly-wrong tail        : mape_pct > {MAPE_TAIL_THRESHOLD:g}  (from model_metrics.csv)")
    print(f"  significant changepoint : |delta| > {DELTA_SIGNIFICANCE}  "
          f"(carried over from the manual DN1380 / D31875 spot-check)")
    print(f"  trend disconnected      : |fitted trend at {config.TRAIN_END} - "
          f"last-{RECENT_WINDOW_MONTHS}mo actual avg| > {TREND_DISCONNECT_PCT:g}% of that average")
    print(f"  recent average          : total y over the last {RECENT_WINDOW_MONTHS} calendar "
          f"months to {config.TRAIN_END}, divided by {RECENT_WINDOW_MONTHS}\n"
          f"                            (months with no sales row count as 0, not as absent)")

    if missing:
        print(f"\n  NOT AUDITED — {len(missing)} target(s) absent from the rebuilt scope frame:")
        for m in missing:
            print(f"    {m}")

    if summary.empty:
        print("\nNo target series could be audited.")
        return

    zero_cp = summary["n_significant_changepoints"] == 0
    disc = summary["trend_disconnected"]
    both = zero_cp & disc

    print(f"\n{sep}\nRESULT — {n} series in the badly-wrong tail\n{sep}")
    print(f"  zero significant changepoints        : {zero_cp.sum():>4} / {n}  ({zero_cp.mean()*100:.0f}%)")
    print(f"  trend disconnected from recent actual: {disc.sum():>4} / {n}  ({disc.mean()*100:.0f}%)")
    print(f"  BOTH (the full spot-check signature) : {both.sum():>4} / {n}  ({both.mean()*100:.0f}%)")
    print(f"  neither                              : {(~zero_cp & ~disc).sum():>4} / {n}")

    print(f"\n{sep}\nPooled vs singleton\n{sep}")
    rows = []
    for label, mask in (("pooled", summary["is_pooled"]), ("singleton", ~summary["is_pooled"])):
        if not mask.any():
            continue
        rows.append({
            "cohort": label,
            "n_series": int(mask.sum()),
            "zero_changepoints": int(zero_cp[mask].sum()),
            "trend_disconnected": int(disc[mask].sum()),
            "both": int(both[mask].sum()),
            "both_pct": round(both[mask].mean() * 100, 1),
        })
    print(pd.DataFrame(rows).to_string(index=False))

    print(f"\n{sep}\nChangepoint deltas across all audited fits\n{sep}")
    n_fits = changepoints.groupby("family_key").ngroups
    print(f"  distinct fitted series      : {n_fits}")
    print(f"  changepoints Prophet considered: {len(changepoints):,} "
          f"({len(changepoints) // max(n_fits, 1)} per fit)")
    print(f"  above |delta| > {DELTA_SIGNIFICANCE}       : {int(changepoints['above_threshold'].sum()):,}")
    print(f"  largest |delta| seen        : {changepoints['delta'].abs().max():.5f}")

    print(f"\n{sep}\nWorst 15 by MAPE\n{sep}")
    view = summary.head(15)[
        ["item_code", "family_key", "is_pooled", "current_mape_pct",
         "n_significant_changepoints", "fitted_trend_at_train_end",
         "actual_avg_last_6mo", "pct_diff_trend_vs_6mo_actual", "trend_disconnected"]
    ]
    print(view.to_string(index=False))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--db", action="store_true",
                    help="Reload raw inputs from Azure SQL instead of the cached CSVs")
    args = ap.parse_args()

    targets = load_targets()
    print(f"Badly-wrong tail: {len(targets)} series with mape_pct > {MAPE_TAIL_THRESHOLD:g}")

    scoped, families, eligible = build_fit_input(cached=not args.db)
    summary, changepoints, trends, missing = audit(targets, scoped, families, eligible)

    out = config.OUTPUT_DIR
    paths = [
        (out / "trend_diagnostic_audit.csv", summary),
        (out / "trend_diagnostic_changepoints.csv", changepoints),
        (out / "trend_diagnostic_trend_series.csv", trends),
    ]
    for path, frame in paths:
        frame.to_csv(path, index=False)
        print(f"Wrote {path}  ({len(frame):,} rows)")

    print_summary(summary, changepoints, missing)
    return 0


if __name__ == "__main__":
    sys.exit(main())
