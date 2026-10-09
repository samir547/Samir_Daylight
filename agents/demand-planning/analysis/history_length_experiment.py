"""
Training-history-length experiment — does the global seasonality fit overfit short-history series? (Phase 4)

WHAT THIS IS
────────────
Investigating one real forecast: A25090 (AU), MAPE 94.3%, bias +51.3% (Prophet
over-forecasting by roughly half, summed across the test window), on a series
with only 45 months of training history — well under the ~96-month most of the
catalog carries, though still above config.MIN_TRAIN_MONTHS = 24. Its component
plot shows a dead-straight, ever-climbing trend and a THREE-peak yearly
seasonal curve swinging -75% to +62% — six free Fourier parameters
(yearly_seasonality = 3) fitted against well under four years of a genuinely
volatile series.

A hand tally against the last full run's model_metrics.csv found the same
signature is not unique to A25090 or to Australia: four AU items under 50
training months averaged 104% absolute bias against seven longer-history AU
items averaging 22%; short-history items in every other region (D25101,
D35851, E31875, U25101, U36401, ...) showed the same pattern on inspection.
That is suggestive, not a finding — four AU items and a handful of spot-checks
by hand, not a controlled comparison. This module IS that comparison.

THE QUESTION
────────────
`yearly_seasonality = 3` and `seasonality_prior_scale = 10.0` are GLOBAL
defaults, validated once by analysis/seasonality_experiment.py on the whole
catalog's CV performance (order 3 beat order 10 on 102/152 series). A
population-level win does not guarantee the right choice for every individual
series — six Fourier parameters is a lot to ask well under 50 months of
monthly data to support without fitting the seasonal curve to noise. This
experiment measures whether either of two more conservative seasonality
configurations reduces cross-validated error relative to today's global
default, and — the actual point of the exercise — whether any such benefit is
concentrated among short-history series specifically, and at what
n_train_months it appears or disappears. It does NOT presuppose the ~50-month
line the hand tally suggested; that boundary is an input to compare against,
not a filter applied before running.

Like analysis/changepoint_experiment.py, and for the same reason: this module
MEASURES two candidate remedies. It does not adopt either one. Nothing here
changes config.py, preprocessing/, models/, any existing output/*.csv, or the
database.

    remedy A — LOWER FOURIER ORDER.  yearly_seasonality = 1 (one sin/cos pair,
        2 free parameters instead of 6), seasonality_prior_scale unchanged.

    remedy B — TIGHTER PRIOR.  yearly_seasonality unchanged (3),
        seasonality_prior_scale = 3.0 — the same amplitude-dampening idea
        analysis/seasonality_experiment.py tried at the whole-catalog level
        and found only helped in combination with per-product Fourier tuning
        (out of scope there; exactly in scope here for a targeted segment).

Order 10 is not retested: analysis/seasonality_experiment.py already rejected
it decisively (102/152 series, median -10.2% MAPE), and nothing about a
short-history segment argues for MORE parameters, only fewer.

WHY NOT SCORE ON THE EXISTING TEST WINDOW
──────────────────────────────────────────
Exactly analysis/changepoint_experiment.py's reasoning, restated because it
matters as much here: the held-out window is what surfaced A25090 as
suspicious in the first place. Scoring a candidate fix on the window that
motivated the investigation would be tuning against the test set. Every
variant here is fitted on data ending at config.TRAIN_END and scored by
prophet.diagnostics.cross_validation at rolling cutoffs INSIDE that training
window — the same CV geometry (190-day horizon, 3-month period, up to 6 folds,
config.MIN_TRAIN_MONTHS as the fold floor) analysis/changepoint_experiment.py
already validated and this module reuses (make_cutoffs is imported from it,
not reimplemented), so the two experiments' numbers stay comparable and a
change to the CV geometry only has to be made once.

SCOPE: THE FULL FITTED CATALOG, NOT A PRE-FLAGGED TAIL
────────────────────────────────────────────────────────
analysis/trend_audit.py and analysis/changepoint_experiment.py both target a
pre-identified "badly wrong" tail. This module does not: it runs over every
series build_fit_input() would hand to Prophet, because the question is
whether a training-length threshold exists at all — pre-filtering to
already-bad series would beg exactly that question. Cost is the trade-off:
every series x 3 variants x up to 6 CV folds is a lot of Prophet fits. See
--sample before committing to a full run.

Run:  python -m analysis.history_length_experiment --sample 10   (timing probe)
      python -m analysis.history_length_experiment                (full catalog)
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
from analysis.changepoint_experiment import make_cutoffs  # noqa: E402 — same CV geometry, one definition
from analysis.trend_audit import build_fit_input  # noqa: E402 — same rebuild, one definition
from models.prophet_model import TRAIN_END_TS, TRAIN_START_TS  # noqa: E402

logging.getLogger("cmdstanpy").setLevel(logging.ERROR)
logging.getLogger("prophet").setLevel(logging.ERROR)

# ── Experiment design constants ────────────────────────────────────────────────

# CV fold geometry: reused from analysis.changepoint_experiment (imported, not
# retyped) via make_cutoffs(). MIN_CV_TRAIN_MONTHS/CV_HORIZON_MONTHS restated
# here only because print_summary() reports them; they are not re-derived.
CV_HORIZON_MONTHS = 6
CV_HORIZON = "190 days"
MIN_CV_TRAIN_MONTHS = config.MIN_TRAIN_MONTHS

DEFAULT_YEARLY = config.PROPHET_PARAMS["yearly_seasonality"]
DEFAULT_SEASON_PRIOR = config.PROPHET_PARAMS["seasonality_prior_scale"]

VARIANT_DEFAULT = "default"
VARIANT_LOWER_ORDER = "yearly_order_1"
VARIANT_TIGHTER_PRIOR = "season_prior_3.0"

VARIANTS: dict[str, dict] = {
    VARIANT_DEFAULT: dict(
        yearly_seasonality=DEFAULT_YEARLY, seasonality_prior_scale=DEFAULT_SEASON_PRIOR,
    ),
    VARIANT_LOWER_ORDER: dict(
        yearly_seasonality=1, seasonality_prior_scale=DEFAULT_SEASON_PRIOR,
    ),
    VARIANT_TIGHTER_PRIOR: dict(
        yearly_seasonality=DEFAULT_YEARLY, seasonality_prior_scale=3.0,
    ),
}

# n_train_months bucket edges for the correlation report. Not a threshold
# decision, just a lens: finer buckets either side of the hand tally's rough
# 50-month split so a different true crossover point would still show up
# rather than being baked in by the bucketing itself.
TRAIN_MONTHS_BUCKETS = [0, 36, 50, 70, 200]
TRAIN_MONTHS_LABELS = ["<36", "36-49", "50-69", "70+"]

# First character of the fit key -> region, matching forecast_training_data.sql's
# own derivation (LEFT(item_code, 1)). A fit key that matches none of these
# should not occur in practice; reported as 'Other' rather than raising, so a
# surprise shows up in the output instead of crashing the run.
REGION_BY_PREFIX = {"A": "AU", "D": "UK", "E": "EU", "U": "USA"}


def region_of(item_code: str) -> str:
    return REGION_BY_PREFIX.get(str(item_code).strip()[:1].upper(), "Other")


# ── Scoring ───────────────────────────────────────────────────────────────────

def _fold_scores(cv: pd.DataFrame) -> dict:
    """
    Per-cutoff MAPE/WAPE averaged over folds (matching
    analysis.changepoint_experiment's convention), plus a POOLED bias.

    Bias is deliberately NOT averaged per-fold-then-across-folds: a series with
    +60% bias in one fold and -60% in another would average to ~0 and hide the
    exact instability this experiment exists to detect. Pooling every fold's
    rows before computing Sigma(p-a)/Sigma(a) — evaluation.metrics.bias()'s own
    formula, reused here for interpretive consistency with model_metrics.csv —
    gives one number for "which direction, and how far off, in total".
    """
    cv = cv.copy()
    cv["yhat"] = cv["yhat"].clip(lower=0)
    cv["err"] = cv["yhat"] - cv["y"]
    cv["abs_err"] = cv["err"].abs()

    mapes, wapes = [], []
    for _, fold in cv.groupby("cutoff"):
        nz = fold[fold["y"] != 0]
        if len(nz):
            mapes.append(float((nz["abs_err"] / nz["y"].abs()).mean() * 100))
        denom = float(fold["y"].abs().sum())
        if denom > 0:
            wapes.append(float(fold["abs_err"].sum() / denom * 100))

    denom_all = float(cv["y"].sum())
    bias = float(cv["err"].sum() / denom_all * 100) if denom_all != 0 else float("nan")

    return {
        "cv_mape": float(np.mean(mapes)) if mapes else float("nan"),
        "cv_wape": float(np.mean(wapes)) if wapes else float("nan"),
        "cv_bias": bias,
        "n_scored_points": len(cv),
    }


def run_variant(train: pd.DataFrame, cutoffs: list[pd.Timestamp], params: dict) -> dict:
    """
    Fit one seasonality variant and cross-validate on `cutoffs`.

    Every other Prophet param comes from config.PROPHET_PARAMS verbatim — only
    yearly_seasonality / seasonality_prior_scale are overridden per VARIANTS, so
    any score difference is attributable to those two alone, not a drifted copy
    of the production config.
    """
    full_params = dict(config.PROPHET_PARAMS)
    full_params.update(params)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = Prophet(**full_params)
        model.fit(train)
        cv = cross_validation(model, horizon=CV_HORIZON, cutoffs=cutoffs, disable_tqdm=True)

    scores = _fold_scores(cv)
    scores["n_cutoffs"] = len(cutoffs)
    return scores


# ── Per-series experiment ─────────────────────────────────────────────────────

def run_series(fit_key: str, series: pd.DataFrame) -> tuple[list[dict], dict]:
    """Run every variant in VARIANTS for one fitted series. Returns (detail_rows, status)."""
    train = (
        series[(series["ds"] >= TRAIN_START_TS) & (series["ds"] <= TRAIN_END_TS)]
        [["ds", "y"]].dropna().sort_values("ds").reset_index(drop=True)
    )
    status = {"fit_key": fit_key, "n_train_months": len(train), "n_cutoffs": 0, "skip_reason": ""}

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
    for variant, params in VARIANTS.items():
        res = run_variant(train, cutoffs, params)
        res.update({"fit_key": fit_key, "variant": variant})
        rows.append(res)
    return rows, status


def summarise_series(detail: pd.DataFrame) -> dict:
    """
    Collapse one series' variant rows into a verdict.

    Ties go to the incumbent, matching analysis.changepoint_experiment's
    convention: an alternative must actually beat the default's CV MAPE — not
    merely differ from it — to be called a winner. Bias is reported for every
    variant regardless of which one wins by MAPE, since bias direction/size is
    the symptom that motivated this whole experiment and a MAPE-winner that
    doesn't also help bias is a weaker result worth being able to see.
    """
    by_variant = detail.set_index("variant")

    def get(v: str, col: str) -> float:
        return float(by_variant.loc[v, col]) if v in by_variant.index else float("nan")

    default_mape, default_bias = get(VARIANT_DEFAULT, "cv_mape"), get(VARIANT_DEFAULT, "cv_bias")

    winner, best_mape = VARIANT_DEFAULT, default_mape
    for v in VARIANTS:
        if v == VARIANT_DEFAULT:
            continue
        val = get(v, "cv_mape")
        if pd.notna(val) and (pd.isna(best_mape) or val < best_mape):
            winner, best_mape = v, val

    winner_bias = get(winner, "cv_bias")
    mape_improvement = (
        (default_mape - best_mape) / default_mape * 100
        if pd.notna(default_mape) and default_mape != 0 and pd.notna(best_mape) else float("nan")
    )
    bias_improvement = (
        abs(default_bias) - abs(winner_bias)
        if pd.notna(default_bias) and pd.notna(winner_bias) else float("nan")
    )

    return {
        "cv_mape_default": round(default_mape, 2) if pd.notna(default_mape) else np.nan,
        "cv_bias_default": round(default_bias, 2) if pd.notna(default_bias) else np.nan,
        "cv_mape_lower_order": round(get(VARIANT_LOWER_ORDER, "cv_mape"), 2),
        "cv_bias_lower_order": round(get(VARIANT_LOWER_ORDER, "cv_bias"), 2),
        "cv_mape_tighter_prior": round(get(VARIANT_TIGHTER_PRIOR, "cv_mape"), 2),
        "cv_bias_tighter_prior": round(get(VARIANT_TIGHTER_PRIOR, "cv_bias"), 2),
        "winner": winner,
        "mape_improvement_pct": round(mape_improvement, 2) if pd.notna(mape_improvement) else np.nan,
        "abs_bias_reduction_pp": round(bias_improvement, 2) if pd.notna(bias_improvement) else np.nan,
    }


# ── Assembly ──────────────────────────────────────────────────────────────────

def run_experiment(scoped: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, float]:
    """Fit every distinct fitted series in `scoped` once, across all VARIANTS."""
    detail_frames: list[pd.DataFrame] = []
    result_rows: list[dict] = []
    started = time.perf_counter()

    keys = sorted(scoped["item_code"].astype(str).str.strip().unique())
    for i, key in enumerate(keys, start=1):
        series = scoped[scoped["item_code"].astype(str).str.strip() == key]
        elapsed = time.perf_counter() - started
        print(f"  [{i:>3}/{len(keys)}] {key} … {elapsed:6.1f}s elapsed", flush=True)

        rows, status = run_series(key, series)
        base = {
            "item_code": key, "region": region_of(key),
            "n_train_months": status["n_train_months"], "n_cutoffs": status["n_cutoffs"],
        }
        if not rows:
            result_rows.append({
                **base, "winner": "not_evaluated",
                "cv_mape_default": np.nan, "cv_bias_default": np.nan,
                "cv_mape_lower_order": np.nan, "cv_bias_lower_order": np.nan,
                "cv_mape_tighter_prior": np.nan, "cv_bias_tighter_prior": np.nan,
                "mape_improvement_pct": np.nan, "abs_bias_reduction_pp": np.nan,
                "note": status["skip_reason"],
            })
            continue

        detail = pd.DataFrame(rows)
        result_rows.append({**base, **summarise_series(detail), "note": ""})
        tagged = detail.copy()
        tagged.insert(0, "item_code", key)
        detail_frames.append(tagged)

    results = pd.DataFrame(result_rows)
    detail_all = pd.concat(detail_frames, ignore_index=True) if detail_frames else pd.DataFrame()
    return results, detail_all, time.perf_counter() - started


def bucket_train_months(results: pd.DataFrame) -> pd.DataFrame:
    out = results.copy()
    out["train_months_bucket"] = pd.cut(
        out["n_train_months"], bins=TRAIN_MONTHS_BUCKETS, labels=TRAIN_MONTHS_LABELS, right=False,
    )
    return out


# ── Reporting ─────────────────────────────────────────────────────────────────

def print_summary(results: pd.DataFrame) -> None:
    sep = "-" * 78
    print(f"\n{sep}\nEXPERIMENT DESIGN\n{sep}")
    print(f"  validation       : prophet.diagnostics.cross_validation, rolling cutoffs")
    print(f"                     INSIDE the training window (<= {config.TRAIN_END}); the")
    print(f"                     {config.TEST_START}-{config.TEST_END} held-out window is never read")
    print(f"  horizon          : {CV_HORIZON} ({CV_HORIZON_MONTHS} monthly points); "
          f"cutoff geometry from analysis.changepoint_experiment.make_cutoffs")
    print(f"  fold minimum     : {MIN_CV_TRAIN_MONTHS} observed months before a cutoff")
    print(f"  variants         : default (yearly={DEFAULT_YEARLY}, "
          f"season_prior={DEFAULT_SEASON_PRIOR}) vs yearly=1 vs season_prior=3.0")
    print(f"  winner rule      : lowest CV MAPE; ties go to the incumbent default")

    evaluated = results[results["winner"] != "not_evaluated"]
    skipped = results[results["winner"] == "not_evaluated"]
    print(f"\n{sep}\nCOVERAGE\n{sep}")
    print(f"  fitted series    : {len(results)}  ({len(evaluated)} evaluated, {len(skipped)} skipped)")
    if len(skipped):
        print(f"\n  SKIPPED — insufficient history for an honest CV fold:")
        for row in skipped.itertuples(index=False):
            print(f"    {row.item_code:<10} ({row.n_train_months} train months) {row.note}")

    if evaluated.empty:
        print("\nNothing could be evaluated.")
        return

    bucketed = bucket_train_months(evaluated)

    print(f"\n{sep}\nTHE CORE QUESTION — does an alternative win more, and reduce bias more, "
          f"as history gets shorter?\n{sep}")
    rows = []
    for label, grp in bucketed.groupby("train_months_bucket", observed=True):
        if grp.empty:
            continue
        n = len(grp)
        won = grp[grp["winner"] != VARIANT_DEFAULT]
        rows.append({
            "train_months": label,
            "n_series": n,
            "avg_default_bias_pct": round(grp["cv_bias_default"].mean(), 1),
            "avg_default_abs_bias_pct": round(grp["cv_bias_default"].abs().mean(), 1),
            "alt_win_rate_pct": round(100 * len(won) / n, 1),
            "avg_abs_bias_reduction_pp_when_won": round(won["abs_bias_reduction_pp"].mean(), 1) if len(won) else np.nan,
            "avg_mape_improvement_pct_when_won": round(won["mape_improvement_pct"].mean(), 1) if len(won) else np.nan,
        })
    print(pd.DataFrame(rows).to_string(index=False))

    print(f"\n{sep}\nSAME BREAKDOWN, BY REGION — is this Australia-specific, or a short-history "
          f"problem Australia happens to have more of?\n{sep}")
    for region in ["AU", "UK", "EU", "USA", "Other"]:
        sub = bucketed[bucketed["region"] == region]
        if sub.empty:
            continue
        print(f"\n  {region} ({len(sub)} series):")
        rows = []
        for label, grp in sub.groupby("train_months_bucket", observed=True):
            if grp.empty:
                continue
            n = len(grp)
            n_won = (grp["winner"] != VARIANT_DEFAULT).sum()
            rows.append({
                "train_months": label, "n": n,
                "avg_default_abs_bias_pct": round(grp["cv_bias_default"].abs().mean(), 1),
                "alt_win_rate_pct": round(100 * n_won / n, 1) if n else np.nan,
            })
        tbl = pd.DataFrame(rows).to_string(index=False)
        print("\n".join("    " + line for line in tbl.split("\n")))

    print(f"\n{sep}\nOVERALL WINNER TALLY\n{sep}")
    counts = evaluated["winner"].value_counts()
    for name, n in counts.items():
        tag = "  (= current default; alternatives never won)" if name == VARIANT_DEFAULT else ""
        print(f"  {name:<20} : {n:>4} / {len(evaluated)}{tag}")

    print(f"\n{sep}\nLARGEST BIAS REDUCTIONS (where an alternative won on MAPE)\n{sep}")
    top = evaluated[evaluated["winner"] != VARIANT_DEFAULT].sort_values(
        "abs_bias_reduction_pp", ascending=False
    ).head(15)
    if top.empty:
        print("  none — the default was never beaten.")
    else:
        print(top[["item_code", "region", "n_train_months", "winner",
                   "cv_bias_default", "abs_bias_reduction_pp", "mape_improvement_pct"]]
              .to_string(index=False))


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--db", action="store_true",
                    help="Reload raw inputs from Azure SQL instead of the cached CSVs")
    ap.add_argument("--sample", type=int, default=0, metavar="N",
                    help="Time a sample of N fitted series, spread across the "
                         "n_train_months range, and stop without writing the result CSV")
    args = ap.parse_args()

    scoped, _families, _eligible = build_fit_input(cached=not args.db)
    keys = sorted(scoped["item_code"].astype(str).str.strip().unique())
    print(f"Fitted series available: {len(keys)}")

    if args.sample:
        # Spread the sample across the n_train_months range rather than taking
        # the first N alphabetically, so a timing probe also gives an early
        # read on whether short-history series behave differently — not just
        # how long the full run will take. Row count per key is a rough proxy
        # for training length here (good enough for sampling; run_series()
        # computes the real, TRAIN-window-bounded n_train_months per series).
        lengths = sorted(
            ((k, len(scoped[scoped["item_code"].astype(str).str.strip() == k])) for k in keys),
            key=lambda t: t[1],
        )
        step = max(1, len(lengths) // args.sample)
        sample_keys = [k for k, _ in lengths[::step][:args.sample]]
        scoped = scoped[scoped["item_code"].astype(str).str.strip().isin(sample_keys)]
        print(f"SAMPLE MODE — {len(sample_keys)} series spread across the training-history range")

    results, detail, elapsed = run_experiment(scoped)

    if args.sample:
        print(f"\n{'-'*78}\nSAMPLE TIMING\n{'-'*78}")
        print(f"  series sampled     : {len(results)}")
        print(f"  wall clock         : {elapsed:.1f}s")
        if len(results):
            per_series = elapsed / len(results)
            print(f"  per series         : {per_series:.1f}s  (3 variants x up to 6 CV folds each)")
            print(f"\n  EXTRAPOLATION to the full catalog ({len(keys)} series):")
            print(f"    {len(keys)} x {per_series:.1f}s = {len(keys) * per_series / 60:.1f} min")
        print("\nSample mode: no CSV written. Re-run without --sample for the full experiment.")
        return 0

    out = config.OUTPUT_DIR
    for path, frame in [
        (out / "history_length_experiment_results.csv", results),
        (out / "history_length_experiment_detail.csv", detail),
    ]:
        frame.to_csv(path, index=False)
        print(f"\nWrote {path}  ({len(frame):,} rows)")

    print_summary(results)
    print(f"\nTotal wall clock: {elapsed / 60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
