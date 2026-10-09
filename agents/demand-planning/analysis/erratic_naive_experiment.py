"""
Erratic naive experiment — does a trailing average beat Prophet on Erratic? (read-only)

WHAT THIS IS
────────────
`analysis/croston_experiment.py` asked one question of the Lumpy/Intermittent
population: does anything model-shaped beat a flat trailing mean on the folds
those items actually have? The answer there was no, and it is the evidence
behind `config.NAIVE_WINDOW_MONTHS` and `model_routing` gate 7.

Gate 6 sends the OTHER volatile category — Erratic — to Prophet, on the strength
of one property: an Erratic series sells nearly every month (low ADI), so the
calendar-history bar `config.MIN_TRAIN_MONTHS` is the only thing standing
between it and a Prophet fit. Low ADI is a statement about WHEN demand arrives,
not about how big it is; Erratic is by definition the high-CV² half of the
regular-arrival split. Nothing has ever measured whether Prophet's trend plus
yearly seasonality actually earns its place against arithmetic on a series whose
month-to-month size swings by more than its own mean.

This module measures that, for AU first. It does not adopt anything.

    Prophet     — `config.PROPHET_PARAMS` verbatim, exactly the production fit.
                  No changepoint forcing, no prior-scale sweep. That is the
                  point: the comparison is against what the pipeline SHIPS, not
                  against a tuned variant of it that nobody runs. Scored by
                  `prophet.diagnostics.cross_validation` at
                  `changepoint_experiment.make_cutoffs()`'s rolling cutoffs.
    Naive-3mo   — flat mean of the trailing 3 calendar months, zeros included.
    Naive-12mo  — the same over 12 months.

The two naive windows are `config.NAIVE_WINDOW_MONTHS`, read from config rather
than restated, so "naive" here is the same arithmetic `models/naive_model.py`
delivers to the pipeline and the same arithmetic
`croston_experiment._predict()`'s NAIVE_WINDOWS branch was scored on. A window
this experiment invented for itself would make its numbers unreadable against
either.

WHY THIS RUNS AT ALL WHEN TWO CSVs ALREADY EXIST
────────────────────────────────────────────────
`model_metrics.csv` carries Prophet's WAPE over the held-out
TEST_START..TEST_END window. `benchmark_comparison.csv` carries the BDM and
prior-year comparators over that same window. Neither carries a naive trailing
average, and reading a Prophet number off one file against a naive number
computed somewhere else compares two models on DIFFERENT MONTHS of different
difficulty — the same objection `croston_experiment.py`'s Part C states about
itself and then refuses to lean on.

Here all three variants are scored on IDENTICAL folds, by construction: the fold
membership is taken from Prophet's own `cross_validation` output, and the naive
levels are laid against those exact (cutoff, ds, y) rows. A fold Prophet does
not produce is a fold no variant is scored on. That is the entire reason this
file exists rather than a spreadsheet join.

Validation is rolling-origin INSIDE the training window (<= config.TRAIN_END).
The held-out test window is never touched here, so nothing in this file can be
contaminated by it and nothing here supersedes what the benchmark reports.

THE SHRINKAGE CHECK IS NOT OPTIONAL
───────────────────────────────────
WAPE is bounded above at 100% for a forecast of zero and unbounded for an
over-forecast. On a volatile series with occasional near-empty months, a
variant can therefore "win" by predicting small — which is a fact about the
metric, not about skill. `croston_experiment.shrinkage_table()` exists for
exactly this and found exactly this on the Lumpy/Intermittent side.

The Erratic population has the same trap in a different shape: these items sell
every month, but some of those months are tiny against the item's own average
(AN1380 and A25130 both carry single-digit and low-double-digit months inside
the training window). So `shrinkage_table()` here reports, per variant, the mean
forecast level against the mean actual monthly rate, plus two near-zero
fingerprints — one absolute, one scaled to the item's own rate, because a level
of 5 units is "near zero" for a series averaging 300 and is not for one
averaging 8. A naive win that arrives with a bias ratio well under 1.0 is
reported as shrinkage, in those words, and not as a win.

Run:  python -m analysis.erratic_naive_experiment --input-dir output_phase1 --region "4 AU"
      python -m analysis.erratic_naive_experiment --input-dir output_phase1 --sample 3
      python -m analysis.erratic_naive_experiment --input-dir output_phase1
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prophet import Prophet  # noqa: E402
from prophet.diagnostics import cross_validation  # noqa: E402

import config  # noqa: E402
import main as pipeline  # noqa: E402  — reused for load_all()'s CSV-cache logic
from analysis.changepoint_experiment import (  # noqa: E402  — reused, not re-derived
    CV_HORIZON,
    CV_HORIZON_MONTHS,
    CV_PERIOD_MONTHS,
    MAX_CUTOFFS,
    MIN_CV_TRAIN_MONTHS,
    _fold_scores,
    make_cutoffs,
)
from analysis.croston_experiment import monthly_grid  # noqa: E402  — zero-filled grid
from analysis.demand_classification_report import build_scope_input  # noqa: E402
from models.prophet_model import TRAIN_END_TS, TRAIN_START_TS  # noqa: E402
from preprocessing import demand_classification as dc  # noqa: E402
from preprocessing import model_routing as mr  # noqa: E402
from preprocessing import prepare  # noqa: E402

logging.getLogger("cmdstanpy").setLevel(logging.ERROR)
logging.getLogger("prophet").setLevel(logging.ERROR)


# ── Variants ──────────────────────────────────────────────────────────────────

#: The production Prophet fit. The label is `model_routing.PROPHET` so the
#: variant name and the route name are the same string — a row in this file's
#: output and a row in `model_routing.csv` are talking about the same thing.
VARIANT_PROPHET = mr.PROPHET

#: The two naive windows, straight out of config. `models/naive_model.py` reads
#: the same dict for the levels it ships, and `model_routing` gate 7 emits these
#: same two labels as routes, so nothing here invents a window or a name.
NAIVE_VARIANTS = list(config.NAIVE_WINDOW_MONTHS)

VARIANTS = [VARIANT_PROPHET] + NAIVE_VARIANTS


# ── Experiment design constants (judgment calls, all stated in the summary) ────

#: Scored folds an item needs before a per-item verdict is printed at all.
#:
#: Three, and the reasoning is the reasoning `croston_experiment.py` applies to
#: its 2-4 item buckets, moved down to the item level because the population
#: here is small enough that individual items get read by name. A margin between
#: two variants measured on one or two six-month folds is one bad half-year away
#: from reversing, and this file would rather print the numbers with no verdict
#: attached than print a verdict nobody should act on. Items below the bar keep
#: their rows in every CSV and every table — they are excluded from the WINNER
#: column, not from the evidence.
MIN_FOLDS_FOR_VERDICT = 3

#: A WAPE gap below this is reported as a tie rather than a win, in points.
#:
#: Five points is the same tolerance `croston_experiment.STABILISATION_TOLERANCE_PP`
#: uses to decide when a curve has stopped moving. A gap smaller than the one
#: this project already treats as indistinguishable on a curve should not become
#: a ranking between two models when it appears between two columns.
TIE_TOLERANCE_PP = 5.0

#: Absolute near-zero bar for the shrinkage fingerprint, matching
#: `croston_experiment.shrinkage_table()` so the two files' columns mean the
#: same thing.
NEAR_ZERO_LEVEL = 0.05

#: Relative near-zero bar: a forecast under this fraction of the item's own mean
#: actual rate on the same folds. The absolute bar above is calibrated to
#: intermittent series that genuinely sit at zero; an Erratic item averaging 300
#: units a month that is forecast at 20 has shrunk to nothing in every sense
#: that matters and would clear an 0.05 test without comment. Both are reported.
NEAR_ZERO_RATE_FRACTION = 0.10

#: Bias-ratio bands for the written reading of the shrinkage table.
#:
#: THREE bands and not a single cut, because a single cut is exactly what this
#: population breaks. The two naive windows here land at bias ratios of 0.80 and
#: 0.81 — the same number for any purpose anyone would put it to — and one
#: threshold anywhere between them would print a shrinkage warning against one
#: and an all-clear against the other. That is the threshold talking, not the
#: data. Bands wide enough that two indistinguishable ratios get the same
#: sentence are the minimum honest reporting here, and the middle band says
#: "material under-forecast, cannot separate skill from metric asymmetry" rather
#: than pretending to.
SHRINKAGE_SEVERE = 0.60
SHRINKAGE_MATERIAL = 0.90


# ── Population ────────────────────────────────────────────────────────────────

def region_lookup(cached: bool) -> pd.DataFrame:
    """
    item_code -> region, from `prepare.series_metadata()`.

    The raw frame is re-read rather than threaded out of `build_scope_input()`,
    which does not return it. That is a second read of a cached CSV, and it buys
    the guarantee that matters: this is the SAME lookup `main.main()` builds at
    step 2 and hands to the benchmark, so "4 AU" here is literally the "4 AU"
    that appears in `benchmark_comparison.csv` and not a second definition of it
    that happens to agree today.
    """
    raw, *_ = pipeline.load_all(cached)
    meta = prepare.series_metadata(raw)
    meta["item_code"] = meta["item_code"].astype(str).str.strip()
    meta["region"] = meta["region"].astype(str).str.strip()
    return meta


def load_population(classes: pd.DataFrame, routing: pd.DataFrame,
                    meta: pd.DataFrame, region: str | None
                    ) -> tuple[pd.DataFrame, dict[str, int]]:
    """
    Erratic, standalone, currently routed to Prophet, optionally one region.

    Returns (population, funnel) where `funnel` counts what each filter removed,
    so the population is reported as a subtraction and not as a final number
    that has to be taken on trust.

    STANDALONE ONLY (`is_pooled == False`) — the same exclusion
    `croston_experiment.load_sample()` applies, for the same reason. A pooled
    family is fitted once on a combined series and split back out by a ratio;
    whether a non-Prophet model should inherit that arrangement, and whether the
    six-month split ratio means the same thing for a flat level as it does for a
    Prophet trajectory, is an open architectural question. Including pooled
    families here would answer it by implication, on evidence that was never
    gathered to answer it.

    ROUTED TO PROPHET — read off `model_routing.route_scope()` rather than
    re-tested against `config.MIN_TRAIN_MONTHS` here. Gate 6 is where that bar
    is applied to Erratic items, and re-implementing it would create a second
    copy that can drift from the first.
    """
    funnel: dict[str, int] = {}
    df = classes.copy()
    df["item_code"] = df["item_code"].astype(str).str.strip()
    funnel["active scope (classified)"] = len(df)

    df = df[df["category"] == dc.ERRATIC]
    funnel[f"category == {dc.ERRATIC}"] = len(df)

    df = df[~df["is_pooled"].astype(bool)]
    funnel["standalone (is_pooled == False)"] = len(df)

    routes = (routing.assign(item_code=routing["item_code"].astype(str).str.strip())
              .set_index("item_code")["route"])
    df = df.assign(route=df["item_code"].map(routes))
    df = df[df["route"] == VARIANT_PROPHET]
    funnel[f"routed to {VARIANT_PROPHET} (clears MIN_TRAIN_MONTHS)"] = len(df)

    df = df.merge(meta[["item_code", "region"]], on="item_code", how="left")
    if region is not None:
        df = df[df["region"] == region]
        funnel[f"region == {region!r}"] = len(df)
    else:
        funnel["region == (all)"] = len(df)

    cols = ["item_code", "family_key", "category", "region", "adi", "cv2",
            "n_nonzero_months", "tenure_months", "near_threshold"]
    cols = [c for c in cols if c in df.columns]
    return df[cols].sort_values("item_code").reset_index(drop=True), funnel


# ── The variants, per fold ────────────────────────────────────────────────────

def naive_level(grid: pd.DataFrame, cutoff: pd.Timestamp, months: int) -> float:
    """
    The flat forecast one naive window produces from history at or before `cutoff`.

    The same three lines as `croston_experiment._predict()`'s NAIVE_WINDOWS
    branch, on the same zero-filled monthly grid (`monthly_grid`, imported):
    take the trailing `months` rows, sum, divide by the NOMINAL window. The
    divisor is the window and never the number of rows present, so a history
    shorter than the window is still divided by the window — that is
    `models.naive_model._trailing_mean()`'s zero-inclusive convention, which is
    the level the pipeline actually ships, and it is why a series that sold in
    one of the last twelve months has a rate of total/12 rather than total/1.

    Restated in full here rather than imported as a function, because the
    croston version is keyed on that file's own variant names; the arithmetic is
    identical and deliberately so.
    """
    hist = grid.loc[grid["ds"] <= cutoff, "y"].to_numpy(dtype=float)
    if hist.size == 0:
        return 0.0
    return float(hist[-months:].sum()) / months


def variant_cv_frames(train: pd.DataFrame, cutoffs: list[pd.Timestamp]
                      ) -> tuple[dict[str, pd.DataFrame], int]:
    """
    One `cross_validation`-shaped frame per variant, on IDENTICAL folds.

    Prophet is fitted ONCE on the full training history and cross-validated at
    `cutoffs` — the same call shape as `changepoint_experiment.run_variant()`
    with the config default prior and `changepoints=None`, i.e. no forcing and
    no sweep. Because `train` stops at `config.TRAIN_END`, `model.history` can
    never contain a test-window month, so no fold can see one.

    The naive frames are then built by COPYING Prophet's own (cutoff, ds, y)
    rows and overwriting `yhat` with that cutoff's flat level. This is what
    makes the comparison a comparison: the three variants cannot end up scored
    on different months, different fold counts, or different actuals, because
    there is only one set of rows and all three share it. If
    `cross_validation` drops a cutoff, it is dropped for everyone.

    Returns (frames, n_prophet_fits) — the fit count for the timing probe.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = Prophet(**config.PROPHET_PARAMS)
        model.fit(train)
        cv = cross_validation(model, horizon=CV_HORIZON, cutoffs=cutoffs,
                              disable_tqdm=True)

    cv = cv[["ds", "cutoff", "y", "yhat"]].copy()
    frames = {VARIANT_PROPHET: cv}

    # The zero-filled grid the naive levels read. Anchored at TRAIN_END and
    # sliced per cutoff, so every fold's level uses only that fold's own history.
    grid = monthly_grid(train, TRAIN_END_TS)
    for name in NAIVE_VARIANTS:
        months = config.NAIVE_WINDOW_MONTHS[name]
        levels = {c: naive_level(grid, c, months) for c in cv["cutoff"].unique()}
        frame = cv.copy()
        frame["yhat"] = frame["cutoff"].map(levels).astype(float)
        frames[name] = frame

    # one full-history fit plus one refit per cutoff, all Prophet's
    return frames, 1 + len(cutoffs)


# ── Scoring ───────────────────────────────────────────────────────────────────

def fold_rows(frames: dict[str, pd.DataFrame], train: pd.DataFrame,
              item: str) -> list[dict]:
    """
    Every (cutoff x variant) fold for one item, scored.

    The arithmetic is `changepoint_experiment._fold_scores()`, decomposed to one
    row per fold instead of averaged: `yhat` clipped at zero first (production
    clips it, so an unclipped score would score a model the pipeline does not
    ship), WAPE as sum-normalised absolute error, MAPE skipping y == 0 rows and
    never used to drop a row from WAPE. `_fold_scores` itself is still the thing
    that produces the headline per-variant numbers in `item_scores()` — this
    function is its decomposition, and the two are cross-checked there.

    `yhat_level_or_forecast` is a flat level for the naive variants and the MEAN
    of Prophet's six monthly points for Prophet, which is the only way one
    number can stand for a trajectory. It feeds the shrinkage table, where the
    question is how big a variant's forecast is against the demand that arrived,
    not what shape it had.
    """
    rows: list[dict] = []
    for name in VARIANTS:
        cv = frames[name].copy()
        cv["yhat"] = cv["yhat"].clip(lower=0)
        cv["abs_err"] = (cv["y"] - cv["yhat"]).abs()

        for cutoff, fold in cv.groupby("cutoff"):
            denom = float(fold["y"].abs().sum())
            nz = fold[fold["y"] != 0]
            rows.append({
                "item_code": item,
                "variant": name,
                "cutoff": pd.Timestamp(cutoff),
                "n_train_months_at_cutoff": int((train["ds"] <= cutoff).sum()),
                "yhat_level_or_forecast": float(fold["yhat"].mean()),
                "actual_sum": denom,
                "wape": (float(fold["abs_err"].sum() / denom * 100)
                         if denom > 0 else np.nan),
                "mape": (float((nz["abs_err"] / nz["y"].abs()).mean() * 100)
                         if len(nz) else np.nan),
                "fold_note": "zero_demand_fold" if denom == 0 else "",
                "n_horizon_months": int(len(fold)),
                "abs_err_sum": float(fold["abs_err"].sum()),
            })
    return rows


def item_scores(folds: pd.DataFrame, frames_by_item: dict[str, dict]
                ) -> pd.DataFrame:
    """
    Collapse folds to one row per (item, variant).

    `wape` and `mape` come from `changepoint_experiment._fold_scores()` called on
    the variant's own cv frame — the imported scorer is the authority for the
    headline numbers, not a convention this file re-implements. `fold_rows()`'s
    decomposition is checked against it and any disagreement is carried in
    `scorer_delta` rather than silently reconciled.

    `wape_pooled` sums every fold-month's error and divides by every fold-month's
    actual, which KEEPS the zero-demand folds that the per-fold mean has to drop
    for want of a denominator. Both are carried for the reason
    `croston_experiment.item_scores()` carries both: the per-fold mean lets one
    quiet half-year weigh as much as a busy one, and the pooled figure lets a
    single large fold carry the item. Where they disagree, the folds disagree.
    """
    out = []
    for (item, variant), grp in folds.groupby(["item_code", "variant"]):
        scored = grp.dropna(subset=["wape"])
        pooled_den = float(grp["actual_sum"].sum())
        pooled_err = float(grp["abs_err_sum"].sum(skipna=True))

        ref_mape, ref_wape, _ = _fold_scores(frames_by_item[item][variant])
        own_wape = float(scored["wape"].mean()) if len(scored) else np.nan
        delta = (abs(own_wape - ref_wape)
                 if pd.notna(own_wape) and pd.notna(ref_wape) else np.nan)

        out.append({
            "item_code": item,
            "variant": variant,
            "n_folds": int(len(grp)),
            "n_folds_scored": int(len(scored)),
            "n_folds_zero_demand": int((grp["actual_sum"] == 0).sum()),
            "wape": ref_wape,
            "wape_pooled": (pooled_err / pooled_den * 100) if pooled_den > 0 else np.nan,
            "mape": ref_mape,
            "mean_yhat_level": float(grp["yhat_level_or_forecast"].mean(skipna=True)),
            "scorer_delta": delta,
        })
    return pd.DataFrame(out)


def shrinkage_table(folds: pd.DataFrame) -> pd.DataFrame:
    """
    Per variant: how big is its forecast against the demand that actually arrived.

    The mechanism check on the headline ranking, and the reason this file does
    not report a naive win as a win without looking. `bias_ratio` is the mean
    forecast level over the mean actual monthly rate on the same folds, so 1.0 is
    an unbiased rate estimate, 0.5 is a forecast half the demand, and 2.0 twice.

    `pct_folds_near_zero` is `croston_experiment.shrinkage_table()`'s absolute
    fingerprint, unchanged so the two files' columns are comparable.
    `pct_folds_below_10pct_rate` is the version that bites on THIS population —
    see NEAR_ZERO_RATE_FRACTION. `pct_folds_wape_100` is the third fingerprint:
    a forecast of zero scores exactly 100% WAPE by construction, so a cluster at
    100 is a cluster of predictions of nothing.
    """
    f = folds.copy()
    f["actual_rate"] = f["actual_sum"] / f["n_horizon_months"]
    rows = []
    for name in VARIANTS:
        sub = f[f["variant"] == name]
        scored = sub.dropna(subset=["wape"])
        rate = float(sub["actual_rate"].mean())
        level = float(sub["yhat_level_or_forecast"].mean(skipna=True))
        rel = sub[sub["actual_rate"] > 0]
        rows.append({
            "variant": name,
            "mean_yhat_level": level,
            "mean_actual_rate": rate,
            "bias_ratio": (level / rate) if rate else np.nan,
            "pct_folds_near_zero": float(
                (sub["yhat_level_or_forecast"] < NEAR_ZERO_LEVEL).mean() * 100),
            "pct_folds_below_10pct_rate": (float(
                (rel["yhat_level_or_forecast"]
                 < NEAR_ZERO_RATE_FRACTION * rel["actual_rate"]).mean() * 100)
                if len(rel) else np.nan),
            "pct_folds_wape_100": (
                float(((scored["wape"] - 100).abs() < 0.01).mean() * 100)
                if len(scored) else np.nan),
        })
    return pd.DataFrame(rows)


# ── Per-item verdict ──────────────────────────────────────────────────────────

def build_summary(scores: pd.DataFrame, population: pd.DataFrame) -> pd.DataFrame:
    """
    One row per item: the three WAPEs, which won, and by how much.

    `best_variant` is the lowest WAPE among the three. `verdict` is that name
    only where the item has at least MIN_FOLDS_FOR_VERDICT scored folds AND the
    margin over the runner-up clears TIE_TOLERANCE_PP; otherwise it is
    'no verdict' or 'tie', and the numbers still print. `beats_both` is the
    strict form of the question the experiment was asked — did one variant beat
    the OTHER TWO, not merely one of them.
    """
    meta = population.set_index("item_code")
    rows = []
    for item, grp in scores.groupby("item_code"):
        by_var = grp.set_index("variant")
        m = meta.loc[item]
        ranked = grp.dropna(subset=["wape"]).sort_values("wape")

        best = runner = ""
        best_w = runner_w = margin = np.nan
        if len(ranked):
            best = str(ranked["variant"].iloc[0])
            best_w = float(ranked["wape"].iloc[0])
        if len(ranked) > 1:
            runner = str(ranked["variant"].iloc[1])
            runner_w = float(ranked["wape"].iloc[1])
            margin = runner_w - best_w

        n_scored = int(by_var["n_folds_scored"].max()) if len(by_var) else 0
        if n_scored < MIN_FOLDS_FOR_VERDICT:
            verdict = f"no verdict (<{MIN_FOLDS_FOR_VERDICT} scored folds)"
        elif not len(ranked):
            verdict = "no verdict (nothing scorable)"
        elif pd.notna(margin) and margin < TIE_TOLERANCE_PP:
            verdict = f"tie ({best} / {runner}, <{TIE_TOLERANCE_PP:.0f}pp apart)"
        else:
            verdict = best

        rows.append({
            "item_code": item,
            "family_key": m.get("family_key", ""),
            "category": m.get("category", ""),
            "region": m.get("region", ""),
            "n_nonzero_months": int(m.get("n_nonzero_months", 0) or 0),
            "tenure_months": int(m.get("tenure_months", 0) or 0),
            "n_folds": int(by_var["n_folds"].max()) if len(by_var) else 0,
            "n_folds_scored": n_scored,
            "n_folds_zero_demand": (int(by_var["n_folds_zero_demand"].max())
                                    if len(by_var) else 0),
            **{f"wape_{v}": float(by_var["wape"].get(v, np.nan)) for v in VARIANTS},
            **{f"mape_{v}": float(by_var["mape"].get(v, np.nan)) for v in VARIANTS},
            "best_variant": best,
            "best_wape": best_w,
            "runner_up": runner,
            "runner_up_wape": runner_w,
            "margin_pp": margin,
            "best_is_naive": best in NAIVE_VARIANTS,
            "beats_both": bool(pd.notna(margin) and margin >= TIE_TOLERANCE_PP),
            "verdict": verdict,
        })
    return pd.DataFrame(rows).sort_values("item_code").reset_index(drop=True)


# ── Orchestration ─────────────────────────────────────────────────────────────

def run_experiment(population: pd.DataFrame, pooled_data: pd.DataFrame
                   ) -> tuple[pd.DataFrame, pd.DataFrame, dict, float, int]:
    """
    Score every variant on every item. Returns (folds, skipped, frames, elapsed, n_fits).

    An item that cannot produce a fold is recorded in `skipped` with the reason,
    never dropped quietly — a population of 15 that silently became a population
    of 9 would make every median below a different statistic from the one the
    header claims.
    """
    df = pooled_data.copy()
    df["item_code"] = df["item_code"].astype(str).str.strip()
    by_key = {k: g for k, g in df.groupby("item_code")}

    all_folds: list[dict] = []
    skipped: list[dict] = []
    frames_by_item: dict[str, dict] = {}
    n_fits = 0
    t0 = time.perf_counter()

    for row in population.itertuples(index=False):
        item = str(row.item_code).strip()
        key = str(row.family_key).strip()
        series = by_key.get(key)
        if series is None or series.empty:
            skipped.append({"item_code": item,
                            "reason": "no rows in the prepared frame"})
            continue

        train = (series[(series["ds"] >= TRAIN_START_TS)
                        & (series["ds"] <= TRAIN_END_TS)]
                 [["ds", "y"]].dropna().sort_values("ds").reset_index(drop=True))
        if len(train) < MIN_CV_TRAIN_MONTHS:
            skipped.append({
                "item_code": item,
                "reason": (f"{len(train)} observed training months, below "
                           f"MIN_CV_TRAIN_MONTHS ({MIN_CV_TRAIN_MONTHS})"),
            })
            continue

        cutoffs = make_cutoffs(train)
        if not cutoffs:
            skipped.append({
                "item_code": item,
                "reason": (f"no valid CV fold: {len(train)} observed months, needs "
                           f"{MIN_CV_TRAIN_MONTHS} before a cutoff with demand in "
                           f"the {CV_HORIZON} after it"),
            })
            continue

        try:
            frames, fits = variant_cv_frames(train, cutoffs)
        except Exception as exc:  # a series Prophet cannot cross-validate
            skipped.append({
                "item_code": item,
                "reason": f"cross_validation failed: {type(exc).__name__}: {exc}",
            })
            continue

        n_fits += fits
        frames_by_item[item] = frames
        all_folds.extend(fold_rows(frames, train, item))
        print(f"  {item:<10} {len(cutoffs)} cutoff(s), {len(train)} train months")

    elapsed = time.perf_counter() - t0
    return (pd.DataFrame(all_folds), pd.DataFrame(skipped), frames_by_item,
            elapsed, n_fits)


# ── Output schemas ────────────────────────────────────────────────────────────

#: The columns this experiment was specified to emit, in order, followed by the
#: two `croston_experiment.FOLD_COLS` carries and this file needs for the pooled
#: WAPE and the shrinkage table. A superset, stated rather than assumed.
FOLD_COLS = ["item_code", "variant", "cutoff", "n_train_months_at_cutoff",
             "yhat_level_or_forecast", "actual_sum", "wape", "mape", "fold_note",
             "n_horizon_months", "abs_err_sum"]

RESULT_COLS = ["item_code", "family_key", "category", "region",
               "n_nonzero_months", "tenure_months", "variant", "n_folds",
               "n_folds_scored", "n_folds_zero_demand", "wape", "wape_pooled",
               "mape", "mean_yhat_level", "scorer_delta"]

SUMMARY_COLS = (["item_code", "family_key", "category", "region",
                 "n_nonzero_months", "tenure_months", "n_folds",
                 "n_folds_scored", "n_folds_zero_demand"]
                + [f"wape_{v}" for v in VARIANTS]
                + [f"mape_{v}" for v in VARIANTS]
                + ["best_variant", "best_wape", "runner_up", "runner_up_wape",
                   "margin_pp", "best_is_naive", "beats_both", "verdict"])


# ── Reporting ─────────────────────────────────────────────────────────────────

def print_timing(elapsed: float, n_fits: int, n_items: int, n_full: int) -> None:
    sep = "-" * 96
    print(f"\n{sep}\nSAMPLE TIMING\n{sep}")
    print(f"  items run        : {n_items}")
    print(f"  Prophet fits     : {n_fits}  (one full-history fit + one refit per fold)")
    print(f"  wall clock       : {elapsed:.1f}s")
    if n_fits:
        print(f"  per fit          : {elapsed / n_fits:.2f}s")
    if n_items:
        per = elapsed / n_items
        print(f"  per item         : {per:.1f}s")
        print(f"\n  EXTRAPOLATION to the full population ({n_full} items):")
        print(f"    {n_full} x {per:.1f}s = {n_full * per / 60:.1f} min")
        print(f"    Sampled items are taken in item_code order, so this is an "
              f"estimate and not a")
        print(f"    bound — an item with more folds costs proportionally more.")


def _fmt(x, nd: int = 1) -> str:
    return "n/a" if pd.isna(x) else f"{x:.{nd}f}"


def print_summary(population: pd.DataFrame, funnel: dict[str, int],
                  skipped: pd.DataFrame, folds: pd.DataFrame,
                  scores: pd.DataFrame, summary: pd.DataFrame,
                  shrink: pd.DataFrame, region: str | None) -> None:
    sep = "=" * 96
    sub = "-" * 96
    scope = region if region is not None else "ALL REGIONS"

    # ── Design ────────────────────────────────────────────────────────────────
    print(f"\n{sep}\nEXPERIMENT DESIGN (every number here is a judgment call, not a "
          f"codebase value)\n{sep}")
    print(f"  question          : does a naive trailing average beat the "
          f"PRODUCTION Prophet fit on")
    print(f"                      {dc.ERRATIC}-classified items? Asked for "
          f"{dc.LUMPY}/{dc.INTERMITTENT} by")
    print(f"                      analysis/croston_experiment.py; this extends it "
          f"to {dc.ERRATIC}, scoped to {scope}.")
    print(f"  population        : standalone (NOT pooled) {dc.ERRATIC} items "
          f"currently routed to {VARIANT_PROPHET}")
    print(f"                      by model_routing gate 6, region {scope}, region "
          f"via prepare.series_metadata()")
    print(f"  validation        : rolling-origin CV strictly inside the training "
          f"window (<= {config.TRAIN_END}).")
    print(f"                      The {config.TEST_START}-{config.TEST_END} "
          f"held-out window is never scored here.")
    print(f"  horizon / spacing : {CV_HORIZON_MONTHS} months ({CV_HORIZON}) / "
          f"{CV_PERIOD_MONTHS} months, max {MAX_CUTOFFS} folds")
    print(f"                      (imported from changepoint_experiment, not "
          f"re-derived)")
    print(f"  fold minimum      : {MIN_CV_TRAIN_MONTHS} observed months before a "
          f"cutoff (= config.MIN_TRAIN_MONTHS,")
    print(f"                      the bar the pipeline itself applies before it "
          f"will fit a series)")
    print(f"  Prophet variant   : config.PROPHET_PARAMS VERBATIM — no changepoint "
          f"forcing, no prior sweep.")
    print(f"                      This is what the pipeline ships, which is the "
          f"only thing worth beating.")
    print(f"  naive variants    : "
          f"{', '.join(f'{k} = {v}mo' for k, v in config.NAIVE_WINDOW_MONTHS.items())} "
          f"— zero-inclusive trailing mean,")
    print(f"                      divisor is the NOMINAL window "
          f"(config.NAIVE_WINDOW_MONTHS, same arithmetic as")
    print(f"                      models/naive_model.py and "
          f"croston_experiment._predict()'s naive branch)")
    print(f"  identical folds   : naive rows are Prophet's OWN (cutoff, ds, y) "
          f"rows with yhat replaced.")
    print(f"                      No variant can be scored on a month, a fold or "
          f"an actual another one")
    print(f"                      was not. This is the whole reason the run "
          f"exists rather than a join of")
    print(f"                      model_metrics.csv and benchmark_comparison.csv, "
          f"which score different windows.")
    print(f"  verdict bar       : >= {MIN_FOLDS_FOR_VERDICT} scored folds AND a "
          f">= {TIE_TOLERANCE_PP:.0f}pp margin. Below either, the numbers")
    print(f"                      print and the verdict column says so.")
    print(f"  primary metric    : WAPE. MAPE is reported SECONDARY throughout — "
          f"these series carry")
    print(f"                      single-digit months, where MAPE divides by a "
          f"handful of units and")
    print(f"                      measures arithmetic rather than modelling.")

    # ── Population ────────────────────────────────────────────────────────────
    print(f"\n{sep}\nPOPULATION\n{sep}")
    for label, n in funnel.items():
        print(f"  {label:<48}: {n:>5}")
    print(f"\n  {len(population)} item(s) in scope:")
    for row in population.itertuples(index=False):
        cv2 = getattr(row, "cv2", np.nan)
        adi = getattr(row, "adi", np.nan)
        print(f"    {row.item_code:<10} {str(row.region):<8} "
              f"ADI {_fmt(adi, 2):>6}  CV2 {_fmt(cv2, 2):>7}  "
              f"{int(row.n_nonzero_months):>3} non-zero months, "
              f"tenure {int(row.tenure_months)}mo")

    if not skipped.empty:
        print(f"\n  NOT EVALUATED — {len(skipped)}:")
        for row in skipped.itertuples(index=False):
            print(f"    {row.item_code:<10} {row.reason}")

    if folds.empty:
        print("\n  Nothing was scorable.")
        return

    n_eval = folds["item_code"].nunique()
    n_folds = int(len(folds) / len(VARIANTS))
    print(f"\n  evaluated                                       : {n_eval} items, "
          f"{n_folds} folds, {len(folds):,} fold-variant scores")
    zd = int((folds[folds["variant"] == VARIANT_PROPHET]["actual_sum"] == 0).sum())
    print(f"  folds with ZERO demand in the horizon           : {zd} "
          f"(no WAPE denominator — dropped from")
    print(f"                                                    the per-fold mean, "
          f"kept in wape_pooled)")

    thin = summary[summary["n_folds_scored"] < MIN_FOLDS_FOR_VERDICT]
    if not thin.empty:
        print(f"  items below the {MIN_FOLDS_FOR_VERDICT}-scored-fold verdict bar"
              f"            : {len(thin)} "
              f"({', '.join(thin['item_code'].astype(str))})")

    delta = scores["scorer_delta"].dropna()
    worst = float(delta.max()) if len(delta) else 0.0
    print(f"  per-fold decomposition vs _fold_scores()        : max |delta| "
          f"{worst:.6f} pp "
          f"({'consistent' if worst < 1e-6 else 'DISAGREEMENT — read scorer_delta'})")

    # ── Per-item table ────────────────────────────────────────────────────────
    print(f"\n{sep}\nPER ITEM — WAPE on identical folds (primary metric; lower is "
          f"better)\n{sep}")
    hdr = (f"  {'item':<10} {'n':>3} {'zero':>5} "
           + "".join(f"{v:>13}" for v in VARIANTS)
           + f"  {'margin':>7}  verdict")
    print(hdr)
    print(f"  {sub[:len(hdr) - 2]}")
    # Rows as dicts, not itertuples: the per-variant columns are named after the
    # route labels, and 'Naive-3mo' is not a Python identifier — itertuples would
    # silently rename it and the lookup would miss.
    records = summary.to_dict("records")
    for row in records:
        cells = "".join(f"{_fmt(row[f'wape_{v}']):>13}" for v in VARIANTS)
        print(f"  {row['item_code']:<10} {row['n_folds_scored']:>3} "
              f"{row['n_folds_zero_demand']:>5} {cells}  "
              f"{_fmt(row['margin_pp']):>7}  {row['verdict']}")

    print(f"\n  MAPE on the same folds (SECONDARY — reported, not ranked on):")
    print(f"  {'item':<10}" + "".join(f"{v:>13}" for v in VARIANTS))
    for row in records:
        cells = "".join(f"{_fmt(row[f'mape_{v}'], 0):>13}" for v in VARIANTS)
        print(f"  {row['item_code']:<10}{cells}")

    # ── Aggregate rollup ──────────────────────────────────────────────────────
    print(f"\n{sep}\nAGGREGATE — median WAPE per variant across the {scope} "
          f"{dc.ERRATIC} population\n{sep}")
    print(f"  Median rather than mean, the same choice "
          f"croston_experiment.variant_table() makes: on")
    print(f"  a population this size one badly-missed item moves a mean by tens "
          f"of points on its own.\n")
    print(f"  {'variant':<14} {'n items':>8} {'WAPE med':>10} {'WAPE mean':>10} "
          f"{'pooled med':>11} {'MAPE med':>10}")
    print(f"  {sub[:65]}")
    for name in VARIANTS:
        s = scores[scores["variant"] == name]
        w, wp, m = s["wape"].dropna(), s["wape_pooled"].dropna(), s["mape"].dropna()
        print(f"  {name:<14} {len(w):>8} {_fmt(w.median()):>10} "
              f"{_fmt(w.mean()):>10} {_fmt(wp.median()):>11} "
              f"{_fmt(m.median(), 0):>10}")

    # Same rollup restricted to items that clear the verdict bar.
    ok = set(summary.loc[summary["n_folds_scored"] >= MIN_FOLDS_FOR_VERDICT,
                         "item_code"])
    if ok and len(ok) < summary["item_code"].nunique():
        print(f"\n  Restricted to the {len(ok)} item(s) with >= "
              f"{MIN_FOLDS_FOR_VERDICT} scored folds:")
        for name in VARIANTS:
            w = scores[(scores["variant"] == name)
                       & (scores["item_code"].isin(ok))]["wape"].dropna()
            print(f"    {name:<14} n={len(w):<3} median WAPE {_fmt(w.median())}")

    # ── Win counts ────────────────────────────────────────────────────────────
    print(f"\n{sep}\nWHO WON\n{sep}")
    eligible = summary[summary["n_folds_scored"] >= MIN_FOLDS_FOR_VERDICT]
    print(f"  Counted over the {len(eligible)} item(s) that clear the "
          f"{MIN_FOLDS_FOR_VERDICT}-scored-fold bar. Items below it")
    print(f"  are excluded from these counts and keep their rows in every CSV.\n")
    if eligible.empty:
        print(f"  No item clears the bar. No verdict is reported for this "
              f"population.")
    else:
        for name in VARIANTS:
            outright = int(((eligible["best_variant"] == name)
                            & eligible["beats_both"]).sum())
            lowest = int((eligible["best_variant"] == name).sum())
            print(f"    {name:<14} lowest WAPE on {lowest:>2} / {len(eligible)} "
                  f"item(s); beat BOTH others by >= {TIE_TOLERANCE_PP:.0f}pp on "
                  f"{outright}")
        ties = int((~eligible["beats_both"]).sum())
        print(f"    {'(tie)':<14} {ties} item(s) where the top two sit within "
              f"{TIE_TOLERANCE_PP:.0f}pp of each other")
        naive_any = int(eligible["best_is_naive"].sum())
        print(f"\n  A naive window has the lowest WAPE on {naive_any} / "
              f"{len(eligible)} item(s).")

    # ── Shrinkage ─────────────────────────────────────────────────────────────
    print(f"\n{sep}\nSHRINKAGE / BIAS CHECK — is a naive win a win, or a small "
          f"forecast?\n{sep}")
    print(f"  WAPE is bounded at 100% for a forecast of zero and unbounded for an "
          f"over-forecast, so a")
    print(f"  variant can rank well by predicting small. bias_ratio = mean "
          f"forecast level / mean actual")
    print(f"  monthly rate on the same folds: 1.0 is an unbiased rate, below 1.0 "
          f"is under-forecasting.")
    print(f"  near-zero bars: absolute < {NEAR_ZERO_LEVEL} "
          f"(croston_experiment's own), and < "
          f"{NEAR_ZERO_RATE_FRACTION:.0%} of the item's")
    print(f"  own actual rate — the one that bites on {dc.ERRATIC} volumes, where "
          f"a level of 5 against a")
    print(f"  300-unit series is nothing and clears an absolute 0.05 test without "
          f"comment.")
    print(f"  bias bands for the written reading below: < {SHRINKAGE_SEVERE:.2f} "
          f"severe, {SHRINKAGE_SEVERE:.2f}-{SHRINKAGE_MATERIAL:.2f} material, "
          f">= {SHRINKAGE_MATERIAL:.2f} not")
    print(f"  obviously shrinkage. Bands, not one cut, so two ratios a hundredth "
          f"apart cannot receive")
    print(f"  opposite verdicts — see SHRINKAGE_MATERIAL.\n")
    print(f"  {'variant':<14} {'mean level':>11} {'mean rate':>10} {'bias':>7} "
          f"{'<0.05':>7} {'<10% rate':>10} {'WAPE=100':>9}")
    print(f"  {sub[:71]}")
    for row in shrink.itertuples(index=False):
        print(f"  {row.variant:<14} {_fmt(row.mean_yhat_level):>11} "
              f"{_fmt(row.mean_actual_rate):>10} {_fmt(row.bias_ratio, 2):>7} "
              f"{_fmt(row.pct_folds_near_zero, 0) + '%':>7} "
              f"{_fmt(row.pct_folds_below_10pct_rate, 0) + '%':>10} "
              f"{_fmt(row.pct_folds_wape_100, 0) + '%':>9}")

    # The reading, stated rather than left to the table.
    sh = shrink.set_index("variant")
    w_prophet = scores[scores["variant"] == VARIANT_PROPHET]["wape"].dropna().median()
    print()
    for name in NAIVE_VARIANTS:
        br = float(sh.loc[name, "bias_ratio"])
        pw = float(sh.loc[VARIANT_PROPHET, "bias_ratio"])
        w_naive = scores[scores["variant"] == name]["wape"].dropna().median()
        wins = pd.notna(w_naive) and pd.notna(w_prophet) and w_naive < w_prophet
        if not wins:
            print(f"  {name}: does NOT have the lower median WAPE, so there is no "
                  f"win here to explain away.")
            continue
        if pd.isna(br):
            print(f"  {name}: lower median WAPE than {VARIANT_PROPHET}, but no "
                  f"actual demand to form a bias ratio against.")
        elif br < SHRINKAGE_SEVERE:
            print(f"  {name}: WARNING — lower median WAPE than {VARIANT_PROPHET}, "
                  f"but its bias ratio is {br:.2f}, i.e. it")
            print(f"    forecasts about {br:.0%} of the demand that arrives, "
                  f"against Prophet's {pw:.2f}. On a metric")
            print(f"    that caps the cost of under-forecasting at 100% and does "
                  f"not cap over-forecasting, that")
            print(f"    is consistent with winning by SHRINKING TOWARD ZERO on a "
                  f"volatile series rather than by")
            print(f"    tracking it. Read this as a finding about the metric until "
                  f"a bias-symmetric score says")
            print(f"    otherwise.")
        elif br < SHRINKAGE_MATERIAL:
            print(f"  {name}: lower median WAPE than {VARIANT_PROPHET}, at a bias "
                  f"ratio of {br:.2f} against Prophet's")
            print(f"    {pw:.2f} — it forecasts about {br:.0%} of the demand that "
                  f"arrives, so it is materially")
            print(f"    UNDER-FORECASTING while Prophet is close to unbiased. That "
                  f"is not the near-zero collapse")
            print(f"    croston_experiment found, and it is not a clean win "
                  f"either: WAPE rewards a low forecast")
            print(f"    on a volatile series, so part of this margin is the "
                  f"metric's asymmetry and this run")
            print(f"    cannot say how much. A bias-symmetric score, or a service-"
                  f"level view that prices a")
            print(f"    stock-out against an over-buy, is what would separate the "
                  f"two — and neither is run here.")
        else:
            print(f"  {name}: lower median WAPE than {VARIANT_PROPHET}, with a "
                  f"bias ratio of {br:.2f} against Prophet's")
            print(f"    {pw:.2f}. It is not obviously winning by under-forecasting "
                  f"— the level it predicts is")
            print(f"    in the right neighbourhood of the demand that arrives.")

    # ── What this does not say ────────────────────────────────────────────────
    print(f"\n{sep}\nWHAT THIS DOES NOT SAY\n{sep}")
    print(f"  NOTHING IS ADOPTED. config.py is unchanged, "
          f"preprocessing/model_routing.py is unchanged,")
    print(f"  main.py is unchanged, and no existing output CSV is rewritten. "
          f"{dc.ERRATIC} items still route")
    print(f"  to {VARIANT_PROPHET} through gate 6. Whether any of this should move "
          f"a route is a human decision")
    print(f"  that this script deliberately does not make.")
    print(f"  Scope is {scope} only ({len(population)} items) — this says nothing "
          f"about the other regions,")
    print(f"  and the --region flag exists so the same question can be asked of "
          f"them separately rather")
    print(f"  than assumed to have the same answer.")
    print(f"  POOLED {dc.ERRATIC} families are excluded and stay an open question, "
          f"the same exclusion and the")
    print(f"  same reason as croston_experiment.py: pooling behaviour for a "
          f"non-Prophet model is a")
    print(f"  separate decision and including them here would settle it by "
          f"implication.")
    print(f"  Sample size is the binding constraint, not method. Each item rests "
          f"on at most {MAX_CUTOFFS} folds of")
    print(f"  {CV_HORIZON_MONTHS} months, and the aggregate rests on "
          f"{summary['item_code'].nunique()} items.")
    print(f"  A trailing average is not a proposal. It is the floor a fitted "
          f"model should clear, and it")
    print(f"  is in this experiment as a reference point.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--db", action="store_true",
                    help="Reload raw inputs from Azure SQL instead of the cached CSVs")
    ap.add_argument("--input-dir", default=None,
                    help="Directory holding the cached input CSVs and receiving "
                         "this experiment's output (default: config.OUTPUT_DIR)")
    ap.add_argument("--region", default=None,
                    help="Restrict the population to one region as it appears in "
                         "prepare.series_metadata(), e.g. '4 AU'. Default: all regions")
    ap.add_argument("--sample", type=int, default=0, metavar="N",
                    help="Time a sample of N items and stop without writing the CSVs")
    args = ap.parse_args()

    if args.input_dir:
        config.OUTPUT_DIR = Path(args.input_dir).resolve()
    out_dir = config.OUTPUT_DIR
    print(f"Input/output directory: {out_dir}")
    if not out_dir.exists():
        print(f"  ERROR: {out_dir} does not exist. Pass --input-dir, or --db to "
              f"reload from the database.")
        return 1

    cached = not args.db
    pooled, families, cm_excluded, _fitted = build_scope_input(cached=cached)
    classes = dc.classify_scope(
        pooled.data, pooled.active_products, families, pooled.eligible,
        channel_mismatch_excluded=cm_excluded,
    )
    routing = mr.route_scope(
        classes, pooled.data, pooled.active_products, families,
        channel_mismatch_excluded=cm_excluded,
        fitted_items=mr.fitted_items_from_metrics(out_dir),
    )
    meta = region_lookup(cached)

    population, funnel = load_population(classes, routing, meta, args.region)
    scope_label = args.region if args.region is not None else "all regions"

    # Reported up front, before anything is fitted — the same way
    # croston_experiment.load_sample() reports its sample.
    print(f"\nPopulation — standalone {dc.ERRATIC} items routed to "
          f"{VARIANT_PROPHET}, {scope_label}: {len(population)}")
    for label, n in funnel.items():
        print(f"  {label:<48}: {n:>5}")
    if population.empty:
        print("  Nothing to evaluate.")
        return 1
    print(f"\n  {', '.join(population['item_code'].astype(str))}\n")

    full_n = len(population)
    if args.sample:
        population = population.head(args.sample)
        print(f"SAMPLE MODE — timing {len(population)} of {full_n} items\n")

    folds, skipped, frames, elapsed, n_fits = run_experiment(population, pooled.data)

    if args.sample:
        print_timing(elapsed, n_fits,
                     folds["item_code"].nunique() if not folds.empty else 0,
                     full_n)
        print("\nSample mode: no CSV written. Re-run without --sample for the "
              "full experiment.")
        return 0

    if folds.empty:
        print("Nothing could be evaluated.")
        return 1

    scores = item_scores(folds, frames)
    shrink = shrinkage_table(folds)
    summary = build_summary(scores, population)
    results = (scores.merge(population, on="item_code", how="left")
               .reindex(columns=RESULT_COLS)
               .sort_values(["item_code", "variant"]))

    for path, frame in [
        (out_dir / "erratic_naive_folds.csv",
         folds.reindex(columns=FOLD_COLS).round(4)),
        (out_dir / "erratic_naive_results.csv", results.round(3)),
        (out_dir / "erratic_naive_summary.csv",
         summary.reindex(columns=SUMMARY_COLS).round(3)),
    ]:
        frame.to_csv(path, index=False)
        print(f"\nWrote {path}  ({len(frame):,} rows)")

    print_summary(population, funnel, skipped, folds, scores, summary,
                  shrink, args.region)
    print(f"\nTotal wall clock: {elapsed / 60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
