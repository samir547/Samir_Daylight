"""
Seasonality overfitting experiment — does constraining the seasonal fit help?

WHAT THIS IS
────────────
A third experiment, separate from the family-pooling work and from
`analysis/changepoint_experiment.py`. It was found by inspecting `DN1380`'s
fitted seasonal component at DAILY resolution instead of the monthly resolution
the pipeline actually forecasts at.

`config.PROPHET_PARAMS['yearly_seasonality'] = True` is Prophet's default of 10
Fourier term pairs — 20 free parameters — fitted against roughly 6-8 repeats of
a yearly cycle. Sampled monthly (12 points a year, the only points the forecast
ever reads) the fitted curve looks like a sensible seasonal wave. Sampled daily
it is not: `DN1380` swings from -969% to +1,588% around trend, and a hand check
of six other products spanning different volatilities and history lengths
(`UN91171`, `A25090`, `A25100`, `DN1370`, `D25090`) found the same shape in
every one, worse in some (`DN1370`: -1,722% to +3,674%). The mechanism — 20
parameters against ~8 examples, in `multiplicative` mode which turns an overfit
percentage into a multiplied blowout — applies to essentially every series in
this pipeline, so the sample here is deliberately NOT the badly-forecast tail.

THIS IS THE OPPOSITE DIRECTION FROM THE TREND-RIGIDITY EXPERIMENT
─────────────────────────────────────────────────────────────────
There the trend needed to be *less* rigid (`changepoint_prior_scale` raised).
Here seasonality needs to be *more* constrained: fewer Fourier terms, a tighter
prior, or additive rather than multiplicative composition. Same root cause in
spirit — Prophet defaults that were never chosen for Daylight's data — but the
remedy moves the other way.

This module MEASURES four candidates. It does not adopt any of them. Nothing
here changes `config.py`, `preprocessing/`, `models/prophet_model.py`, any
existing `output/*.csv`, or the database. It writes two new CSVs and prints a
summary; whether any of it should be wired into the pipeline is a human decision
this script deliberately does not make.

    lower_order      yearly_seasonality = 3, and separately 5 (Prophet accepts an
                     integer here to set the Fourier order directly), against the
                     default's 10. Fewer basis functions, so fewer wiggles are
                     representable at all.
    tighter_prior    seasonality_prior_scale = 3.0, and separately 1.0, down from
                     the current 10.0. Same basis, shrunk harder toward zero.
    combined         the winning `lower_order` sub-variant and the winning
                     `tighter_prior` sub-variant for THAT product, applied
                     together. Chosen per product, recorded per product.
    additive_mode    seasonality_mode = 'additive', Fourier order and prior scale
                     left at the current defaults. Isolates whether multiplicative
                     composition itself is the bigger lever, independent of the
                     parameter values.

Every other value comes from `config.PROPHET_PARAMS` verbatim, imported and
never retyped, so a candidate cannot silently drift from the model it is
compared against.

WHY THE EXISTING TEST WINDOW IS NEVER READ
──────────────────────────────────────────
`model_metrics.csv` / `benchmark_comparison.csv` score on 2025-11 → 2026-04.
Scoring a candidate fix there would be tuning against the held-out set — the
same class of error the split-ratio look-ahead fix removed. So validation is
`prophet.diagnostics.cross_validation` at rolling cutoffs strictly INSIDE the
training window, using `changepoint_experiment`'s cutoff geometry and fold
scorer imported directly rather than reimplemented (see that module's docstring
for why the horizon is 190 days and why the newest cutoff sits seven months
back). `model_metrics.csv` is read for ONE thing only: the product list and its
`n_train_months`. No metric from it is used to score anything.

THE DIAGNOSTIC METRIC, AND WHY IT IS REPORTED NEXT TO ACCURACY
──────────────────────────────────────────────────────────────
Accuracy alone cannot tell you whether a candidate worked for the reason claimed.
So every candidate also gets the daily-resolution swing — the same check done by
hand on the seven products above — from a single fit on the full training window.
It needs no cross-validation: it is a property of one fitted seasonal curve, not
a forecast error.

Both modes are measured as the same quantity, the seasonal deviation as a
percentage of trend, i.e. `(yhat - trend) / trend * 100` evaluated at daily
resolution across the last 365 days of the training window:

  * multiplicative — Prophet's `multiplicative_terms` IS that fraction already,
    so it is read directly (x100). Exact, no division involved.
  * additive       — `additive_terms / trend * 100`. This one does divide by
    trend, so a series whose fitted trend touches or crosses zero inside the
    diagnostic year has no well-defined percentage; those fall back to the mean
    absolute trend as the denominator and are flagged in `swing_note` rather
    than reported as a clean number.

Only yearly seasonality is enabled (weekly/daily are False in PROPHET_PARAMS,
no holidays, no regressors), so these terms are the yearly component and nothing
else.

The point of carrying both numbers is the cross-check: a candidate that improves
accuracy WITHOUT reducing the swing, or reduces the swing WITHOUT improving
accuracy, is evidence against the stated mechanism and is surfaced explicitly in
the summary rather than averaged away.

Run:  python -m analysis.seasonality_experiment --sample 5   (timing probe)
      python -m analysis.seasonality_experiment               (full sample)
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

import config  # noqa: E402
from analysis.trend_audit import build_fit_input  # noqa: E402  — one definition of the rebuild
from analysis.changepoint_experiment import (  # noqa: E402  — reused, not re-derived
    CV_HORIZON,
    CV_HORIZON_MONTHS,
    CV_PERIOD_MONTHS,
    MAX_CUTOFFS,
    MIN_CV_TRAIN_MONTHS,
    _fold_scores,
    make_cutoffs,
)
from prophet.diagnostics import cross_validation  # noqa: E402
from models.prophet_model import TRAIN_END_TS, TRAIN_START_TS  # noqa: E402

logging.getLogger("cmdstanpy").setLevel(logging.ERROR)
logging.getLogger("prophet").setLevel(logging.ERROR)


# ── Experiment design constants (judgment calls, all stated in the summary) ────

#: Minimum training history to enter the sample. 36 months ~ 3 yearly cycles.
#: Below that the yearly component is barely estimable and a comparison between
#: candidates would be measuring noise, not seasonality. This is ABOVE the
#: pipeline's own bar (config.MIN_TRAIN_MONTHS = 24) and is deliberately so.
MIN_HISTORY_MONTHS = 36

#: The default the candidates are measured against, read from config so the
#: control cannot drift from production.
DEFAULT_ORDER = 10          # what yearly_seasonality=True resolves to in Prophet
DEFAULT_PRIOR = config.PROPHET_PARAMS["seasonality_prior_scale"]
DEFAULT_MODE = config.PROPHET_PARAMS["seasonality_mode"]

LOWER_ORDERS = [3, 5]
TIGHTER_PRIORS = [3.0, 1.0]

CAND_DEFAULT = "default"
CAND_COMBINED = "combined"
CAND_ADDITIVE = "additive_mode"


def _order_name(order: int) -> str:
    return f"lower_order_{order}"


def _prior_name(prior: float) -> str:
    return f"tighter_prior_{prior}"


#: Diagnostic window: the last 365 days of the training window, sampled daily.
#: One representative year — Prophet's yearly component is periodic, so which
#: year is chosen changes the multiplicative answer not at all and the additive
#: answer only through the trend level in the denominator.
DIAGNOSTIC_DAYS = 365


# ── Sample selection ──────────────────────────────────────────────────────────

def load_sample() -> pd.DataFrame:
    """
    Every series in `model_metrics.csv` with >= MIN_HISTORY_MONTHS of history.

    Deliberately NOT filtered to the badly-forecast tail. The overfit mechanism
    is structural, so a candidate that helps the bad series while quietly hurting
    the good ones is a net loss, and that can only be seen if the good ones are
    in the sample. `mape_pct` is carried through for cohort reporting ONLY —
    it comes from the held-out test window and is never used to score a
    candidate.
    """
    metrics = pd.read_csv(config.OUTPUT_DIR / "model_metrics.csv")
    metrics["item_code"] = metrics["item_code"].astype(str).str.strip()
    metrics["family_key"] = metrics["family_key"].astype(str).str.strip()
    metrics["n_train_months"] = pd.to_numeric(metrics["n_train_months"], errors="coerce")
    metrics["mape_pct"] = pd.to_numeric(metrics["mape_pct"], errors="coerce")

    sample = metrics[metrics["n_train_months"] >= MIN_HISTORY_MONTHS].copy()
    cols = [c for c in ["item_code", "family_key", "n_train_months", "mape_pct", "family"]
            if c in sample.columns]
    return sample[cols].sort_values("item_code").reset_index(drop=True)


# ── Candidate parameter sets ──────────────────────────────────────────────────

def candidate_params(order: int, prior: float, mode: str) -> dict:
    """
    `config.PROPHET_PARAMS` with the three seasonality levers overridden.

    Everything else — changepoint_prior_scale, interval_width, weekly/daily
    seasonality — is the production value, because this experiment is about
    seasonality and nothing else.
    """
    params = dict(config.PROPHET_PARAMS)
    params["yearly_seasonality"] = order
    params["seasonality_prior_scale"] = prior
    params["seasonality_mode"] = mode
    return params


# ── The diagnostic metric ─────────────────────────────────────────────────────

def seasonal_swing(model: Prophet, mode: str) -> tuple[float, float, str]:
    """
    Min and max seasonal deviation as a percentage of trend, at daily resolution.

    Returns (min_pct, max_pct, note). See the module docstring for why the two
    modes are read from different columns and why they are nevertheless the same
    quantity.
    """
    start = TRAIN_END_TS - pd.Timedelta(days=DIAGNOSTIC_DAYS - 1)
    grid = pd.DataFrame({"ds": pd.date_range(start, TRAIN_END_TS, freq="D")})

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        pred = model.predict(grid)

    note = ""
    if mode == "multiplicative":
        # multiplicative_terms is already the fraction of trend. No division.
        pct = pred["multiplicative_terms"].to_numpy(dtype=float) * 100.0
    else:
        trend = pred["trend"].to_numpy(dtype=float)
        add = pred["additive_terms"].to_numpy(dtype=float)
        if (trend <= 0).any():
            # A trend that touches or crosses zero makes a per-point percentage
            # meaningless (and sign-flipping). Fall back to a single denominator
            # for the whole year and say so rather than emit a clean-looking
            # number that is an artifact of near-zero division.
            den = float(np.abs(trend).mean())
            note = "trend_nonpositive_in_year:mean_abs_trend_denominator"
            if den == 0:
                return float("nan"), float("nan"), "trend_zero:swing_undefined"
            pct = add / den * 100.0
        else:
            pct = add / trend * 100.0

    return float(np.min(pct)), float(np.max(pct)), note


# ── One candidate, one series ─────────────────────────────────────────────────

def run_candidate(train: pd.DataFrame, cutoffs: list[pd.Timestamp],
                  order: int, prior: float, mode: str) -> dict:
    """
    Fit one candidate on the full training window, then cross-validate it.

    The single full-history fit serves both purposes: `cross_validation` requires
    a model already fitted on the full history (it reads `model.history` and
    refits a copy per cutoff), and the diagnostic swing is a property of exactly
    that fit. So the diagnostic costs no extra Prophet fit.

    Because `train` stops at `config.TRAIN_END`, no fold's history can contain a
    test-window month.
    """
    params = candidate_params(order, prior, mode)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = Prophet(**params)
        model.fit(train)

        swing_min, swing_max, swing_note = seasonal_swing(model, mode)

        cv = cross_validation(
            model, horizon=CV_HORIZON, cutoffs=cutoffs, disable_tqdm=True,
        )

    mape, wape, n_points = _fold_scores(cv)

    swing_range = (
        swing_max - swing_min
        if pd.notna(swing_min) and pd.notna(swing_max) else float("nan")
    )

    return {
        "cv_mape": mape,
        "cv_wape": wape,
        "n_cutoffs": len(cutoffs),
        "seasonal_swing_min_pct": swing_min,
        "seasonal_swing_max_pct": swing_max,
        "seasonal_swing_range_pct": swing_range,
        "n_scored_points": n_points,
        "fourier_order": order,
        "seasonality_prior_scale": prior,
        "seasonality_mode": mode,
        "swing_note": swing_note,
    }


# ── One series, every candidate ───────────────────────────────────────────────

def run_series(fit_key: str, series: pd.DataFrame) -> tuple[list[dict], dict]:
    """
    Run the full candidate set for one fitted series.

    Returns (candidate_rows, status). Pooled families are run ONCE here and
    attributed to each of their item codes by the caller — the pipeline fits one
    model per family, so running one per member would be scoring models that do
    not exist.

    `combined` is resolved AFTER the sub-variants because it is defined as the
    best sub-variant of each kind for this product, which is not knowable in
    advance. Ties inside each kind go to the smaller Fourier order / tighter
    prior, i.e. toward the more constrained model, since the whole hypothesis is
    that constraint is what is missing.
    """
    train = (
        series[(series["ds"] >= TRAIN_START_TS) & (series["ds"] <= TRAIN_END_TS)]
        [["ds", "y"]].dropna().sort_values("ds").reset_index(drop=True)
    )

    status = {
        "fit_key": fit_key,
        "n_train_months": len(train),
        "n_cutoffs": 0,
        "skip_reason": "",
        "combined_order": np.nan,
        "combined_prior": np.nan,
    }

    cutoffs = make_cutoffs(train)
    if not cutoffs:
        status["skip_reason"] = (
            f"no valid CV fold: {len(train)} train months, needs "
            f"{MIN_CV_TRAIN_MONTHS} before a cutoff {CV_HORIZON_MONTHS + 1} "
            f"months from the series end"
        )
        return [], status
    status["n_cutoffs"] = len(cutoffs)

    rows: list[dict] = []

    res = run_candidate(train, cutoffs, DEFAULT_ORDER, DEFAULT_PRIOR, DEFAULT_MODE)
    res["candidate"] = CAND_DEFAULT
    rows.append(res)

    for order in LOWER_ORDERS:
        res = run_candidate(train, cutoffs, order, DEFAULT_PRIOR, DEFAULT_MODE)
        res["candidate"] = _order_name(order)
        rows.append(res)

    for prior in TIGHTER_PRIORS:
        res = run_candidate(train, cutoffs, DEFAULT_ORDER, prior, DEFAULT_MODE)
        res["candidate"] = _prior_name(prior)
        rows.append(res)

    # `combined` = this product's best lower_order x its best tighter_prior.
    best_order = _pick_best(rows, [_order_name(o) for o in LOWER_ORDERS], "fourier_order")
    best_prior = _pick_best(rows, [_prior_name(p) for p in TIGHTER_PRIORS],
                            "seasonality_prior_scale")
    if best_order is not None and best_prior is not None:
        status["combined_order"] = best_order
        status["combined_prior"] = best_prior
        res = run_candidate(train, cutoffs, int(best_order), float(best_prior), DEFAULT_MODE)
        res["candidate"] = CAND_COMBINED
        rows.append(res)

    res = run_candidate(train, cutoffs, DEFAULT_ORDER, DEFAULT_PRIOR, "additive")
    res["candidate"] = CAND_ADDITIVE
    rows.append(res)

    return rows, status


def _pick_best(rows: list[dict], names: list[str], param_col: str):
    """
    The parameter value of the lowest-CV-MAPE row among `names`.

    Returns None when every candidate in the group failed to score, in which case
    `combined` cannot be defined for this series and is not run.
    """
    pool = [r for r in rows if r["candidate"] in names and pd.notna(r["cv_mape"])]
    if not pool:
        return None
    # Sort by MAPE then by the parameter value ascending, so a tie resolves
    # toward the more constrained setting.
    pool.sort(key=lambda r: (r["cv_mape"], r[param_col]))
    return pool[0][param_col]


# ── Per-series verdict ────────────────────────────────────────────────────────

def summarise_series(detail: pd.DataFrame) -> dict:
    """
    Collapse one series' candidate rows into the verdict columns.

    Ties go to the incumbent: an alternative has to actually beat the default to
    be called a winner, because "no measurable difference" is an argument for
    changing nothing.

    `winner_has_smallest_swing` is the mechanism check. The claim under test is
    that constraining the seasonal curve is WHY accuracy improves. If the
    accuracy winner is also the candidate with the tightest daily swing, that is
    consistent with the claim. If it is not, the improvement is real but is
    happening for some other reason, and the summary says so rather than letting
    the accuracy number carry an explanation it has not earned.
    """
    by_cand = detail.set_index("candidate")
    mape = by_cand["cv_mape"].to_dict()
    wape = by_cand["cv_wape"].to_dict()
    swing = by_cand["seasonal_swing_range_pct"].to_dict()

    default = mape.get(CAND_DEFAULT, float("nan"))

    alts = detail[detail["candidate"] != CAND_DEFAULT].dropna(subset=["cv_mape"])

    winner, winner_mape = CAND_DEFAULT, default
    if not alts.empty:
        best = alts.loc[alts["cv_mape"].idxmin()]
        if pd.isna(default) or float(best["cv_mape"]) < default:
            winner, winner_mape = str(best["candidate"]), float(best["cv_mape"])

    improvement = (
        (default - winner_mape) / default * 100
        if pd.notna(default) and default != 0 and pd.notna(winner_mape) else float("nan")
    )

    # Smallest swing across EVERY candidate including the default — the question
    # is which fitted curve is least contorted, not which alternative is.
    swings = detail.dropna(subset=["seasonal_swing_range_pct"])
    if swings.empty:
        smallest_swing_cand = ""
    else:
        smallest_swing_cand = str(swings.loc[swings["seasonal_swing_range_pct"].idxmin(),
                                             "candidate"])

    # Regression check: every alternative actually run was worse than default.
    all_worse = bool(len(alts)) and pd.notna(default) and bool((alts["cv_mape"] > default).all())

    return {
        "cv_mape_default": round(default, 2) if pd.notna(default) else np.nan,
        "cv_wape_default": round(wape.get(CAND_DEFAULT, float("nan")), 2),
        "winner": winner,
        "cv_mape_winner": round(winner_mape, 2) if pd.notna(winner_mape) else np.nan,
        "cv_wape_winner": (round(wape[winner], 2) if winner in wape
                           and pd.notna(wape[winner]) else np.nan),
        "improvement_pct": round(improvement, 2) if pd.notna(improvement) else np.nan,
        "swing_range_default": (round(swing.get(CAND_DEFAULT, float("nan")), 1)
                                if pd.notna(swing.get(CAND_DEFAULT, float("nan"))) else np.nan),
        "swing_range_winner": (round(swing[winner], 1) if winner in swing
                               and pd.notna(swing[winner]) else np.nan),
        "smallest_swing_candidate": smallest_swing_cand,
        "winner_has_smallest_swing": bool(winner == smallest_swing_cand),
        "all_candidates_worse": all_worse,
        "n_candidates_tested": int(len(alts)),
        "n_candidates_worse": int((alts["cv_mape"] > default).sum()) if pd.notna(default) else 0,
    }


# ── Assembly ──────────────────────────────────────────────────────────────────

def run_experiment(sample: pd.DataFrame, scoped: pd.DataFrame
                   ) -> tuple[pd.DataFrame, pd.DataFrame, float]:
    """Fit every distinct series once, attribute the result to every item code."""
    by_key = {k: g for k, g in scoped.groupby("item_code")}

    detail_frames: list[pd.DataFrame] = []
    summary_rows: list[dict] = []
    cache: dict[str, tuple[pd.DataFrame, dict]] = {}
    started = time.perf_counter()

    n_series = sample["family_key"].nunique()
    for i, row in enumerate(sample.itertuples(index=False), start=1):
        key = row.family_key

        if key not in by_key:
            summary_rows.append(_blank_summary(
                row, note="absent from the rebuilt scope frame", n_cutoffs=0))
            continue

        if key not in cache:
            elapsed = time.perf_counter() - started
            print(f"  [{len(cache) + 1:>3}/{n_series}] {key} … {elapsed:6.1f}s elapsed",
                  flush=True)
            rows, status = run_series(key, by_key[key])
            cache[key] = (pd.DataFrame(rows), status)

        detail, status = cache[key]

        if detail.empty:
            summary_rows.append(_blank_summary(
                row, note=status["skip_reason"], n_cutoffs=status["n_cutoffs"]))
            continue

        summary_rows.append({
            "item_code": row.item_code,
            "family_key": key,
            "n_train_months": status["n_train_months"],
            "n_cutoffs": status["n_cutoffs"],
            **summarise_series(detail),
            "combined_fourier_order": status["combined_order"],
            "combined_prior_scale": status["combined_prior"],
            "note": "",
        })

        tagged = detail.copy()
        tagged.insert(0, "n_train_months", status["n_train_months"])
        tagged.insert(0, "family_key", key)
        tagged.insert(0, "item_code", row.item_code)
        detail_frames.append(tagged)

    results = (pd.concat(detail_frames, ignore_index=True)
               if detail_frames else pd.DataFrame())
    summary = pd.DataFrame(summary_rows)
    return results, summary, time.perf_counter() - started


def _blank_summary(row, note: str, n_cutoffs: int) -> dict:
    """A sample member that could not be evaluated, with the reason kept."""
    return {
        "item_code": row.item_code,
        "family_key": row.family_key,
        "n_train_months": int(row.n_train_months),
        "n_cutoffs": n_cutoffs,
        "cv_mape_default": np.nan, "cv_wape_default": np.nan,
        "winner": "not_evaluated", "cv_mape_winner": np.nan, "cv_wape_winner": np.nan,
        "improvement_pct": np.nan,
        "swing_range_default": np.nan, "swing_range_winner": np.nan,
        "smallest_swing_candidate": "", "winner_has_smallest_swing": False,
        "all_candidates_worse": False,
        "n_candidates_tested": 0, "n_candidates_worse": 0,
        "combined_fourier_order": np.nan, "combined_prior_scale": np.nan,
        "note": note,
    }


# ── Output schemas ────────────────────────────────────────────────────────────

RESULT_COLS = [
    "item_code", "family_key", "n_train_months", "candidate",
    "cv_mape", "cv_wape", "n_cutoffs",
    "seasonal_swing_min_pct", "seasonal_swing_max_pct", "seasonal_swing_range_pct",
    # Provenance beyond the required schema: exactly which parameter set produced
    # the row, so `combined` is readable without re-deriving it per product.
    "fourier_order", "seasonality_prior_scale", "seasonality_mode",
    "n_scored_points", "swing_note",
]

SUMMARY_COLS = [
    "item_code", "family_key", "n_train_months", "n_cutoffs",
    "cv_mape_default", "cv_wape_default",
    "winner", "cv_mape_winner", "cv_wape_winner", "improvement_pct",
    "swing_range_default", "swing_range_winner",
    "smallest_swing_candidate", "winner_has_smallest_swing",
    "all_candidates_worse", "n_candidates_tested", "n_candidates_worse",
    "combined_fourier_order", "combined_prior_scale", "note",
]


def _round_results(results: pd.DataFrame) -> pd.DataFrame:
    out = results.copy()
    for col, nd in [("cv_mape", 2), ("cv_wape", 2), ("seasonal_swing_min_pct", 1),
                    ("seasonal_swing_max_pct", 1), ("seasonal_swing_range_pct", 1)]:
        if col in out.columns:
            out[col] = out[col].astype(float).round(nd)
    return out


# ── Reporting ─────────────────────────────────────────────────────────────────

def print_summary(results: pd.DataFrame, summary: pd.DataFrame,
                  sample: pd.DataFrame) -> None:
    sep = "-" * 78
    print(f"\n{sep}\nEXPERIMENT DESIGN (every number below is a judgment call, not a codebase value)\n{sep}")
    print(f"  sample rule       : every model_metrics.csv series with "
          f">= {MIN_HISTORY_MONTHS} train months")
    print(f"                      (pipeline's own bar is {config.MIN_TRAIN_MONTHS}; "
          f"36 ~ 3 yearly cycles)")
    print(f"  validation        : prophet.diagnostics.cross_validation, rolling cutoffs")
    print(f"                      INSIDE the training window (<= {config.TRAIN_END}). The")
    print(f"                      {config.TEST_START}-{config.TEST_END} held-out window is never scored.")
    print(f"  horizon / period  : {CV_HORIZON} ({CV_HORIZON_MONTHS} monthly points) / "
          f"{CV_PERIOD_MONTHS} months, max {MAX_CUTOFFS} folds")
    print(f"                      (imported from changepoint_experiment, not re-derived)")
    print(f"  control           : order {DEFAULT_ORDER} (= yearly_seasonality=True), "
          f"prior {DEFAULT_PRIOR}, {DEFAULT_MODE}")
    print(f"  candidates        : lower_order {LOWER_ORDERS}, tighter_prior {TIGHTER_PRIORS},")
    print(f"                      combined (per-product best of each), additive_mode")
    print(f"  diagnostic        : (yhat - trend)/trend x100 over the last "
          f"{DIAGNOSTIC_DAYS} days of training,")
    print(f"                      sampled DAILY — the resolution the monthly forecast hides")

    evaluated = summary[summary["winner"] != "not_evaluated"]
    skipped = summary[summary["winner"] == "not_evaluated"]

    print(f"\n{sep}\nCOVERAGE\n{sep}")
    print(f"  model_metrics.csv series          : {len(pd.read_csv(config.OUTPUT_DIR / 'model_metrics.csv'))}")
    print(f"  in sample (>= {MIN_HISTORY_MONTHS} train months)   : {len(sample)} item codes, "
          f"{sample['family_key'].nunique()} distinct fitted series")
    if "mape_pct" in sample.columns:
        bad = int((sample["mape_pct"] > 100).sum())
        print(f"    currently mape_pct > 100        : {bad}")
        print(f"    currently mape_pct <= 100       : {len(sample) - bad}")
        print(f"    (test-window figure, used for cohort labels ONLY — never to score)")
    print(f"  evaluated                         : {len(evaluated)}")
    if len(skipped):
        print(f"\n  NOT EVALUATED — {len(skipped)}:")
        for row in skipped.drop_duplicates("family_key").itertuples(index=False):
            print(f"    {row.family_key:<10} {row.note}")

    if evaluated.empty:
        print("\nNothing could be evaluated.")
        return

    dedup = evaluated.drop_duplicates("family_key")
    det = results.drop_duplicates(["family_key", "candidate"])
    n_series = len(dedup)

    # ── Per-candidate improve / regress ───────────────────────────────────────
    print(f"\n{sep}\nPER-CANDIDATE ACCURACY vs DEFAULT ({n_series} distinct fitted series)\n{sep}")
    base = det[det["candidate"] == CAND_DEFAULT].set_index("family_key")
    print(f"  {'candidate':<20} {'improved':>9} {'regressed':>10} {'n':>5}  "
          f"{'median MAPE delta':>18}")
    order = ([CAND_DEFAULT] + [_order_name(o) for o in LOWER_ORDERS]
             + [_prior_name(p) for p in TIGHTER_PRIORS] + [CAND_COMBINED, CAND_ADDITIVE])
    for cand in order:
        if cand == CAND_DEFAULT:
            continue
        sub = det[det["candidate"] == cand].set_index("family_key")
        joined = sub[["cv_mape"]].join(base[["cv_mape"]], rsuffix="_default",
                                       how="inner").dropna()
        if joined.empty:
            print(f"  {cand:<20} {'-':>9} {'-':>10} {0:>5}")
            continue
        better = int((joined["cv_mape"] < joined["cv_mape_default"]).sum())
        worse = int((joined["cv_mape"] > joined["cv_mape_default"]).sum())
        delta = (joined["cv_mape"] - joined["cv_mape_default"]).median()
        print(f"  {cand:<20} {better:>9} {worse:>10} {len(joined):>5}  "
              f"{delta:>+17.2f}pp")
    print(f"\n  (delta is candidate MAPE minus default MAPE; negative = candidate better)")

    print(f"\n{sep}\nWINNERS — best candidate per series, ties to the incumbent\n{sep}")
    counts = dedup["winner"].value_counts()
    for cand in order:
        if cand in counts.index:
            print(f"  {cand:<20} : {counts[cand]:>4} / {n_series}")
    for cand in counts.index:
        if cand not in order:
            print(f"  {cand:<20} : {counts[cand]:>4} / {n_series}")

    n_all_worse = int(dedup["all_candidates_worse"].sum())
    print(f"\n{sep}\nREGRESSION CHECK — every candidate worse than the current default\n{sep}")
    print(f"  {n_all_worse} / {n_series} series")
    if n_all_worse:
        worst = dedup[dedup["all_candidates_worse"]]
        print("  " + ", ".join(sorted(worst["family_key"].unique())[:25])
              + (" …" if n_all_worse > 25 else ""))

    # ── additive alone vs the parameter changes ───────────────────────────────
    print(f"\n{sep}\nDOES additive_mode ALONE CAPTURE THE PARAMETER CHANGES' BENEFIT?\n{sep}")
    param_cands = [_order_name(o) for o in LOWER_ORDERS] + \
                  [_prior_name(p) for p in TIGHTER_PRIORS] + [CAND_COMBINED]
    best_param = (det[det["candidate"].isin(param_cands)]
                  .dropna(subset=["cv_mape"])
                  .groupby("family_key")["cv_mape"].min().rename("best_param_mape"))
    add = (det[det["candidate"] == CAND_ADDITIVE]
           .set_index("family_key")["cv_mape"].rename("additive_mape"))
    cmp = pd.concat([base["cv_mape"].rename("default_mape"), best_param, add],
                    axis=1).dropna()
    if cmp.empty:
        print("  not comparable — no series has both.")
    else:
        print(f"  series with both measured        : {len(cmp)}")
        print(f"  additive beats default           : "
              f"{int((cmp['additive_mape'] < cmp['default_mape']).sum())}")
        print(f"  best param change beats default  : "
              f"{int((cmp['best_param_mape'] < cmp['default_mape']).sum())}")
        print(f"  additive beats best param change : "
              f"{int((cmp['additive_mape'] < cmp['best_param_mape']).sum())}")
        gap = cmp["additive_mape"] - cmp["best_param_mape"]
        print(f"  median MAPE gap (additive - best param) : {gap.median():+.2f} pp "
              f"(negative = additive alone is better)")
        med_d = cmp["default_mape"].median()
        print(f"  median MAPE  default {med_d:.2f} | best param "
              f"{cmp['best_param_mape'].median():.2f} | additive "
              f"{cmp['additive_mape'].median():.2f}")

    # ── swing magnitudes ──────────────────────────────────────────────────────
    print(f"\n{sep}\nDIAGNOSTIC — daily seasonal swing range, by candidate\n{sep}")
    print(f"  {'candidate':<20} {'median':>12} {'p90':>12} {'max':>14}   "
          f"{'>1000pp':>8}")
    for cand in order:
        sub = det[det["candidate"] == cand]["seasonal_swing_range_pct"].dropna()
        if sub.empty:
            continue
        tag = "  <- current default" if cand == CAND_DEFAULT else ""
        print(f"  {cand:<20} {sub.median():>12.0f} {sub.quantile(0.9):>12.0f} "
              f"{sub.max():>14.0f}   {int((sub > 1000).sum()):>8}{tag}")
    print(f"\n  (range = max - min seasonal % of trend across one year at daily resolution;")
    print(f"   the monthly view the pipeline forecasts at never shows these excursions)")

    # ── do swing reduction and accuracy improvement track together? ───────────
    print(f"\n{sep}\nDO SWING REDUCTION AND ACCURACY IMPROVEMENT TRACK TOGETHER?\n{sep}")
    print(f"  winner also had the smallest swing : "
          f"{int(dedup['winner_has_smallest_swing'].sum())} / {n_series}")
    won_by_alt = dedup[dedup["winner"] != CAND_DEFAULT]
    print(f"  (of the {len(won_by_alt)} series where an alternative won: "
          f"{int(won_by_alt['winner_has_smallest_swing'].sum())})")

    # Per-series correlation between "swing got smaller" and "MAPE got smaller",
    # computed across candidates, then summarised across series. A candidate can
    # only be credited to the overfit mechanism if these move together.
    joined = det.merge(
        base[["cv_mape", "seasonal_swing_range_pct"]].rename(
            columns={"cv_mape": "mape_d", "seasonal_swing_range_pct": "swing_d"}),
        left_on="family_key", right_index=True, how="inner")
    joined = joined[joined["candidate"] != CAND_DEFAULT].dropna(
        subset=["cv_mape", "mape_d", "seasonal_swing_range_pct", "swing_d"])
    if joined.empty:
        print("  no comparable candidate rows.")
    else:
        swing_down = joined["seasonal_swing_range_pct"] < joined["swing_d"]
        mape_down = joined["cv_mape"] < joined["mape_d"]
        n = len(joined)
        print(f"\n  Across all {n} candidate-vs-default comparisons:")
        print(f"    swing DOWN and MAPE DOWN (mechanism holds)  : "
              f"{int((swing_down & mape_down).sum()):>5}  ({(swing_down & mape_down).mean()*100:.0f}%)")
        print(f"    swing DOWN but MAPE UP   (constraint hurt)  : "
              f"{int((swing_down & ~mape_down).sum()):>5}  ({(swing_down & ~mape_down).mean()*100:.0f}%)")
        print(f"    swing UP but MAPE DOWN   (helped, not via")
        print(f"                              the stated cause) : "
              f"{int((~swing_down & mape_down).sum()):>5}  ({(~swing_down & mape_down).mean()*100:.0f}%)")
        print(f"    swing UP and MAPE UP                        : "
              f"{int((~swing_down & ~mape_down).sum()):>5}  ({(~swing_down & ~mape_down).mean()*100:.0f}%)")
        corr = joined[["seasonal_swing_range_pct", "cv_mape"]].corr(method="spearman").iloc[0, 1]
        print(f"\n    Spearman rank correlation (swing range vs CV MAPE), pooled: {corr:+.3f}")
        print(f"    A weak correlation means the two are largely independent — swing")
        print(f"    severity would then NOT be a usable proxy for forecast damage.")

    # ── cohort split: does a fix that helps the bad tail hurt the good ones? ──
    if "mape_pct" in sample.columns:
        print(f"\n{sep}\nCOHORT SPLIT — currently-bad vs currently-good series\n{sep}")
        lbl = (sample.drop_duplicates("family_key")
               .set_index("family_key")["mape_pct"] > 100).rename("currently_bad")
        coh = dedup.set_index("family_key").join(lbl, how="inner")
        for flag, name in [(True, "currently bad (mape>100)"), (False, "currently good")]:
            grp = coh[coh["currently_bad"] == flag]
            if grp.empty:
                continue
            improved = int((grp["winner"] != CAND_DEFAULT).sum())
            print(f"  {name:<26} : {len(grp):>3} series, {improved:>3} improved by some "
                  f"candidate, {int(grp['all_candidates_worse'].sum()):>3} regressed under all")
        print(f"  (cohort label is the held-out test-window MAPE — a LABEL only; every")
        print(f"   score above is in-training cross-validation)")

    print(f"\n{sep}\nLARGEST IMPROVEMENTS (distinct fitted series)\n{sep}")
    top = (dedup[dedup["winner"] != CAND_DEFAULT]
           .sort_values("improvement_pct", ascending=False).head(15))
    if top.empty:
        print("  none — the current default was never beaten.")
    else:
        print(top[["family_key", "n_train_months", "cv_mape_default", "winner",
                   "cv_mape_winner", "improvement_pct", "swing_range_default",
                   "swing_range_winner", "winner_has_smallest_swing"]]
              .to_string(index=False))

    print(f"\n{sep}\nWHAT THIS DOES NOT SAY\n{sep}")
    print("  No adoption is proposed here. These are in-training CV numbers on a")
    print("  single sample; the held-out window remains unread and is still available")
    print("  to score whatever a human decides to adopt.")


def print_timing(results: pd.DataFrame, elapsed: float, n_full_series: int,
                 n_full_items: int) -> None:
    sep = "-" * 78
    n_series = results["family_key"].nunique() if not results.empty else 0
    n_fits = int((results["n_cutoffs"] + 1).sum()) if not results.empty else 0

    print(f"\n{sep}\nSAMPLE TIMING\n{sep}")
    print(f"  distinct series fitted : {n_series}")
    print(f"  candidate rows         : {len(results)}")
    print(f"  Prophet fits           : ~{n_fits}   "
          f"(1 full-history fit + 1 refit per fold, per candidate)")
    print(f"  wall clock             : {elapsed:.1f}s")
    if n_fits:
        print(f"  per fit                : {elapsed / n_fits:.2f}s")
    if n_series:
        per_series = elapsed / n_series
        print(f"  per distinct series    : {per_series:.1f}s")
        print(f"\n  EXTRAPOLATION to the full sample "
              f"({n_full_items} item codes -> {n_full_series} distinct fitted series):")
        print(f"    {n_full_series} x {per_series:.1f}s = "
              f"{n_full_series * per_series / 60:.1f} min")
        print(f"\n  Levers if that is too long, in the order that costs least information:")
        print(f"    - MAX_CUTOFFS {MAX_CUTOFFS} -> 4      (~-30% runtime, fewer folds per score)")
        print(f"    - drop one sub-variant per candidate  (~-15%)")
        print(f"    - sample the product list             (loses the good/bad cohort split)")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--db", action="store_true",
                    help="Reload raw inputs from Azure SQL instead of the cached CSVs")
    ap.add_argument("--sample", type=int, default=0, metavar="N",
                    help="Time a sample of N distinct fitted series and stop without "
                         "writing any CSV")
    args = ap.parse_args()

    sample = load_sample()
    n_full_items = len(sample)
    n_full_series = sample["family_key"].nunique()
    print(f"Sample: {n_full_items} item codes with >= {MIN_HISTORY_MONTHS} train months "
          f"({n_full_series} distinct fitted series)")

    scoped, _families, _eligible = build_fit_input(cached=not args.db)

    if args.sample:
        # Span the history range so the timing is not taken from the shortest
        # (and therefore fastest) series only.
        keys = (sample.drop_duplicates("family_key")
                .sort_values("n_train_months")["family_key"].tolist())
        step = max(1, len(keys) // args.sample)
        picked = keys[::step][:args.sample]
        sample = sample[sample["family_key"].isin(picked)].drop_duplicates("family_key")
        print(f"\nSAMPLE MODE — {len(sample)} distinct series "
              f"({sample['n_train_months'].min()}-{sample['n_train_months'].max()} train months)")

    print()
    results, summary, elapsed = run_experiment(sample, scoped)

    if args.sample:
        print_timing(results, elapsed, n_full_series, n_full_items)
        print("\nSample mode: no CSV written. Re-run without --sample for the full experiment.")
        return 0

    results = _round_results(results).reindex(columns=RESULT_COLS)
    summary = summary.reindex(columns=SUMMARY_COLS)

    out = config.OUTPUT_DIR
    for path, frame in [
        (out / "seasonality_experiment_results.csv", results),
        (out / "seasonality_experiment_summary.csv", summary),
    ]:
        frame.to_csv(path, index=False)
        print(f"\nWrote {path}  ({len(frame):,} rows)")

    print_summary(results, summary, load_sample())
    print(f"\nTotal wall clock: {elapsed / 60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
