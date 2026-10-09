"""
Croston/TSB experiment — which intermittent-demand model, and how many
occurrences does it actually need? (read-only)

WHAT THIS IS
────────────
`config.CROSTON_MIN_OCCURRENCES = 10` is, by its own comment, "a starting value
chosen by reasoning, not measurement": n occurrences give only n-1 observed
gaps, so below ~10 the interval estimate rests on a handful of intervals. That
is an argument, not evidence. `MIN_TRAIN_MONTHS`, `yearly_seasonality` and
`CHANGEPOINT_PRIOR_KNOWN_CHANGEOVER` all sat in exactly this position until
rolling-origin CV inside the training window settled them. This module gives
`CROSTON_MIN_OCCURRENCES` the same treatment, and answers the question that has
to be settled first: WHICH Croston-family estimator is being thresholded.

    PART A   CrostonClassic / CrostonOptimized / CrostonSBA / TSB (statsforecast),
             compared by rolling-origin CV on the standalone Lumpy+Intermittent
             population. WAPE is primary. MAPE is reported and labelled
             secondary — HOW_IT_WORKS.md's UN1300 case (MAPE in the thousands of
             percent) is exactly this population, and a metric that divides by a
             quantity of 1 is measuring arithmetic, not modelling.

    PART A'  The same Part A comparison, rerun SEPARATELY inside each trend
             subgroup instead of pooled. `bias_drivers()` already splits the
             population on `decline_ratio` to explain a bias; it never re-slices
             the win/lose-vs-baseline comparison itself. Part A' does, on the
             identical ratio and window, and reports the third bucket
             (`rate_prior_24m` undefined) that `bias_drivers` drops.

    PART B   The winning variant across occurrence-count buckets, to find where
             WAPE stops improving with more occurrences. That inflection is the
             proposed threshold.

    PART C   For items that are ALSO currently Prophet-fitted, the winning
             variant's CV WAPE next to Prophet's reported WAPE. CONTEXT ONLY —
             computed after A and B are decided, and read by neither.

Nothing here is adopted. No config value moves, `preprocessing/model_routing.py`
and `main.py` are not touched, nothing is fitted by the pipeline, and no existing
output is rewritten. Five new CSVs and a printed summary; the decision is a
human's.

WHY THE POPULATION IS STANDALONE-ONLY
─────────────────────────────────────
A pooled successor family is fitted as ONE series and divided back out by a
6-month split ratio. Whether an intermittent-demand model should inherit that
arrangement — and whether a split ratio derived from Prophet-era assumptions even
means the same thing for a Croston fit — is an open architectural question. It is
not answered here, and it would be answered *implicitly* by including pooled
families in the sample: a good result would read as "pooling works for Croston
too" when all it would show is that pooling was never tested against the
alternative. So `is_pooled` items are excluded and counted, and nothing in this
file has an opinion about them.

WHY ITEMS BELOW THE CURRENT THRESHOLD ARE IN THE SAMPLE
───────────────────────────────────────────────────────
Sampling only items that clear `CROSTON_MIN_OCCURRENCES = 10` would confirm the
placeholder by construction — the curve cannot bend where there are no points.
Every standalone Lumpy/Intermittent item is in, occurrence count regardless,
including the six the current gate blocks.

TWO DELIBERATE DEVIATIONS FROM changepoint_experiment's CUTOFF GEOMETRY
───────────────────────────────────────────────────────────────────────
Origin spacing (`CV_PERIOD_MONTHS = 3`), fold cap (`MAX_CUTOFFS = 6`) and horizon
(`CV_HORIZON_MONTHS = 6`) are imported from `analysis.changepoint_experiment`,
not re-typed. Two rules there are Prophet-API artifacts and are NOT reproduced:

  1. `MIN_CV_TRAIN_MONTHS = config.MIN_TRAIN_MONTHS` (24 CALENDAR months before a
     cutoff). That bar exists because Prophet needs two yearly cycles to have a
     seasonality to find. Croston has no seasonality and no calendar opinion — it
     estimates a size and an interval from occurrences. Applying 24 calendar
     months here would drop E91807, U52080, A35309, AN1450 and A21088 from the
     sample outright, i.e. exactly the low-occurrence end Part B exists to
     measure, and the threshold curve would then be truncated by the fold rule
     rather than by the data. The analogous floor is on OCCURRENCES, and it is
     set to `MIN_CV_OCCURRENCES = 2` — the point at which a Croston fit is
     defined at all (one demand size, one observed interval), deliberately far
     below any candidate threshold so the fold rule cannot pre-decide Part B.

  2. The newest cutoff sits SIX months before the series end, not seven.
     `changepoint_experiment`'s seven is forced by `prophet.diagnostics.
     cross_validation` rejecting `cutoff > max(ds) - horizon` when the horizon is
     a 190-day Timedelta standing in for six calendar months — its own docstring
     calls that "the price of a horizon that is uniform across folds". This
     module forecasts a monthly grid directly and takes exactly six monthly
     points, so the constraint does not exist. Reproducing it would discard the
     most recent six-month fold for the sake of an API this code does not call.

Everything else is the same shape: rolling origins strictly inside the training
window, each fold fitted on months <= its cutoff and scored on six months it
never saw, and the 2026-01 → 2026-06 held-out window never read as a score.

THE ZERO-FILL, WHICH IS NOT OPTIONAL HERE
─────────────────────────────────────────
`prepare()` emits no zero-quantity rows, so a month with no sales is ABSENT from
the frame rather than zero. Prophet is indifferent — it fits scattered points.
Croston is not: its entire input is the pattern of zeros and non-zeros, and
handed a frame of non-zero months only it would see a series that sells every
month and reduce to plain exponential smoothing. Every series is therefore
reindexed onto a complete monthly grid and the gaps filled with 0.

The grid starts at the series' FIRST NON-ZERO MONTH, not at `config.TRAIN_START`
— the same convention `demand_classification._tenure_months()` uses, and for the
same reason: a code launched in 2025 has not been failing to sell since 2018, it
did not exist, and padding it with seven years of manufactured zeros would drive
its demand-probability estimate toward zero on the strength of its launch date.

WAPE, AND THE FOLDS WHERE IT IS UNDEFINED
─────────────────────────────────────────
`_fold_scores` in `changepoint_experiment` computes per-fold WAPE and averages
over folds; that convention is kept, so per-item WAPE means the same thing here
as there. On this population it has an edge case that does not arise there: a
six-month fold in which the item sold NOTHING has a zero denominator and no
defined WAPE. Those folds are dropped from the per-fold mean and counted on
every row (`n_folds_zero_demand`).

Dropping them is not neutral. Croston-family models always predict a positive
constant, so a zero-demand fold is pure error, and excluding it systematically
favours whichever variant forecasts HIGHER. So every item also carries
`wape_pooled` — one WAPE over all fold-months at once, which is defined as long
as the item sold anything across the whole CV span and therefore keeps the
zero-demand folds in. If the two disagree about which variant wins, the summary
says so instead of reporting the convenient one.

THE SHRINKAGE DIAGNOSTIC, AND WHY IT IS REPORTED NEXT TO THE RANKING
────────────────────────────────────────────────────────────────────
WAPE is asymmetric on this population in a way it is not on Prophet's. A flat
forecast of ZERO scores exactly 100% WAPE, always — the errors are the actuals.
There is no such ceiling in the other direction: a forecast of twice the true
rate on a series that then sells nothing for four of six months scores several
hundred. So a variant can climb this ranking by forecasting SMALLER rather than
by forecasting better, and the ranking alone cannot tell those apart.

Every variant therefore also carries its mean forecast level against the mean
actual demand rate over the same folds (`bias_ratio`), the share of folds where
it predicted essentially nothing, and the share of scored folds landing on
exactly 100% WAPE. If the WAPE ordering turns out to be the bias ordering, the
experiment has measured the metric's asymmetry and the summary says so instead
of announcing a winner.

Where a bias shows up, `bias_drivers()` asks what it TRACKS, because two very
different diagnoses produce the same over-forecast and they call for opposite
responses:

  * a stale level — Croston updates only in months with a demand occurrence and
    so cannot decay through a zero run, while TSB decays its demand probability
    every zero month and a trailing mean forgets outright. On an obsolescing
    population Croston holds a rate the series no longer has. The fix is an
    estimator that forgets.
  * the estimator's own arithmetic — Croston forecasts smoothed SIZE over
    smoothed INTERVAL, and the expectation of that ratio runs above the ratio of
    the expectations by more the longer the intervals get. This is the textbook
    Croston inversion bias, and it is what `CrostonSBA` exists to correct with a
    fixed `1 - alpha/2 = 0.95` factor. The fix is a debiased estimator.

They are separable: the first predicts bias tracking the item's rate of decline,
the second predicts bias tracking its ADI. Both are measured and both are
reported, rather than the mechanism being asserted from whichever result appeared
first. On the current data the decline relationship is much the stronger of the
two, which also explains why Part B finds no occurrence threshold: an error that
comes from a level being STALE is not one that more occurrences fix.

THE BASELINE ROW
────────────────
`naive_mean_12m` — the mean of the last twelve calendar months (zeros included)
before each cutoff, held flat. It is NOT a candidate and is excluded from the
Part A pick and the Part B curve. It is here because "WAPE stabilises at 78%" is
unreadable without knowing what arithmetic alone scores on the same folds: a
threshold above which a model does not beat a trailing average is not a threshold
worth having.

`naive_mean_3m` — the same arithmetic over a three-month window, and equally not
a candidate. It is here for the Part A' declining bucket, where TSB's WAPE kept
improving as its alpha rose to the edge of the grid with no plateau. That shape
says the advantage may be about reacting to recent months quickly rather than
about the Croston/TSB estimator, and a shorter trailing mean is the cheapest way
to ask: it has no fitted parameter, and at three months it reacts SLOWER than the
alphas it is set against, so a gap it closes is a gap a faster-reacting naive was
enough to close. It is a reference point, not a proposal, on the same terms as
the twelve-month row.

Run:  python -m analysis.croston_experiment --input-dir output_phase1
      python -m analysis.croston_experiment --db
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from analysis.changepoint_experiment import (  # noqa: E402  — reused, not re-derived
    CV_HORIZON_MONTHS,
    CV_PERIOD_MONTHS,
    MAX_CUTOFFS,
)
from analysis.demand_classification_report import build_scope_input  # noqa: E402
from preprocessing import demand_classification as dc  # noqa: E402

from statsforecast.models import (  # noqa: E402
    TSB,
    CrostonClassic,
    CrostonOptimized,
    CrostonSBA,
)


# ── Experiment design constants (judgment calls, all stated in the summary) ────

#: Occurrences a fold's own history must contain before it may be scored. Two is
#: the floor at which a Croston fit exists at all — one demand size, one observed
#: interval. See the module docstring for why this is NOT config.MIN_TRAIN_MONTHS.
MIN_CV_OCCURRENCES = 2

#: TSB takes no default smoothing parameters, so a choice has to be made and it
#: cannot be hidden. 0.1 is `CrostonClassic`'s own fixed alpha in statsforecast,
#: so `tsb_a0.1` isolates the METHOD (TSB tracks demand probability and decays it
#: in zero months; Croston tracks an interval and does not) rather than the
#: smoothing rate. The rest are carried so a TSB loss is not really a loss to one
#: arbitrary alpha. Both parameters are moved together — a full (alpha_d, alpha_p)
#: grid would be per-population tuning of a value nothing has proposed.
#:
#: 0.5 is in the grid for one reason: the same standard `config.py` applies to
#: CHANGEPOINT_PRIOR_KNOWN_CHANGEOVER, where 0.5 was rejected for being "a
#: boundary solution the grid does not bracket". If the best alpha is the largest
#: one tested, the grid has not found an optimum, it has found a direction — and
#: a direction toward faster forgetting is a finding about the METRIC (see the
#: shrinkage diagnostic) rather than about TSB.
#:
#: 0.7 and 0.9 are here because that is exactly what happened. In the Part A'
#: declining bucket median WAPE fell monotonically across the whole original grid
#: — 260 -> 184 -> 153 -> 132 at 0.1/0.2/0.3/0.5 — with the best value sitting on
#: the edge, which is the boundary shape the paragraph above says not to read as
#: an answer. The grid is extended rather than the boundary result reported,
#: because the two possible continuations mean opposite things:
#:
#:   * a PLATEAU or REVERSAL by 0.9 brackets a real interior optimum. There is
#:     then a genuine best forgetting rate for this population, and TSB's ranking
#:     is about the estimator rather than about how small its forecasts are.
#:   * STILL FALLING at 0.9 is stronger evidence for the shrinkage reading, not
#:     weaker. As alpha -> 1 TSB stops smoothing altogether and degenerates toward
#:     "predict the last observed demand rate", which on an obsolescing series
#:     that has just run a string of zero months means predicting near zero — and
#:     a forecast of zero scores exactly 100% WAPE while an over-forecast has no
#:     such ceiling. A curve that keeps improving all the way into that limit is
#:     measuring the metric's asymmetry, not skill, and no alpha in it should be
#:     adopted.
#:
#: 0.9 rather than 1.0 as the top of the grid: at exactly 1.0 the smoothing
#: recursion has no memory at all and the "estimator" is not one, so it would
#: illustrate the degenerate case instead of testing the approach to it.
TSB_ALPHAS = [0.1, 0.2, 0.3, 0.5, 0.7, 0.9]

#: Trailing window for the context baseline. Twelve calendar months INCLUDING
#: zero months — the same convention as `analysis.trend_audit._window_average()`,
#: which divides by the calendar count and not by the rows present.
BASELINE_WINDOW_MONTHS = 12
BASELINE = "naive_mean_12m"

#: A second, SHORTER naive reference. Three months, same zero-inclusive
#: convention as the twelve-month one.
#:
#: It exists to test one specific reading of the Part A' declining-bucket result.
#: The TSB_ALPHAS block above records that median WAPE there kept falling as
#: alpha rose toward 0.9 with no plateau — a boundary, not an optimum. That shape
#: is itself evidence that whatever edge the estimator has in that bucket is
#: about REACTING TO VERY RECENT MONTHS rather than about anything specific to
#: Croston/TSB, and that claim can be tested by a trailing mean with no fitted
#: parameter at all: shorten the window and see how much of the gap closes.
#:
#: Three months rather than something tuned to match. Note the direction of the
#: conservatism: TSB at alpha 0.7-0.9 has an effective memory of roughly 1/alpha
#: months, i.e. about 1-1.4 months, so a 3-month flat window is SLOWER to react
#: than the alphas it is being compared against, not faster. If three months
#: already closes most of the gap, that is if anything an understatement of the
#: "a faster-reacting naive is enough" reading, not a window picked to produce it.
NAIVE_SHORT_WINDOW_MONTHS = 3
NAIVE_SHORT = "naive_mean_3m"

#: Both naive rows are the same arithmetic at two window lengths, so `_predict`
#: runs them through one branch. The divisor is the NOMINAL window and not the
#: number of months actually present — a history shorter than the window is
#: divided by the window anyway. That is the existing twelve-month behaviour,
#: generalised rather than changed.
NAIVE_WINDOWS = {BASELINE: BASELINE_WINDOW_MONTHS,
                 NAIVE_SHORT: NAIVE_SHORT_WINDOW_MONTHS}

CROSTON_CLASSIC = "croston_classic"
CROSTON_OPTIMIZED = "croston_optimized"
CROSTON_SBA = "croston_sba"


def _tsb_name(alpha: float) -> str:
    return f"tsb_a{alpha}"


#: Every variant, in report order. The context rows are appended separately
#: wherever they appear so neither can be picked as a winner by accident.
CANDIDATES = [CROSTON_CLASSIC, CROSTON_OPTIMIZED, CROSTON_SBA] + [
    _tsb_name(a) for a in TSB_ALPHAS
]

#: Scored and reported everywhere a candidate is, eligible to win nothing.
CONTEXT_ROWS = [BASELINE, NAIVE_SHORT]

#: Bucket edges for the per-ITEM occurrence curve (Part B), as [lo, hi) pairs.
#: Sized off the actual population rather than chosen round: the standalone
#: Lumpy/Intermittent occurrence counts are dense above 20 and sparse below 12,
#: so the low end is cut fine enough to see a bend and the high end is pooled
#: wide enough that each bucket holds more than a couple of items. `print_summary`
#: prints the realised n per bucket so a bucket that ended up thin is visible.
ITEM_BUCKETS = [(4, 8), (8, 12), (12, 16), (16, 20), (20, 30), (30, 50), (50, 999)]

#: Bucket edges for the per-FOLD curve. Finer, because it has ~6x the points:
#: every fold contributes its own "occurrences the model had at this cutoff".
FOLD_BUCKETS = [(2, 4), (4, 6), (6, 8), (8, 10), (10, 12), (12, 16), (16, 24),
                (24, 40), (40, 999)]

#: A bucket counts as "stabilised" when no bucket above it improves median WAPE by
#: more than this many percentage points. The rule is stated rather than eyeballed
#: so the proposed threshold is reproducible from the CSV. 5pp is a judgment call:
#: it is the scale below which a difference in median WAPE across ~5-item buckets
#: is not distinguishable from which items happened to land in which bucket.
STABILISATION_TOLERANCE_PP = 5.0

#: The item the current placeholder sits directly above, named in the summary
#: whichever side of the validated threshold it lands on.
FOCUS_ITEM = "A21088"


# ── Sample selection ──────────────────────────────────────────────────────────

def load_sample(classes: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Standalone Lumpy/Intermittent items, and the pooled ones excluded alongside.

    Returns (sample, excluded_pooled). Both are returned so the exclusion is
    reported as a number rather than happening silently — see the module
    docstring for why pooled families are not in this experiment.
    """
    li = classes[classes["category"].isin([dc.LUMPY, dc.INTERMITTENT])].copy()
    sample = li[~li["is_pooled"].astype(bool)].copy()
    excluded = li[li["is_pooled"].astype(bool)].copy()

    cols = ["item_code", "family_key", "category", "adi", "cv2",
            "n_nonzero_months", "tenure_months", "near_threshold"]
    sample = sample[cols].sort_values("n_nonzero_months").reset_index(drop=True)
    return sample, excluded[cols]


def monthly_grid(series: pd.DataFrame, anchor: pd.Timestamp) -> pd.DataFrame:
    """
    One row per calendar month from the first non-zero month to `anchor`, zeros
    filled in. See the module docstring — this is what makes the series legible
    to a Croston-family model at all.
    """
    s = series[["ds", "y"]].copy()
    s["ds"] = pd.to_datetime(s["ds"])
    s = s[s["ds"] <= anchor]
    s = s.groupby("ds", as_index=False)["y"].sum()
    nz = s[s["y"] > 0]
    if nz.empty:
        return pd.DataFrame(columns=["ds", "y"])

    idx = pd.date_range(nz["ds"].min(), anchor, freq="MS")
    out = (s.set_index("ds").reindex(idx, fill_value=0.0)
           .rename_axis("ds").reset_index())
    out["y"] = out["y"].astype(float).clip(lower=0)
    return out


# ── Cutoff construction ───────────────────────────────────────────────────────

def make_cutoffs(grid: pd.DataFrame) -> list[pd.Timestamp]:
    """
    Rolling origins inside the training window, oldest first.

    Same spacing and fold cap as `changepoint_experiment.make_cutoffs`; the two
    rules that differ from it are the two deviations documented in the module
    docstring. A cutoff survives only if:
      * its own history holds at least MIN_CV_OCCURRENCES non-zero months (below
        that the model is undefined, not merely uncertain),
      * the six months after it are all present in the grid — a partial fold
        would be scored on fewer points than the others and would not be
        comparable across variants at different cutoffs,
      * it is strictly later than the first observed month.
    """
    if grid.empty:
        return []

    max_ds, min_ds = grid["ds"].max(), grid["ds"].min()
    newest = max_ds - pd.DateOffset(months=CV_HORIZON_MONTHS)

    cutoffs: list[pd.Timestamp] = []
    for i in range(MAX_CUTOFFS):
        c = newest - pd.DateOffset(months=CV_PERIOD_MONTHS * i)
        if c <= min_ds:
            break
        hist = grid[grid["ds"] <= c]
        if int((hist["y"] > 0).sum()) < MIN_CV_OCCURRENCES:
            break
        horizon = grid[(grid["ds"] > c)
                       & (grid["ds"] <= c + pd.DateOffset(months=CV_HORIZON_MONTHS))]
        if len(horizon) < CV_HORIZON_MONTHS:
            continue
        cutoffs.append(c)

    return sorted(cutoffs)


# ── The variants ──────────────────────────────────────────────────────────────

def _predict(name: str, history: np.ndarray) -> float:
    """
    One variant's flat forecast level for the six months after a cutoff.

    Every Croston-family estimator here produces a CONSTANT over the horizon —
    a demand rate, not a trajectory — so a single float is the whole forecast and
    is broadcast across the six months by the caller.
    """
    if name in NAIVE_WINDOWS:
        months = NAIVE_WINDOWS[name]
        window = history[-months:]
        return float(window.sum()) / months

    if name == CROSTON_CLASSIC:
        model = CrostonClassic()
    elif name == CROSTON_OPTIMIZED:
        model = CrostonOptimized()
    elif name == CROSTON_SBA:
        model = CrostonSBA()
    elif name.startswith("tsb_a"):
        alpha = float(name.removeprefix("tsb_a"))
        model = TSB(alpha_d=alpha, alpha_p=alpha)
    else:
        raise ValueError(f"croston_experiment: unknown variant {name!r}")

    yhat = model.fit(history).predict(CV_HORIZON_MONTHS)["mean"]
    return float(np.asarray(yhat, dtype=float)[0])


# ── Scoring ───────────────────────────────────────────────────────────────────

def fold_rows(grid: pd.DataFrame, cutoffs: list[pd.Timestamp],
              item: str) -> list[dict]:
    """
    Every (cutoff x variant) fold for one item, scored.

    `yhat` is clipped at zero to match what `models.prophet_model.forecast_series`
    ships — a negative forecast is never delivered, so scoring one would score a
    model the pipeline would not run. Croston-family output is non-negative
    anyway; the clip is there so the convention is identical to
    `changepoint_experiment._fold_scores` rather than assumed away.

    `n_occurrences_at_cutoff` is the number the Part B per-fold curve is bucketed
    on: the occurrences the model ACTUALLY had when it was fitted, which is what
    `CROSTON_MIN_OCCURRENCES` is really a statement about.
    """
    rows: list[dict] = []
    for c in cutoffs:
        hist = grid.loc[grid["ds"] <= c, "y"].to_numpy(dtype=float)
        actual = grid.loc[
            (grid["ds"] > c)
            & (grid["ds"] <= c + pd.DateOffset(months=CV_HORIZON_MONTHS)), "y"
        ].to_numpy(dtype=float)

        n_occ = int((hist > 0).sum())
        denom = float(np.abs(actual).sum())

        for name in CANDIDATES + CONTEXT_ROWS:
            try:
                level = max(_predict(name, hist), 0.0)
            except Exception as exc:  # a variant that cannot fit this history
                rows.append({
                    "item_code": item, "cutoff": c, "variant": name,
                    "n_occurrences_at_cutoff": n_occ, "n_horizon_months": len(actual),
                    "yhat_level": np.nan, "actual_sum": denom,
                    "abs_err_sum": np.nan, "wape": np.nan, "mape": np.nan,
                    "fold_note": f"fit failed: {type(exc).__name__}",
                })
                continue

            err = np.abs(actual - level)
            nz = actual != 0
            rows.append({
                "item_code": item, "cutoff": c, "variant": name,
                "n_occurrences_at_cutoff": n_occ, "n_horizon_months": len(actual),
                "yhat_level": level, "actual_sum": denom,
                "abs_err_sum": float(err.sum()),
                "wape": float(err.sum() / denom * 100) if denom > 0 else np.nan,
                "mape": (float((err[nz] / np.abs(actual[nz])).mean() * 100)
                         if nz.any() else np.nan),
                "fold_note": "zero_demand_fold" if denom == 0 else "",
            })
    return rows


def item_scores(folds: pd.DataFrame) -> pd.DataFrame:
    """
    Collapse folds to one row per (item, variant).

    `wape` follows `changepoint_experiment._fold_scores`: the mean of the
    per-fold WAPEs, folds with a zero denominator dropped. `wape_pooled` is the
    same errors summed over every fold-month and divided by every fold-month's
    actual — which keeps the zero-demand folds in. See the module docstring for
    why both are carried and never just the first.
    """
    out = []
    for (item, variant), grp in folds.groupby(["item_code", "variant"]):
        scored = grp.dropna(subset=["wape"])
        pooled_den = float(grp["actual_sum"].sum())
        pooled_err = float(grp["abs_err_sum"].sum(skipna=True))
        out.append({
            "item_code": item,
            "variant": variant,
            "n_folds": int(len(grp)),
            "n_folds_scored": int(len(scored)),
            "n_folds_zero_demand": int((grp["actual_sum"] == 0).sum()),
            "wape": float(scored["wape"].mean()) if len(scored) else np.nan,
            "wape_pooled": (pooled_err / pooled_den * 100) if pooled_den > 0 else np.nan,
            "mape": (float(grp["mape"].dropna().mean())
                     if grp["mape"].notna().any() else np.nan),
            "mean_yhat_level": float(grp["yhat_level"].mean(skipna=True)),
        })
    return pd.DataFrame(out)


# ── Part A ────────────────────────────────────────────────────────────────────

def variant_table(scores: pd.DataFrame) -> pd.DataFrame:
    """
    Mean and median WAPE per variant across items, plus the pooled-WAPE and MAPE
    columns.

    Median is the column the pick is made on. Same reasoning the rest of this
    project applies to skewed series: a handful of items whose actuals average
    under two units a month can move a mean by tens of points on their own, and
    a variant should not be chosen by which one it happened to miss.
    """
    rows = []
    for name in CANDIDATES + CONTEXT_ROWS:
        sub = scores[scores["variant"] == name]
        w = sub["wape"].dropna()
        wp = sub["wape_pooled"].dropna()
        m = sub["mape"].dropna()
        rows.append({
            "variant": name,
            "is_candidate": name in CANDIDATES,
            "n_items": int(len(w)),
            "wape_median": float(w.median()) if len(w) else np.nan,
            "wape_mean": float(w.mean()) if len(w) else np.nan,
            "wape_pooled_median": float(wp.median()) if len(wp) else np.nan,
            "wape_pooled_mean": float(wp.mean()) if len(wp) else np.nan,
            "mape_median": float(m.median()) if len(m) else np.nan,
            "mape_mean": float(m.mean()) if len(m) else np.nan,
        })
    return pd.DataFrame(rows)


def shrinkage_table(folds: pd.DataFrame) -> pd.DataFrame:
    """
    Per variant: how big is its forecast against the demand that actually arrived.

    The mechanism check on Part A. `bias_ratio` is the mean forecast level over
    the mean actual monthly rate across the same folds, so 1.0 is an unbiased
    rate estimate and 2.0 is a forecast twice the demand. `pct_folds_near_zero`
    and `pct_folds_wape_100` are the two fingerprints of winning by shrinkage:
    a forecast of zero scores exactly 100% WAPE by construction.
    """
    f = folds.copy()
    f["actual_rate"] = f["actual_sum"] / f["n_horizon_months"]
    rows = []
    for name in CANDIDATES + CONTEXT_ROWS:
        sub = f[f["variant"] == name]
        scored = sub.dropna(subset=["wape"])
        rate = float(sub["actual_rate"].mean())
        level = float(sub["yhat_level"].mean(skipna=True))
        rows.append({
            "variant": name,
            "is_candidate": name in CANDIDATES,
            "mean_yhat_level": level,
            "mean_actual_rate": rate,
            "bias_ratio": (level / rate) if rate else np.nan,
            "pct_folds_near_zero": float((sub["yhat_level"] < 0.05).mean() * 100),
            "pct_folds_wape_100": (float(((scored["wape"] - 100).abs() < 0.01).mean() * 100)
                                   if len(scored) else np.nan),
        })
    return pd.DataFrame(rows)


def bias_drivers(folds: pd.DataFrame, sample: pd.DataFrame,
                 facts: pd.DataFrame, variant: str) -> dict:
    """
    What one variant's per-item over-forecast tracks: ADI, CV^2, or decline.

    The follow-up to `shrinkage_table`. A bias that grows with ADI is the
    estimator's own arithmetic (Croston's rate is a ratio of two smoothed
    quantities, and the ratio's expectation runs above the expectations' ratio
    the longer the intervals). A bias that grows as demand declines would instead
    be a stale-level problem, fixable by any estimator that decays. The two
    diagnoses call for opposite responses, so which one the data supports is
    worth a correlation rather than an assumption.

    `decline_ratio` is the item's demand rate over the last TREND_WINDOW_MONTHS
    against the window before it — below 1 means shrinking.
    """
    f = folds[(folds["variant"] == variant)].copy()
    f["rate"] = f["actual_sum"] / f["n_horizon_months"]
    per_item = f.groupby("item_code").agg(level=("yhat_level", "mean"),
                                          rate=("rate", "mean"))
    per_item = per_item[per_item["rate"] > 0]
    per_item["bias"] = per_item["level"] / per_item["rate"]

    j = per_item.join(sample.set_index("item_code")[["adi", "cv2"]])
    if not facts.empty:
        fc = facts.set_index("item_code")
        decline = fc["rate_recent_24m"] / fc["rate_prior_24m"].replace(0, np.nan)
        j = j.join(decline.rename("decline_ratio"))

    def _rho(col: str) -> float:
        sub = j[["bias", col]].replace([np.inf, -np.inf], np.nan).dropna()
        if len(sub) < 3:
            return float("nan")
        return float(sub.corr(method="spearman").iloc[0, 1])

    out = {"variant": variant, "n_items": int(len(j)),
           "median_bias": float(j["bias"].median()),
           "rho_adi": _rho("adi"), "rho_cv2": _rho("cv2")}
    if "decline_ratio" in j.columns:
        sub = j.replace([np.inf, -np.inf], np.nan).dropna(subset=["decline_ratio"])
        out["rho_decline"] = _rho("decline_ratio")
        out["n_declining"] = int((sub["decline_ratio"] < 1).sum())
        out["bias_declining"] = (float(sub.loc[sub["decline_ratio"] < 1, "bias"].median())
                                 if (sub["decline_ratio"] < 1).any() else np.nan)
        out["bias_stable"] = (float(sub.loc[sub["decline_ratio"] >= 1, "bias"].median())
                              if (sub["decline_ratio"] >= 1).any() else np.nan)
    return out


def pick_winner(table: pd.DataFrame) -> tuple[str, str]:
    """
    The candidate with the lowest MEDIAN WAPE, and the winner under the pooled
    metric for the cross-check. The baseline is never eligible.

    Returns ("", "") when no candidate has a defined median WAPE. Part A never
    reaches that branch — the pooled table always has scorable items — but Part A'
    reruns this on trend subgroups, and a subgroup can contain only dormant items
    and therefore no defined WAPE at all. Returning empty is what lets that bucket
    be REPORTED as having no winner rather than raising or being dropped.
    """
    cands = table[table["is_candidate"]].dropna(subset=["wape_median"])
    if cands.empty:
        return "", ""
    winner = str(cands.loc[cands["wape_median"].idxmin(), "variant"])
    pooled = cands.dropna(subset=["wape_pooled_median"])
    pooled_winner = (str(pooled.loc[pooled["wape_pooled_median"].idxmin(), "variant"])
                     if not pooled.empty else "")
    return winner, pooled_winner


# ── Part A' ───────────────────────────────────────────────────────────────────

#: A bucket holding this many scored items or fewer is reported WITHOUT a verdict.
#: Not a new judgment call: the closing section of this file already states that
#: "the low-occurrence buckets Part B turns on hold 2-4 items each" and treats
#: that as the point where a median stops being readable. The same bar is applied
#: here rather than a second, more convenient one being invented for Part A'.
THIN_BUCKET_MAX_ITEMS = 4

DECLINING = "declining"
STABLE_GROWING = "stable_or_growing"
NO_PRIOR_WINDOW = "insufficient_history"
NO_PRIOR_DEMAND = "prior_window_all_zero"
NO_TREND_FACTS = "no_trend_facts"

#: Report order. The last two exist so that nothing can fall out of the split
#: silently — see `assign_decline_bucket`. They are printed only when non-empty,
#: and their n is stated when they are.
SPLIT_BUCKET_ORDER = [DECLINING, STABLE_GROWING, NO_PRIOR_WINDOW,
                      NO_PRIOR_DEMAND, NO_TREND_FACTS]

SPLIT_BUCKET_RULE = {
    DECLINING: "decline_ratio < 1.00",
    STABLE_GROWING: "decline_ratio >= 1.00",
    NO_PRIOR_WINDOW: "rate_prior_24m undefined — no full prior window",
    NO_PRIOR_DEMAND: "prior window present but sold nothing in it",
    NO_TREND_FACTS: "no row in facts (empty monthly grid)",
}


def item_decline_ratio(facts: pd.DataFrame) -> pd.DataFrame:
    """
    Per item: `decline_ratio`, and whether a prior window existed to divide by.

    The ratio is derived here EXACTLY as `bias_drivers` derives it — the same
    `series_facts` columns, the same TREND_WINDOW_MONTHS windows, the same
    `.replace(0, np.nan)` on the denominator. Nothing is recomputed from the
    grids, so Part A's bias diagnostic and Part A's buckets cannot drift apart.

    `has_prior_window` is carried separately because the ratio alone cannot tell
    the two undefined cases apart: an item with no prior 24-month window at all,
    and an item that had one and sold nothing in it. `bias_drivers` folds both
    into a `dropna` and never counts them; Part A' separates and reports them.
    """
    cols = ["decline_ratio", "has_prior_window"]
    if facts.empty:
        return pd.DataFrame(columns=cols).rename_axis("item_code")

    fc = facts.set_index("item_code")
    out = pd.DataFrame(index=fc.index)
    out["decline_ratio"] = (fc["rate_recent_24m"]
                            / fc["rate_prior_24m"].replace(0, np.nan))
    out["has_prior_window"] = fc["rate_prior_24m"].notna()
    return out[cols]


def assign_decline_bucket(scores: pd.DataFrame, facts: pd.DataFrame) -> pd.Series:
    """
    One bucket label per evaluated item, indexed by item_code.

    Three buckets are the point of Part A' — declining, stable/growing, and the
    insufficient-history items `bias_drivers` drops. Two more labels exist purely
    so the assignment is total: every item in `scores` gets exactly one, and a
    bucket that turns out empty is skipped in the report rather than an item
    going missing from it without a number.
    """
    items = pd.Index(sorted(scores["item_code"].astype(str).unique()),
                     name="item_code")
    dr = item_decline_ratio(facts)
    known = items.isin(dr.index)
    j = dr.reindex(items)

    ratio = j["decline_ratio"]
    has_prior = j["has_prior_window"].fillna(False).astype(bool).to_numpy()

    bucket = pd.Series(NO_TREND_FACTS, index=items, dtype=object)
    bucket[known & ~has_prior] = NO_PRIOR_WINDOW
    bucket[known & has_prior & ratio.isna().to_numpy()] = NO_PRIOR_DEMAND
    bucket[known & has_prior & (ratio < 1.0).to_numpy()] = DECLINING
    bucket[known & has_prior & (ratio >= 1.0).to_numpy()] = STABLE_GROWING
    return bucket


def split_variant_table(scores: pd.DataFrame, buckets: pd.Series) -> pd.DataFrame:
    """
    Part A's variant table and Part A's pick, recomputed inside each bucket.

    `variant_table` and `pick_winner` are called on a FILTERED `scores` — the
    ranking logic, the median-over-mean choice and the baseline's ineligibility
    are the Part A ones by construction, not a second copy that could be edited
    apart from them.

    `n_items_in_bucket` (items assigned) and `n_items` (items with a defined
    WAPE) are both carried, and they differ by the dormant items, exactly as the
    pooled Part A table differs from the evaluated count.
    """
    frames = []
    for name in SPLIT_BUCKET_ORDER:
        members = buckets.index[buckets == name]
        if len(members) == 0:
            continue

        sub = scores[scores["item_code"].astype(str).isin(set(members))]
        t = variant_table(sub)
        winner, pooled_winner = pick_winner(t)

        base = t.loc[t["variant"] == BASELINE, "wape_median"]
        base_med = float(base.iloc[0]) if len(base) and pd.notna(base.iloc[0]) else np.nan
        base_pool = t.loc[t["variant"] == BASELINE, "wape_pooled_median"]
        base_pool_med = (float(base_pool.iloc[0])
                         if len(base_pool) and pd.notna(base_pool.iloc[0]) else np.nan)

        t.insert(0, "decline_bucket", name)
        t.insert(1, "n_items_in_bucket", int(len(members)))
        t["baseline_wape_median"] = base_med
        t["wape_median_vs_baseline"] = t["wape_median"] - base_med
        t["beats_baseline"] = (t["is_candidate"] & t["wape_median"].notna()
                               & (t["wape_median"] < base_med))
        # The same comparison under the metric that KEEPS the zero-demand folds.
        # `bucket_pooled_winner` only ever catches a DIFFERENT candidate winning
        # under wape_pooled; it says nothing about whether the candidate that wins
        # under both actually clears the baseline under both. That is a separate
        # failure and it happens in this sample, so it gets its own column.
        t["baseline_wape_pooled_median"] = base_pool_med
        t["beats_baseline_pooled"] = (t["is_candidate"] & t["wape_pooled_median"].notna()
                                      & (t["wape_pooled_median"] < base_pool_med))
        t["beats_baseline_both"] = t["beats_baseline"] & t["beats_baseline_pooled"]
        t["bucket_winner"] = winner
        t["bucket_pooled_winner"] = pooled_winner
        t["is_bucket_winner"] = t["variant"] == winner if winner else False
        frames.append(t)

    return (pd.concat(frames, ignore_index=True) if frames
            else pd.DataFrame(columns=SPLIT_COLS))


# ── Part B ────────────────────────────────────────────────────────────────────

def _bucket_label(lo: int, hi: int) -> str:
    return f"{lo}-{hi - 1}" if hi < 999 else f"{lo}+"


def bucket_curve(frame: pd.DataFrame, value_col: str, count_col: str,
                 buckets: list[tuple[int, int]], unit: str) -> pd.DataFrame:
    """
    Median/mean of `value_col` within each occurrence bucket.

    `n` is reported on every row without exception — a bucket with three members
    has a median, and that median means very little, and the only defence against
    reading it as if it meant something is showing the number next to it.
    """
    rows = []
    for lo, hi in buckets:
        sub = frame[(frame[count_col] >= lo) & (frame[count_col] < hi)]
        vals = sub[value_col].dropna()
        rows.append({
            "bucket": _bucket_label(lo, hi),
            "lo": lo, "hi": hi,
            f"n_{unit}": int(len(vals)),
            "wape_median": float(vals.median()) if len(vals) else np.nan,
            "wape_mean": float(vals.mean()) if len(vals) else np.nan,
            "wape_p25": float(vals.quantile(0.25)) if len(vals) else np.nan,
            "wape_p75": float(vals.quantile(0.75)) if len(vals) else np.nan,
        })
    return pd.DataFrame(rows)


def curve_trend(frame: pd.DataFrame, value_col: str, count_col: str) -> float:
    """
    Spearman rank correlation between occurrence count and WAPE, on the raw
    (unbucketed) points.

    The precondition for reading a threshold off a curve. "Where does the curve
    flatten" presumes the curve descends somewhere first; if WAPE is unrelated to
    occurrence count, there is no flattening point to find and any number the
    stabilisation rule returns is describing which items landed in which bucket.
    Negative = more occurrences, lower error.
    """
    sub = frame[[count_col, value_col]].dropna()
    if len(sub) < 3 or sub[count_col].nunique() < 3:
        return float("nan")
    return float(sub.corr(method="spearman").iloc[0, 1])


#: How negative `curve_trend` has to be before a stabilisation point is reported
#: as a threshold rather than as an artifact. -0.2 is a judgment call and a low
#: bar deliberately: it is set to catch "no relationship at all", not to demand a
#: strong one.
MIN_CURVE_TREND = -0.2


def find_stabilisation(curve: pd.DataFrame,
                       tol: float = STABILISATION_TOLERANCE_PP) -> tuple[int, str]:
    """
    The lowest occurrence count from which no larger bucket does better by `tol`.

    The question Part B asks is not "where is WAPE lowest" — that is always the
    largest bucket, and thresholding there would block every series that has not
    been selling for eight years. It is "where does MORE data stop buying
    accuracy", i.e. the first bucket whose median WAPE is already within `tol` of
    the best any richer bucket achieves. Returns (occurrence_count, bucket_label),
    or (-1, "") when no bucket satisfies it.

    NOTE the failure mode this cannot detect on its own, and which `curve_trend`
    exists to catch: on a FLAT curve every bucket satisfies the rule, so the first
    one is returned and looks like an answer. It is not one. Read the two together.
    """
    valid = curve.dropna(subset=["wape_median"]).reset_index(drop=True)
    for i, row in valid.iterrows():
        best_above = valid.loc[i:, "wape_median"].min()
        if row["wape_median"] - best_above <= tol:
            return int(row["lo"]), str(row["bucket"])
    return -1, ""


# ── Part C ────────────────────────────────────────────────────────────────────

def prophet_context(scores: pd.DataFrame, winner: str, out_dir: Path) -> pd.DataFrame:
    """
    The winning variant's CV WAPE beside Prophet's REPORTED WAPE, for items the
    last run fitted.

    Two numbers measured on different windows: the Croston column is
    rolling-origin CV strictly inside the training window, and the Prophet column
    is `model_metrics.csv`, which scores the held-out 2026-01 → 2026-06 window.
    They are not a controlled comparison and the summary says so every time it
    prints them. Fitting Prophet on these folds would make them comparable and is
    deliberately not done: this is context for a decision, and a number computed
    to be optimised against is a number that starts getting optimised against.
    """
    path = out_dir / "model_metrics.csv"
    if not path.exists():
        return pd.DataFrame()

    m = pd.read_csv(path)
    m["item_code"] = m["item_code"].astype(str).str.strip()
    for col in ("wape_pct", "mape_pct", "n_test_months"):
        if col in m.columns:
            m[col] = pd.to_numeric(m[col], errors="coerce")

    win = (scores[scores["variant"] == winner]
           .set_index("item_code")[["wape", "wape_pooled", "mape", "n_folds_scored"]])
    joined = m.set_index("item_code").join(win, how="inner").dropna(subset=["wape"])
    if joined.empty:
        return pd.DataFrame()

    out = joined.reset_index()[
        ["item_code", "wape_pct", "mape_pct", "n_test_months",
         "wape", "wape_pooled", "mape", "n_folds_scored"]
    ].rename(columns={"wape_pct": "prophet_wape_test_window",
                      "mape_pct": "prophet_mape_test_window",
                      "wape": "croston_wape_cv",
                      "wape_pooled": "croston_wape_pooled_cv",
                      "mape": "croston_mape_cv"})
    out["wape_delta"] = out["croston_wape_cv"] - out["prophet_wape_test_window"]
    return out.sort_values("wape_delta").reset_index(drop=True)


# ── Assembly ──────────────────────────────────────────────────────────────────

#: Window for the direction-of-travel diagnostic: mean monthly demand over the
#: last N calendar months against the N before them, zeros included. 24 months
#: because a shorter window on a series that sells four times a year is reading
#: noise, and this is a description of the population, not a per-item verdict.
TREND_WINDOW_MONTHS = 24


def series_facts(grid: pd.DataFrame, anchor: pd.Timestamp) -> dict:
    """
    Last sale, and recent demand rate against the rate before it.

    Not an input to any score. It is what makes the Part A ranking readable: the
    three estimator families here differ mainly in how fast they forget, so
    whether this population is growing, flat or obsolescing decides the ranking
    before any of them is fitted.
    """
    y = grid.set_index("ds")["y"]
    recent_start = anchor - pd.DateOffset(months=TREND_WINDOW_MONTHS - 1)
    prior_start = anchor - pd.DateOffset(months=2 * TREND_WINDOW_MONTHS - 1)
    recent = y[y.index >= recent_start]
    prior = y[(y.index < recent_start) & (y.index >= prior_start)]
    nz = y[y > 0]
    return {
        "last_sale": nz.index.max() if len(nz) else pd.NaT,
        "rate_recent_24m": float(recent.mean()) if len(recent) else np.nan,
        "rate_prior_24m": float(prior.mean()) if len(prior) else np.nan,
    }


def run_experiment(sample: pd.DataFrame, pooled_data: pd.DataFrame,
                   anchor: pd.Timestamp
                   ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Score every variant on every sampled item. Returns (folds, skipped, facts)."""
    df = pooled_data.copy()
    df["item_code"] = df["item_code"].astype(str).str.strip()
    by_key = {k: g for k, g in df.groupby("item_code")}

    all_folds: list[dict] = []
    skipped: list[dict] = []
    facts: list[dict] = []

    for row in sample.itertuples(index=False):
        key = str(row.family_key).strip()
        if key not in by_key:
            skipped.append({"item_code": row.item_code,
                            "n_nonzero_months": row.n_nonzero_months,
                            "reason": "no rows in the prepared frame"})
            continue

        grid = monthly_grid(by_key[key], anchor)
        if not grid.empty:
            facts.append({"item_code": str(row.item_code),
                          **series_facts(grid, anchor)})

        cutoffs = make_cutoffs(grid)
        if not cutoffs:
            skipped.append({
                "item_code": row.item_code,
                "n_nonzero_months": row.n_nonzero_months,
                "reason": (f"no valid CV fold: {len(grid)} calendar months on the "
                           f"grid, needs {MIN_CV_OCCURRENCES} occurrences before a "
                           f"cutoff with {CV_HORIZON_MONTHS} whole months after it"),
            })
            continue
        all_folds.extend(fold_rows(grid, cutoffs, str(row.item_code)))

    return pd.DataFrame(all_folds), pd.DataFrame(skipped), pd.DataFrame(facts)


# ── Output schemas ────────────────────────────────────────────────────────────

FOLD_COLS = ["item_code", "variant", "cutoff", "n_occurrences_at_cutoff",
             "n_horizon_months", "yhat_level", "actual_sum", "abs_err_sum",
             "wape", "mape", "fold_note"]

RESULT_COLS = ["item_code", "family_key", "category", "n_nonzero_months",
               "tenure_months", "near_threshold", "variant", "n_folds",
               "n_folds_scored", "n_folds_zero_demand", "wape", "wape_pooled",
               "mape", "mean_yhat_level"]

SPLIT_COLS = ["decline_bucket", "n_items_in_bucket", "variant", "is_candidate",
              "n_items", "wape_median", "wape_mean", "wape_pooled_median",
              "wape_pooled_mean", "mape_median", "mape_mean",
              "baseline_wape_median", "wape_median_vs_baseline",
              "beats_baseline", "baseline_wape_pooled_median",
              "beats_baseline_pooled", "beats_baseline_both",
              "is_bucket_winner", "bucket_winner",
              "bucket_pooled_winner"]

SUMMARY_COLS = ["item_code", "family_key", "category", "n_nonzero_months",
                "tenure_months", "near_threshold", "currently_fitted",
                "clears_current_placeholder", "n_folds_scored",
                "n_folds_zero_demand", "best_variant", "best_wape",
                "winner_wape", "winner_wape_pooled", "winner_mape",
                "baseline_wape", "winner_beats_baseline"]


def build_summary(scores: pd.DataFrame, sample: pd.DataFrame, winner: str,
                  fitted: frozenset[str]) -> pd.DataFrame:
    """One row per item: its own best variant, the global winner's score, the
    baseline, and whether the global winner actually beat arithmetic on it."""
    rows = []
    meta = sample.set_index("item_code")
    for item, grp in scores.groupby("item_code"):
        by_var = grp.set_index("variant")
        cands = grp[grp["variant"].isin(CANDIDATES)].dropna(subset=["wape"])
        best = (str(cands.loc[cands["wape"].idxmin(), "variant"])
                if not cands.empty else "")
        w = by_var["wape"].get(winner, np.nan)
        b = by_var["wape"].get(BASELINE, np.nan)
        m = meta.loc[item]
        rows.append({
            "item_code": item,
            "family_key": m["family_key"],
            "category": m["category"],
            "n_nonzero_months": int(m["n_nonzero_months"]),
            "tenure_months": int(m["tenure_months"]),
            "near_threshold": bool(m["near_threshold"]),
            "currently_fitted": item in fitted,
            "clears_current_placeholder": bool(
                int(m["n_nonzero_months"]) >= config.CROSTON_MIN_OCCURRENCES),
            "n_folds_scored": int(by_var["n_folds_scored"].get(winner, 0)),
            "n_folds_zero_demand": int(by_var["n_folds_zero_demand"].get(winner, 0)),
            "best_variant": best,
            "best_wape": (float(cands["wape"].min()) if not cands.empty else np.nan),
            "winner_wape": float(w) if pd.notna(w) else np.nan,
            "winner_wape_pooled": float(by_var["wape_pooled"].get(winner, np.nan)),
            "winner_mape": float(by_var["mape"].get(winner, np.nan)),
            "baseline_wape": float(b) if pd.notna(b) else np.nan,
            "winner_beats_baseline": bool(pd.notna(w) and pd.notna(b) and w < b),
        })
    return pd.DataFrame(rows).sort_values("n_nonzero_months").reset_index(drop=True)


# ── Reporting ─────────────────────────────────────────────────────────────────

def print_split(split: pd.DataFrame, buckets: pd.Series, winner: str,
                sep: str, sub: str, pooled_beats_both: bool) -> None:
    """
    PART A' — the Part A table and the Part A pick, one bucket at a time.

    Every bucket's n is stated before its table and again beside its verdict, and
    a bucket at or below THIN_BUCKET_MAX_ITEMS gets its verdict withheld rather
    than printed and hedged. Part B's buckets hold 2-4 items and the closing
    section already calls that unreadable; a Part A' bucket of the same size is
    not more readable for being sliced a different way.

    `pooled_beats_both` is the pooled Part A result under the SAME both-metrics
    bar this section applies, passed in rather than restated: whether the split
    changes the pooled answer cannot be described by a sentence hard-coded to
    whatever the pooled answer was on the day it was written.
    """
    print(f"\n{sep}\nPART A' — SPLIT BY TREND (Part A rerun inside each "
          f"decline bucket)\n{sep}")
    if pooled_beats_both:
        print(f"  Part A pooled the whole standalone Lumpy/Intermittent sample "
              f"and its pick, {winner},")
        print(f"  clears {BASELINE} there under both metrics. The bias diagnostic "
              f"above already splits")
        print(f"  that population on")
    else:
        print(f"  Part A pooled the whole standalone Lumpy/Intermittent sample and "
              f"found no candidate")
        print(f"  beating {BASELINE} under both metrics. The bias diagnostic above "
              f"already splits that")
        print(f"  population on")
    print(f"  decline_ratio = rate_recent_24m / rate_prior_24m "
          f"({TREND_WINDOW_MONTHS}-month windows, cut at 1.00) —")
    print(f"  but only to explain a bias, never to re-slice the "
          f"win/lose-vs-baseline comparison. This")
    print(f"  section reruns that comparison, on the same ratio and the same cut, "
          f"inside each bucket.")
    print(f"  The ratio is REUSED from series_facts(), not recomputed.")

    if split.empty:
        print("\n  (no bucket could be formed)")
        return

    # ── Bucket sizes, before any table ────────────────────────────────────────
    print(f"\n{sub}\n  BUCKET SIZES — items assigned / items with a defined WAPE"
          f"\n{sub}")
    sizes = split.drop_duplicates("decline_bucket").set_index("decline_bucket")
    for name in SPLIT_BUCKET_ORDER:
        if name not in sizes.index:
            continue
        assigned = int(sizes.loc[name, "n_items_in_bucket"])
        scored = int(split.loc[(split["decline_bucket"] == name)
                               & (split["variant"] == BASELINE), "n_items"].iloc[0])
        thin = "   <-- a handful of items" if scored <= THIN_BUCKET_MAX_ITEMS else ""
        print(f"    {name:<22} ({SPLIT_BUCKET_RULE[name]:<44}) : "
              f"{assigned:>3} / {scored:>3}{thin}")
    print(f"\n  The two counts differ by the dormant items — assigned to a bucket, "
          f"but with no defined")
    print(f"  WAPE in any scored horizon, exactly as they are absent from the "
          f"pooled Part A table.")
    if NO_PRIOR_WINDOW in sizes.index:
        print(f"\n  {NO_PRIOR_WINDOW} is the bucket bias_drivers() DROPS: "
              f"rate_prior_24m is undefined, so")
        print(f"  these items have no {TREND_WINDOW_MONTHS}-month window before "
              f"the last {TREND_WINDOW_MONTHS} to be compared against and")
        print(f"  no decline_ratio exists for them at all. They are neither "
              f"declining nor stable; they")
        print(f"  are unmeasured on this axis, and they are counted here rather "
              f"than disappearing into")
        print(f"  the difference between two other numbers.")

    # ── One Part A table per bucket ───────────────────────────────────────────
    verdicts = []
    for name in SPLIT_BUCKET_ORDER:
        blk = split[split["decline_bucket"] == name]
        if blk.empty:
            continue
        assigned = int(blk["n_items_in_bucket"].iloc[0])
        scored = int(blk.loc[blk["variant"] == BASELINE, "n_items"].iloc[0])
        bwin = str(blk["bucket_winner"].iloc[0])
        bpool = str(blk["bucket_pooled_winner"].iloc[0])
        base_med = float(blk["baseline_wape_median"].iloc[0])
        base_pool_med = float(blk["baseline_wape_pooled_median"].iloc[0])

        print(f"\n{sub}\n  {name.upper()}  ({SPLIT_BUCKET_RULE[name]})  —  "
              f"n = {scored} scored items of {assigned} assigned\n{sub}")
        if scored == 0:
            print(f"  No item in this bucket has a defined WAPE, so there is no "
                  f"comparison to run in it.")
            verdicts.append((name, scored, "", np.nan, "no scorable items", False))
            continue

        view = blk.copy()
        view["variant"] = np.where(view["is_candidate"], view["variant"],
                                   view["variant"] + "  (context)")
        print(view[["variant", "n_items", "wape_median", "wape_mean",
                    "wape_pooled_median", "mape_median"]]
              .round(1).to_string(index=False))

        if not bwin:
            print(f"\n  No candidate has a defined median WAPE here — no pick.")
            verdicts.append((name, scored, "", np.nan, "no candidate scorable", False))
            continue

        win_row = blk.loc[blk["variant"] == bwin]
        win_med = float(win_row["wape_median"].iloc[0])
        win_pool_med = float(win_row["wape_pooled_median"].iloc[0])
        beats = bool(win_row["beats_baseline"].iloc[0])
        beats_both = bool(win_row["beats_baseline_both"].iloc[0])
        delta = win_med - base_med
        delta_pool = win_pool_med - base_pool_med
        print(f"\n  bucket winner : {bwin}  (median WAPE {win_med:.1f} vs "
              f"{BASELINE} {base_med:.1f}, {delta:+.1f}pp)")

        if name == DECLINING:
            # The three numbers the shorter window was added for, named in prose
            # rather than left to be picked out of the table by eye. No verdict
            # attaches to them — see WHAT THIS DOES NOT SAY.
            short = blk.loc[blk["variant"] == NAIVE_SHORT]
            if len(short):
                s_med = float(short["wape_median"].iloc[0])
                s_pool = float(short["wape_pooled_median"].iloc[0])
                print(f"  short window  : {NAIVE_SHORT} against "
                      f"{BASELINE} and against the winner —")
                print(f"                  {BASELINE:<16} median {base_med:8.1f}"
                      f"   pooled {base_pool_med:8.1f}")
                print(f"                  {NAIVE_SHORT:<16} median {s_med:8.1f}"
                      f"   pooled {s_pool:8.1f}")
                print(f"                  {bwin:<16} median {win_med:8.1f}"
                      f"   pooled {win_pool_med:8.1f}")
                print(f"                  {NAIVE_SHORT} vs {BASELINE}: "
                      f"{s_med - base_med:+.1f}pp median, "
                      f"{s_pool - base_pool_med:+.1f}pp pooled")
                print(f"                  {NAIVE_SHORT} vs {bwin}: "
                      f"{s_med - win_med:+.1f}pp median, "
                      f"{s_pool - win_pool_med:+.1f}pp pooled")

        if bpool and bpool != bwin:
            print(f"  cross-check   : under wape_pooled this bucket's winner is "
                  f"{bpool}, not {bwin} —")
            print(f"                  the pick turns on how zero-demand folds are "
                  f"treated, not on the model.")
        if bwin != winner:
            print(f"  note          : this is NOT the pooled Part A pick "
                  f"({winner}).")

        if scored <= THIN_BUCKET_MAX_ITEMS:
            print(f"\n  ** n = {scored}. NO VERDICT IS REPORTED FOR THIS BUCKET. "
                  f"That is the same handful-of-")
            print(f"     items count the closing section already calls unreadable "
                  f"in Part B, and a median")
            print(f"     over {scored} item(s) describes which items landed here, "
                  f"not which estimator is")
            print(f"     better. The numbers are printed because suppressing them "
                  f"would hide the bucket;")
            print(f"     'beats baseline' is deliberately not concluded either way.")
            verdicts.append((name, scored, bwin, delta, "n too small to read", False))
            continue

        if beats_both:
            print(f"\n  ** {bwin} BEATS {BASELINE} in this bucket by "
                  f"{-delta:.1f}pp of median WAPE and by")
            print(f"     {-delta_pool:.1f}pp of wape_pooled_median — under BOTH "
                  f"metrics, and by a margin the")
            if pooled_beats_both:
                print(f"     pooled Part A comparison does not show: there the same "
                      f"bar is cleared, but on the")
                print(f"     whole population rather than on the subgroup the "
                      f"estimator is actually suited to.")
            else:
                print(f"     pooled Part A comparison does not show anywhere. The "
                      f"pooled result is therefore an")
                print(f"     average over subgroups that do not agree, not a "
                      f"uniform finding.")
            verdicts.append((name, scored, bwin, delta,
                             "candidate beats baseline (both metrics)", True))
        elif beats:
            print(f"\n  ** ONE METRIC ONLY, so this is NOT counted as a bucket win. "
                  f"{bwin} beats {BASELINE}")
            print(f"     under wape_median ({win_med:.1f} vs {base_med:.1f}, "
                  f"{delta:+.1f}pp) and LOSES to it under")
            print(f"     wape_pooled ({win_pool_med:.1f} vs {base_pool_med:.1f}, "
                  f"{delta_pool:+.1f}pp). The two differ only in")
            print(f"     whether the zero-demand folds are kept, and a "
                  f"Croston-family model always predicts")
            print(f"     a positive constant — so the metric that DROPS those folds "
                  f"is the one that flatters")
            print(f"     it, and it is the only one it wins under. The "
                  f"cross-check above cannot catch this:")
            print(f"     it fires when the pooled metric picks a DIFFERENT "
                  f"candidate, and here the same")
            print(f"     candidate wins under both and clears the baseline under "
                  f"only one. A candidate has")
            print(f"     to beat the trailing mean under both to count.")
            verdicts.append((name, scored, bwin, delta,
                             "beats baseline on wape_median ONLY", False))
        else:
            print(f"\n  No candidate beats {BASELINE} here either: the best of "
                  f"them is {delta:+.1f}pp against a")
            print(f"  {BASELINE_WINDOW_MONTHS}-month mean with no parameters. "
                  f"Same verdict as pooled Part A, now shown to")
            print(f"  hold within this subgroup rather than only on average "
                  f"across all of them.")
            verdicts.append((name, scored, bwin, delta, "baseline still wins", False))

    # ── What the split changed, if anything ───────────────────────────────────
    print(f"\n{sub}\n  DOES SPLITTING BY TREND CHANGE THE PART A ANSWER?\n{sub}")
    print(f"  The bar for a bucket counting as a flip is beats_baseline_both: "
          f"lower than {BASELINE}")
    print(f"  under wape_median AND under wape_pooled_median. Clearing only the "
          f"first is reported")
    print(f"  and not counted — the delta shown is the wape_median one either way.")
    for name, scored, bwin, delta, note, _both in verdicts:
        d = f"{delta:+6.1f}pp" if pd.notna(delta) else "     n/a"
        print(f"    {name:<22} n={scored:<3} winner {bwin or '—':<18} "
              f"{d} vs baseline   {note}")

    readable = [v for v in verdicts if v[1] > THIN_BUCKET_MAX_ITEMS and v[2]]
    flipped = [v for v in readable if v[5]]
    if not readable:
        print(f"\n  ** EVERY bucket is at or below {THIN_BUCKET_MAX_ITEMS} scored "
              f"items, so the split has no readable")
        print(f"     bucket in it. Part A' has not confirmed or overturned Part A; "
              f"it has established")
        print(f"     that this sample cannot answer the question once it is cut "
              f"this way.")
    elif flipped:
        print(f"\n  ** The pooled Part A verdict does NOT hold uniformly: "
              f"{len(flipped)} of {len(readable)} readable")
        print(f"     bucket(s) have a candidate beating {BASELINE} under BOTH "
              f"wape_median and")
        print(f"     wape_pooled. Which population a Croston-family model is being "
              f"asked to serve is")
        print(f"     therefore part of the 'whether to use one at all' question, "
              f"not separate from it.")
        print(f"     Still adopted by nothing — see the closing section.")
    else:
        one_metric = [v for v in readable
                      if v[4] == "beats baseline on wape_median ONLY"]
        print(f"\n  No. In every readable bucket the {BASELINE_WINDOW_MONTHS}-month "
              f"trailing mean still wins, so the")
        print(f"     pooled Part A result is not an artifact of mixing declining "
              f"and stable items")
        print(f"     together. The trend split does not rescue any candidate.")
        if one_metric:
            print(f"\n     {len(one_metric)} readable bucket(s) came close and are "
                  f"NOT counted: "
                  f"{', '.join(v[0] for v in one_metric)}.")
            print(f"     There the bucket winner is under {BASELINE} on wape_median "
                  f"and over it on")
            print(f"     wape_pooled, i.e. it wins only on the metric that drops the "
                  f"zero-demand folds a")
            print(f"     always-positive forecast is guaranteed to lose. That is the "
                  f"nearest this split")
            print(f"     comes to overturning Part A, and it is not near enough to "
                  f"call a flip.")


def print_summary(sample: pd.DataFrame, excluded_pooled: pd.DataFrame,
                  skipped: pd.DataFrame, folds: pd.DataFrame,
                  scores: pd.DataFrame, table: pd.DataFrame,
                  summary: pd.DataFrame, winner: str, pooled_winner: str,
                  item_curve: pd.DataFrame, fold_curve: pd.DataFrame,
                  context: pd.DataFrame, shrink: pd.DataFrame,
                  facts: pd.DataFrame, drivers: dict, item_trend: float,
                  fold_trend: float, split: pd.DataFrame,
                  split_buckets: pd.Series) -> None:
    sep = "=" * 96
    sub = "-" * 96

    print(f"\n{sep}\nEXPERIMENT DESIGN (every number here is a judgment call, not a "
          f"codebase value)\n{sep}")
    print(f"  population        : standalone (NOT pooled) items classified "
          f"{dc.LUMPY} or {dc.INTERMITTENT}")
    print(f"                      by preprocessing/demand_classification.py, "
          f"occurrence count regardless")
    print(f"  validation        : rolling-origin CV strictly inside the training "
          f"window (<= {config.TRAIN_END}).")
    print(f"                      The {config.TEST_START}-{config.TEST_END} "
          f"held-out window is never scored here.")
    print(f"  horizon / spacing : {CV_HORIZON_MONTHS} monthly points / "
          f"{CV_PERIOD_MONTHS} months, max {MAX_CUTOFFS} folds")
    print(f"                      (imported from changepoint_experiment, not "
          f"re-derived)")
    print(f"  fold minimum      : {MIN_CV_OCCURRENCES} occurrences before a cutoff "
          f"— NOT config.MIN_TRAIN_MONTHS ({config.MIN_TRAIN_MONTHS}")
    print(f"                      calendar months). See the module docstring: that "
          f"bar would delete the")
    print(f"                      low-occurrence end this experiment exists to "
          f"measure.")
    print(f"  series form       : monthly grid from first sale to {config.TRAIN_END}, "
          f"gaps filled with 0")
    print(f"                      (prepare() emits no zero rows; Croston is unusable "
          f"without the zeros)")
    print(f"  candidates        : {', '.join(CANDIDATES)}")
    print(f"  context only      : {', '.join(CONTEXT_ROWS)} — never eligible to "
          f"win anything")
    print(f"  primary metric    : WAPE. MAPE is reported SECONDARY throughout — on "
          f"this population it")
    print(f"                      divides by quantities of 1-2 units (HOW_IT_WORKS.md, "
          f"UN1300).")

    # ── Coverage ──────────────────────────────────────────────────────────────
    print(f"\n{sep}\nPOPULATION\n{sep}")
    print(f"  standalone Lumpy/Intermittent items    : {len(sample)}")
    print(f"    of which {dc.LUMPY:<26}: "
          f"{int((sample['category'] == dc.LUMPY).sum())}")
    print(f"    of which {dc.INTERMITTENT:<26}: "
          f"{int((sample['category'] == dc.INTERMITTENT).sum())}")
    print(f"  POOLED Lumpy/Intermittent EXCLUDED     : {len(excluded_pooled)} items "
          f"over {excluded_pooled['family_key'].nunique()} families")
    print(f"    ({', '.join(sorted(excluded_pooled['item_code'].astype(str)))})")
    print(f"    Excluded on purpose. Whether Croston/TSB should pool at all, and "
          f"whether the 6-month")
    print(f"    split ratio means the same thing for it, is an open architectural "
          f"question and is NOT")
    print(f"    answered here — including them would answer it by implication.")
    print(f"  below the current placeholder ({config.CROSTON_MIN_OCCURRENCES} occ)  : "
          f"{int((sample['n_nonzero_months'] < config.CROSTON_MIN_OCCURRENCES).sum())} "
          f"items, all kept in")
    if not skipped.empty:
        print(f"\n  NOT EVALUATED — {len(skipped)}:")
        for row in skipped.itertuples(index=False):
            print(f"    {row.item_code:<10} ({row.n_nonzero_months} occ)  {row.reason}")
    n_eval = scores["item_code"].nunique()
    print(f"\n  evaluated                              : {n_eval} items, "
          f"{int(folds['cutoff'].count() / (len(CANDIDATES) + len(CONTEXT_ROWS)))} "
          f"folds, "
          f"{len(folds):,} fold-variant scores")
    zd = folds[(folds["variant"] == winner) & (folds["actual_sum"] == 0)]
    print(f"  folds with ZERO demand in the horizon  : {len(zd)} "
          f"(no WAPE denominator — dropped from the per-fold mean, kept in "
          f"wape_pooled)")

    dormant = summary[summary["n_folds_scored"] == 0]
    if not dormant.empty:
        print(f"\n  DORMANT — {len(dormant)} item(s) sold NOTHING in any scored "
              f"six-month horizon, so they have")
        print(f"  no WAPE at all and are absent from every table below. They are "
              f"not thin data; they are")
        print(f"  items on the ACTIVE list with no recent demand, which is a "
              f"scoping finding, not a")
        print(f"  modelling one — no estimator has anything to be right or wrong "
              f"about here.")
        fct = facts.set_index("item_code") if not facts.empty else pd.DataFrame()
        for row in dormant.itertuples(index=False):
            last = (fct["last_sale"].get(row.item_code, pd.NaT)
                    if len(fct) else pd.NaT)
            last_s = last.date() if pd.notna(last) else "never"
            print(f"    {row.item_code:<10} {row.n_nonzero_months:>3} occurrences, "
                  f"last sale {last_s}")

    if not facts.empty:
        f2 = facts.dropna(subset=["rate_recent_24m", "rate_prior_24m"])
        f2 = f2[f2["rate_prior_24m"] > 0]
        if not f2.empty:
            ratio = f2["rate_recent_24m"] / f2["rate_prior_24m"]
            print(f"\n  DIRECTION OF TRAVEL — mean monthly demand, last "
                  f"{TREND_WINDOW_MONTHS} months vs the {TREND_WINDOW_MONTHS} before:")
            print(f"    median ratio {ratio.median():.2f}  |  declining "
                  f"{int((ratio < 1).sum())} / {len(ratio)} items  "
                  f"({len(facts) - len(f2)} items have no prior window to compare)")
            print(f"    Reported because it is the obvious explanation for a "
                  f"Croston over-forecast — the")
            print(f"    estimator cannot decay through a zero run. The bias check "
                  f"below tests that story")
            print(f"    rather than assuming it.")

    # ── PART A ────────────────────────────────────────────────────────────────
    print(f"\n{sep}\nPART A — VARIANT COMPARISON ({n_eval} items, rolling-origin CV)"
          f"\n{sep}")
    view = table.copy()
    view["variant"] = np.where(view["is_candidate"], view["variant"],
                               view["variant"] + "  (context)")
    print(view[["variant", "n_items", "wape_median", "wape_mean",
                "wape_pooled_median", "mape_median", "mape_mean"]]
          .round(1).to_string(index=False))
    print(f"\n  wape_median / wape_mean  : across items, of the per-item mean of "
          f"per-fold WAPE")
    print(f"  wape_pooled_median       : across items, of one WAPE over all "
          f"fold-months at once —")
    print(f"                             the version that KEEPS the zero-demand folds")
    print(f"  mape_*                   : SECONDARY. Computed on non-zero actual "
          f"months only.")

    n_scored = int((summary["n_folds_scored"] > 0).sum())
    print(f"\n  n_items is {n_scored}, not {n_eval}: the dormant items above "
          f"contribute no defined WAPE.")

    # ── The mechanism check on Part A ─────────────────────────────────────────
    print(f"\n{sub}\n  SHRINKAGE CHECK — is the ranking measuring accuracy or "
          f"forecast size?\n{sub}")
    sv = shrink.copy()
    sv["variant"] = np.where(sv["is_candidate"], sv["variant"],
                             sv["variant"] + "  (context)")
    print(sv[["variant", "mean_yhat_level", "mean_actual_rate", "bias_ratio",
              "pct_folds_near_zero", "pct_folds_wape_100"]].round(2).to_string(index=False))
    print(f"\n  bias_ratio 1.0 = an unbiased rate estimate; 2.0 = a forecast twice "
          f"the demand that arrived.")
    print(f"  A flat forecast of ZERO scores exactly 100% WAPE by construction, "
          f"and there is no")
    print(f"  matching ceiling on over-forecasting — so pct_folds_wape_100 is the "
          f"share of folds where")
    print(f"  a variant was rewarded for predicting nothing.")

    order_wape = (table[table["is_candidate"]].dropna(subset=["wape_median"])
                  .sort_values("wape_median")["variant"].tolist())
    order_bias = (shrink[shrink["is_candidate"]].dropna(subset=["bias_ratio"])
                  .sort_values("bias_ratio")["variant"].tolist())
    if order_wape == order_bias:
        print(f"\n  ** The WAPE ranking IS the bias ranking, position for position:")
        print(f"     {' < '.join(order_wape)}")
        print(f"     Every candidate over-forecasts (bias_ratio > 1), and they place "
              f"in exactly the order")
        print(f"     of how much. On this population the comparison is therefore "
              f"measuring which")
        print(f"     estimator shrinks hardest, not which one models intermittent "
              f"demand best. Treat the")
        print(f"     Part A 'winner' as the least-biased estimator of the six, "
              f"which is a weaker claim.")
    else:
        print(f"\n  The WAPE ranking is not simply the bias ranking "
              f"({' < '.join(order_wape)} by WAPE,")
        print(f"  {' < '.join(order_bias)} by bias), so the pick is not purely an "
              f"artifact of forecast size.")

    # ── What the worst bias tracks ────────────────────────────────────────────
    print(f"\n{sub}\n  WHAT THE OVER-FORECAST TRACKS — {drivers['variant']} "
          f"(the most biased candidate)\n{sub}")
    print(f"  median per-item bias {drivers['median_bias']:.2f}x over "
          f"{drivers['n_items']} items")
    print(f"  Spearman(bias, ADI)  {drivers['rho_adi']:+.2f}   "
          f"Spearman(bias, CV^2) {drivers['rho_cv2']:+.2f}")
    if "rho_decline" in drivers:
        print(f"  Spearman(bias, recent/prior demand rate) "
              f"{drivers['rho_decline']:+.2f}")
        print(f"    median bias on DECLINING items "
              f"{drivers['bias_declining']:.2f}x  ({drivers['n_declining']} items)")
        print(f"    median bias on stable/growing  "
              f"{drivers['bias_stable']:.2f}x")
        if (pd.notna(drivers["bias_declining"]) and pd.notna(drivers["bias_stable"])
                and drivers["bias_declining"] <= drivers["bias_stable"]):
            print(f"\n  ** NEGATIVE RESULT: obsolescence does NOT explain the "
                  f"over-forecast. The stale-level")
            print(f"     story predicts the worst bias on the declining items, and "
                  f"the declining items are")
            print(f"     where the bias is SMALLER. Recorded rather than dropped: "
                  f"it rules out the fix that")
            print(f"     story would imply (just use an estimator that decays) as "
                  f"the whole answer.")
        elif abs(drivers["rho_decline"]) > abs(drivers["rho_adi"]):
            print(f"\n  ** The stale-level story HOLDS, and dominates the "
                  f"alternative. Bias tracks decline")
            print(f"     ({drivers['rho_decline']:+.2f}) far more strongly than it "
                  f"tracks ADI ({drivers['rho_adi']:+.2f}): the faster an item's")
            print(f"     demand is falling away, the further Croston's level sits "
                  f"above what arrives.")
            print(f"     Croston updates only in months with a sale, so through a "
                  f"lengthening zero run it")
            print(f"     holds a rate the series no longer has — and this "
                  f"population is declining at a")
            print(f"     median rate of about half over two years.")
            print(f"\n     This is a statement about WHICH ESTIMATOR, not about "
                  f"occurrence counts, and it is")
            print(f"     the one thing in this experiment that separates the "
                  f"candidates for a reason:")
            cro = shrink[shrink["variant"].str.startswith("croston")]["bias_ratio"]
            tsb = shrink[shrink["variant"].str.startswith("tsb_")]["bias_ratio"]
            print(f"     Croston structurally cannot decay and TSB can, and the "
                  f"bias splits accordingly:")
            print(f"     Croston {cro.min():.2f}-{cro.max():.2f}x against TSB "
                  f"{tsb.min():.2f}-{tsb.max():.2f}x, with TSB falling "
                  f"monotonically as")
            print(f"     its decay rate rises. It is also why more occurrences do "
                  f"not help (Part B) —")
            print(f"     the error is a level that is stale, not a level that is "
                  f"under-informed.")
    print(f"\n  The weaker ADI relationship ({drivers['rho_adi']:+.2f}) is the "
          f"estimator's own arithmetic and is")
    print(f"  present underneath: Croston forecasts smoothed SIZE over smoothed "
          f"INTERVAL, and that")
    print(f"  ratio runs above the ratio of the underlying rates by more the "
          f"longer the intervals")
    print(f"  get. `croston_sba` is the published correction for it and applies a "
          f"fixed factor of")
    print(f"  1 - alpha/2 = 0.95 at statsforecast's alpha of 0.1 — a 5% shave "
          f"against a median")
    print(f"  over-forecast of {drivers['median_bias']:.1f}x, which is why it "
          f"barely separates from croston_classic")
    print(f"  above.")

    print(f"\n{sub}\n  PICK: {winner}  (lowest median WAPE among candidates)\n{sub}")
    if pooled_winner != winner:
        print(f"  ** DISAGREEMENT: under wape_pooled the winner is {pooled_winner}, "
              f"not {winner}.")
        print(f"     The two differ only in how zero-demand folds are treated, so "
              f"this is evidence")
        print(f"     that the pick turns on that choice rather than on the model. "
              f"Read both columns.")
    else:
        print(f"  Cross-check: {winner} also wins under wape_pooled, so the pick "
              f"does not depend on")
        print(f"  how zero-demand folds are handled.")

    per_item = summary["best_variant"].value_counts()
    print(f"\n  Per-item best variant (the global pick need not win item by item):")
    for name in CANDIDATES:
        if name in per_item.index:
            print(f"    {name:<20} : {per_item[name]:>3} / {len(summary)}")
    beat = int(summary["winner_beats_baseline"].sum())
    win_med = float(table.loc[table["variant"] == winner, "wape_median"].iloc[0])
    base_med = float(table.loc[table["variant"] == BASELINE, "wape_median"].iloc[0])
    print(f"\n  {winner} beats {BASELINE} on {beat} / {n_scored} scored items "
          f"(median WAPE {win_med:.1f} vs {base_med:.1f})")

    # The same both-metrics bar Part A' applies. Up to here the pooled metric was
    # only ever checked against candidate IDENTITY (pooled_winner != winner) and
    # never against whether that pooled winner clears the baseline, which is a
    # different failure — see split_variant_table.beats_baseline_both.
    pooled_ref = pooled_winner or winner
    pw = table.loc[table["variant"] == pooled_ref, "wape_pooled_median"]
    bp = table.loc[table["variant"] == BASELINE, "wape_pooled_median"]
    win_pool = float(pw.iloc[0]) if len(pw) and pd.notna(pw.iloc[0]) else np.nan
    base_pool = float(bp.iloc[0]) if len(bp) and pd.notna(bp.iloc[0]) else np.nan
    pooled_fails = bool(pd.notna(win_pool) and pd.notna(base_pool)
                        and base_pool <= win_pool)

    # The pooled equivalent of `beats_baseline_both`: the SAME variant the pick was
    # made on, under both metrics. Part A' is handed this rather than asserting the
    # pooled answer in prose.
    ow = table.loc[table["variant"] == winner, "wape_pooled_median"]
    own_pool = float(ow.iloc[0]) if len(ow) and pd.notna(ow.iloc[0]) else np.nan
    pooled_beats_both = bool(win_med < base_med and pd.notna(own_pool)
                             and pd.notna(base_pool) and own_pool < base_pool)

    if base_med <= win_med:
        print(f"\n  ** NO CANDIDATE BEAT THE TRAILING AVERAGE. The best "
              f"Croston-family variant tested is")
        print(f"     {win_med - base_med:+.1f}pp of median WAPE against a "
              f"{BASELINE_WINDOW_MONTHS}-month mean that costs nothing to")
        print(f"     compute and has no parameters. Part B's threshold is still "
              f"derived below because it")
        print(f"     was asked for and the curve is worth seeing — but a threshold "
              f"governs WHEN to use a")
        print(f"     model, and this result is about WHETHER to. That question is "
              f"logically first and is")
        print(f"     not one this script can settle: see the closing section.")
        if pooled_fails:
            print(f"\n     Under wape_pooled too: {pooled_ref} scores "
                  f"{win_pool:.1f} against {BASELINE}'s {base_pool:.1f}")
            print(f"     ({win_pool - base_pool:+.1f}pp), so the verdict does not "
                  f"depend on dropping the zero-demand")
            print(f"     folds. Stated for consistency with Part A''s "
                  f"both-metrics bar, not because it")
            print(f"     changes the headline — the wape_median comparison above "
                  f"already fails on its own.")
        elif pd.notna(win_pool) and pd.notna(base_pool):
            print(f"\n     Note: under wape_pooled {pooled_ref} IS below "
                  f"{BASELINE} ({win_pool:.1f} vs {base_pool:.1f},")
            print(f"     {win_pool - base_pool:+.1f}pp). It still fails the "
                  f"both-metrics bar, and the metric it wins")
            print(f"     under is the one that keeps the zero-demand folds, so this "
                  f"is a split result and not")
            print(f"     a win.")
    elif pooled_beats_both:
        print(f"\n  ** {winner} clears {BASELINE} under BOTH metrics here: "
              f"{win_med:.1f} vs {base_med:.1f} on")
        print(f"     wape_median ({win_med - base_med:+.1f}pp) and {own_pool:.1f} "
              f"vs {base_pool:.1f} on wape_pooled_median")
        print(f"     ({own_pool - base_pool:+.1f}pp). That is the same bar Part A' "
              f"applies per bucket, stated")
        print(f"     here so the pooled row and the bucket rows are judged the same "
              f"way. Read it next to")
        print(f"     the shrinkage check and the boundary note above before reading "
              f"it as a win: the")
        print(f"     margin is a fraction of the {BASELINE_WINDOW_MONTHS}-month "
              f"mean's own error, on {n_scored} items.")
    elif pd.notna(own_pool) and pd.notna(base_pool):
        print(f"\n  ** ONE METRIC ONLY. {winner} is below {BASELINE} on wape_median "
              f"({win_med - base_med:+.1f}pp)")
        print(f"     and NOT on wape_pooled_median ({own_pool:.1f} vs "
              f"{base_pool:.1f}, {own_pool - base_pool:+.1f}pp), so it")
        print(f"     fails the both-metrics bar Part A' applies per bucket. The "
              f"metric it wins under is")
        print(f"     the one that DROPS the zero-demand folds an always-positive "
              f"forecast is guaranteed")
        print(f"     to lose, which is the direction that flatters it. Not counted "
              f"as beating arithmetic.")

    if winner.startswith("tsb_a") and float(winner.removeprefix("tsb_a")) == max(TSB_ALPHAS):
        print(f"\n  ** BOUNDARY SOLUTION: the winning alpha ({max(TSB_ALPHAS)}) is "
              f"the largest in the tested grid")
        print(f"     {TSB_ALPHAS}, so the grid brackets no optimum — it shows a "
              f"direction, toward forgetting")
        print(f"     the past faster. `config.py` rejected exactly this shape of "
              f"result for")
        print(f"     CHANGEPOINT_PRIOR_KNOWN_CHANGEOVER (0.5, 'a boundary solution "
              f"the grid does not")
        print(f"     bracket'), and the same objection applies here.")

    # ── PART A' ───────────────────────────────────────────────────────────────
    print_split(split, split_buckets, winner, sep, sub, pooled_beats_both)

    # ── PART B ────────────────────────────────────────────────────────────────
    print(f"\n{sep}\nPART B — OCCURRENCE THRESHOLD, variant = {winner}\n{sep}")
    print(f"  Curve 1 — PER ITEM, bucketed on total non-zero months through "
          f"{config.TRAIN_END}.")
    print(f"  This is the quantity `CROSTON_MIN_OCCURRENCES` is actually compared "
          f"against in\n  model_routing.route_one(), so it is the curve the "
          f"threshold has to be read off.\n")
    print(item_curve[["bucket", "n_items", "wape_median", "wape_mean",
                      "wape_p25", "wape_p75"]].round(1).to_string(index=False))
    item_cut, item_bucket = find_stabilisation(item_curve)

    print(f"\n  Curve 2 — PER FOLD, bucketed on occurrences the model HAD at that "
          f"cutoff.")
    print(f"  Same question with ~{MAX_CUTOFFS}x the points: a long-history item "
          f"contributes an early fold\n  fitted on 5 occurrences and a late one "
          f"fitted on 60. Folds within an item are\n  correlated, so this is a "
          f"sharper picture of the same effect and not {len(folds)} independent\n"
          f"  observations.\n")
    print(fold_curve[["bucket", "n_folds", "wape_median", "wape_mean",
                      "wape_p25", "wape_p75"]].round(1).to_string(index=False))
    fold_cut, fold_bucket = find_stabilisation(fold_curve)

    print(f"\n{sub}\n  DOES EITHER CURVE DESCEND AT ALL? (Spearman, occurrences vs "
          f"WAPE, unbucketed)\n{sub}")
    print(f"  per-item : {item_trend:+.3f} over {n_scored} items")
    print(f"  per-fold : {fold_trend:+.3f} over "
          f"{int(folds[(folds['variant'] == winner) & folds['wape'].notna()].shape[0])} "
          f"scored folds")
    print(f"  Negative = more occurrences, lower error. This is the precondition "
          f"for the rule below:")
    print(f"  'where does the curve flatten' presumes it descends somewhere first. "
          f"A correlation near")
    print(f"  zero means the buckets differ by which items fell into them, and any "
          f"stabilisation point")
    print(f"  read off them is a property of the sample, not of the estimator.")

    print(f"\n{sub}\n  STABILISATION RULE: the lowest bucket from which no larger "
          f"bucket improves median\n  WAPE by more than "
          f"{STABILISATION_TOLERANCE_PP:.0f}pp.\n{sub}")
    for label, cut, bucket, trend in (
        ("per-item", item_cut, item_bucket, item_trend),
        ("per-fold", fold_cut, fold_bucket, fold_trend),
    ):
        where = (f"bucket {bucket} -> {cut} occurrences" if cut > 0
                 else "no bucket qualifies")
        verdict = ("READABLE" if pd.notna(trend) and trend <= MIN_CURVE_TREND
                   else f"NOT READABLE (trend {trend:+.3f} is not below "
                        f"{MIN_CURVE_TREND})")
        print(f"  {label} curve : {where:<34} {verdict}")

    if not (pd.notna(item_trend) and item_trend <= MIN_CURVE_TREND) and \
       not (pd.notna(fold_trend) and fold_trend <= MIN_CURVE_TREND):
        print(f"\n  ** NO THRESHOLD IS PROPOSED. Neither curve descends, so neither "
              f"has a point where it")
        print(f"     flattens. The numbers above are what the rule mechanically "
              f"returns on a flat curve —")
        print(f"     the FIRST bucket, always — and reporting either as a validated "
              f"replacement for")
        print(f"     CROSTON_MIN_OCCURRENCES would be dressing up sampling noise as "
              f"a measurement.")
        print(f"     What the flatness itself says: on this population, occurrence "
              f"count is not what")
        print(f"     limits accuracy. Adding occurrences to a series does not make "
              f"the forecast better,")
        print(f"     so a gate on occurrence count — at 10, or at any other value — "
              f"is not selecting for")
        print(f"     the thing it was meant to select for.")

    # A21088, by name.
    focus = summary[summary["item_code"] == FOCUS_ITEM]
    print(f"\n{sub}\n  {FOCUS_ITEM} — named because it sits one occurrence below the "
          f"current placeholder\n{sub}")
    if focus.empty:
        print(f"  {FOCUS_ITEM} is not in the evaluated set.")
    else:
        f = focus.iloc[0]
        print(f"  occurrences {int(f['n_nonzero_months'])}, tenure "
              f"{int(f['tenure_months'])} months, {f['category']}, "
              f"{int(f['n_folds_scored'])} scored folds")
        print(f"  {winner} WAPE {f['winner_wape']:.1f} (pooled "
              f"{f['winner_wape_pooled']:.1f}) vs {BASELINE} "
              f"{f['baseline_wape']:.1f}")
        print(f"  under the current placeholder ({config.CROSTON_MIN_OCCURRENCES} "
              f"occurrences) : BLOCKED")
        for label, cut, trend in (("per-item curve", item_cut, item_trend),
                                  ("per-fold curve", fold_cut, fold_trend)):
            readable = pd.notna(trend) and trend <= MIN_CURVE_TREND
            if cut <= 0:
                verdict = "no threshold derived"
            else:
                verdict = ("CLEARS it" if int(f["n_nonzero_months"]) >= cut
                           else "still blocked by it")
            note = "" if readable else "   [curve not readable — see above]"
            print(f"  against the {label}'s stabilisation point ({cut}) : "
                  f"{verdict}{note}")
        print(f"\n  The number that actually matters for {FOCUS_ITEM} is not on "
              f"either side of a threshold:")
        print(f"  its WAPE is {f['winner_wape']:.0f}, worse than the "
              f"{BASELINE_WINDOW_MONTHS}-month mean's {f['baseline_wape']:.0f} on "
              f"the same folds. Moving")
        print(f"  the gate to admit it would admit it to a model that is not "
              f"beating arithmetic on it.")

    # ── PART C ────────────────────────────────────────────────────────────────
    print(f"\n{sep}\nPART C — CONTEXT ONLY: {winner} vs Prophet, for items the last "
          f"run FITTED\n{sep}")
    print(f"  NOT A CONTROLLED COMPARISON, and nothing in Part A or Part B was "
          f"chosen with reference")
    print(f"  to it. The Croston column is rolling-origin CV INSIDE the training "
          f"window; the Prophet")
    print(f"  column is model_metrics.csv, which scores the held-out "
          f"{config.TEST_START}-{config.TEST_END} window.")
    print(f"  Different months, different difficulty. Read it as a direction, not "
          f"a margin.\n")
    if context.empty:
        print("  (no overlap with model_metrics.csv)")
    else:
        print(context.round(1).to_string(index=False))
        better = int((context["wape_delta"] < 0).sum())
        print(f"\n  {winner} scores lower than Prophet's reported WAPE on "
              f"{better} / {len(context)} of these items.")
        print(f"  median Croston CV WAPE {context['croston_wape_cv'].median():.1f} "
              f"| median Prophet test-window WAPE "
              f"{context['prophet_wape_test_window'].median():.1f}")
        thin = int((context["n_test_months"] < CV_HORIZON_MONTHS).sum())
        if thin:
            print(f"  {thin} of these items have fewer than {CV_HORIZON_MONTHS} "
                  f"scored test months in model_metrics.csv, so their Prophet "
                  f"number rests on\n  as little as one month.")

    # ── What this does not say ────────────────────────────────────────────────
    print(f"\n{sep}\nWHAT THIS DOES NOT SAY\n{sep}")
    print(f"  Nothing is adopted. config.CROSTON_MIN_OCCURRENCES is still "
          f"{config.CROSTON_MIN_OCCURRENCES} and still")
    print(f"  provisional; model_routing.py, main.py and the fitting path are "
          f"untouched.")
    print(f"  The sample is {len(sample)} items, {n_scored} of them scorable — see "
          f"the population section.")
    print(f"  Sample size, not method, is the binding constraint on how firmly any "
          f"of this reads, and")
    print(f"  the low-occurrence buckets Part B turns on hold 2-4 items each.")
    print(f"  Pooled families are unmeasured here and stay an open question.")
    print(f"  A trailing average is not a proposal either — it is the floor a "
          f"model has to clear, and")
    print(f"  it was put in this experiment to be a reference point, not a "
          f"recommendation. What")
    print(f"  beating it would take (a different estimator, a different segment of "
          f"the population, or")
    print(f"  accepting that some of these items should get no forecast at all) is "
          f"the open question")
    print(f"  this run hands back.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--db", action="store_true",
                    help="Reload raw inputs from Azure SQL instead of the cached CSVs")
    ap.add_argument("--input-dir", default=None,
                    help="Directory holding the cached input CSVs and receiving "
                         "this experiment's output (default: config.OUTPUT_DIR)")
    args = ap.parse_args()

    if args.input_dir:
        config.OUTPUT_DIR = Path(args.input_dir).resolve()
    out_dir = config.OUTPUT_DIR
    print(f"Input/output directory: {out_dir}")
    if not out_dir.exists():
        print(f"  ERROR: {out_dir} does not exist. Pass --input-dir, or --db to "
              f"reload from the database.")
        return 1

    pooled, families, cm_excluded, fitted_items = build_scope_input(cached=not args.db)
    classes = dc.classify_scope(
        pooled.data, pooled.active_products, families, pooled.eligible,
        channel_mismatch_excluded=cm_excluded,
    )
    sample, excluded_pooled = load_sample(classes)
    print(f"Sample: {len(sample)} standalone Lumpy/Intermittent items "
          f"({len(excluded_pooled)} pooled excluded)")

    anchor = dc.train_end_ts()
    folds, skipped, facts = run_experiment(sample, pooled.data, anchor)
    if folds.empty:
        print("Nothing could be evaluated.")
        return 1

    scores = item_scores(folds)
    table = variant_table(scores)
    shrink = shrinkage_table(folds)
    winner, pooled_winner = pick_winner(table)
    # The most biased candidate is the one whose bias is worth explaining.
    most_biased = str(shrink[shrink["is_candidate"]]
                      .sort_values("bias_ratio", ascending=False)["variant"].iloc[0])
    drivers = bias_drivers(folds, sample, facts, most_biased)

    # Part A': the same comparison, re-run inside each decline bucket. Reuses the
    # decline_ratio bias_drivers derives, and the Part A table/pick unchanged.
    split_buckets = assign_decline_bucket(scores, facts)
    split = split_variant_table(scores, split_buckets)

    summary = build_summary(scores, sample, winner, frozenset(fitted_items))

    win_scores = scores[scores["variant"] == winner].merge(
        sample[["item_code", "n_nonzero_months"]], on="item_code", how="left")
    win_folds = folds[folds["variant"] == winner]
    item_curve = bucket_curve(win_scores, "wape", "n_nonzero_months",
                              ITEM_BUCKETS, "items")
    fold_curve = bucket_curve(win_folds, "wape", "n_occurrences_at_cutoff",
                              FOLD_BUCKETS, "folds")
    item_trend = curve_trend(win_scores, "wape", "n_nonzero_months")
    fold_trend = curve_trend(win_folds, "wape", "n_occurrences_at_cutoff")

    context = prophet_context(scores, winner, out_dir)

    results = (scores.merge(sample, on="item_code", how="left")
               .reindex(columns=RESULT_COLS)
               .sort_values(["item_code", "variant"]))
    # The Part A table with its own mechanism check attached, so the ranking and
    # the bias that explains it are never read apart from each other.
    variants = table.merge(shrink.drop(columns=["is_candidate"]), on="variant")

    for path, frame in [
        (out_dir / "croston_experiment_variants.csv", variants.round(3)),
        (out_dir / "croston_experiment_folds.csv",
         folds.reindex(columns=FOLD_COLS).round(4)),
        (out_dir / "croston_experiment_results.csv", results.round(3)),
        (out_dir / "croston_experiment_summary.csv",
         summary.reindex(columns=SUMMARY_COLS).round(3)),
        (out_dir / "croston_experiment_split.csv",
         split.reindex(columns=SPLIT_COLS).round(3)),
    ]:
        frame.to_csv(path, index=False)
        print(f"Wrote {path}  ({len(frame):,} rows)")

    print_summary(sample, excluded_pooled, skipped, folds, scores, table,
                  summary, winner, pooled_winner, item_curve, fold_curve,
                  context, shrink, facts, drivers, item_trend, fold_trend,
                  split, split_buckets)
    return 0


if __name__ == "__main__":
    sys.exit(main())
