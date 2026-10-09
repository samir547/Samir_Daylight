"""
Per-item model tournament — does letting each product pick its own tool beat a
fixed routing rule? (read-only)

WHAT THIS IS, AND WHY IT IS DIFFERENT FROM EVERY OTHER FILE THIS WEEK
─────────────────────────────────────────────────────────────────────
Every experiment this week asked one model a question on one nominated
population. `erratic_naive_experiment.py` asked whether arithmetic beats Prophet
on the Erratic items. `croston_experiment.py` asked it of Lumpy/Intermittent.
`ets_sarima_experiment.py` asked whether a fitted competitor helps on two
shortlists of 22 items chosen by an ACF ladder and a trend statistic.

None of those tests the hypothesis that started the week, which is not about any
one model. It is: DOES PER-ITEM SELECTION BEAT A FIXED RULE? Production routes
by demand pattern — Smooth/Erratic to Prophet, Lumpy/Intermittent to a naive
window — and that rule is applied to every item without ever asking the item's
own history which forecaster fits it best. This file asks exactly that, of
EVERY item in the current book rather than a shortlist, and then asks the only
question that makes the answer worth anything: does the per-item choice close
more ground against BDM than the fixed rule does.

Nothing here is new machinery. The CV folds, the fitting functions, the tie
tolerance, the shrinkage bands and the two-track BDM design are all imported
from the files that built them; what is new is the population (all of it), the
selection (per item, not per population) and the safeguard around the selection.

THE SAFEGUARD IS THE POINT, NOT A FORMALITY
───────────────────────────────────────────
A tournament over seven candidates on six folds will ALWAYS produce a winner per
item. On a 199-item book with roughly six folds each, picking the per-item
minimum of seven noisy numbers is close to a machine for manufacturing false
positives: the winner is whichever candidate got the luckiest draw, and reported
without a bar it would read as 199 discoveries. Three things stop that here, and
all three are IMPORTED bars rather than bars chosen to make this result look
good:

  1. EVIDENCE BAR — `MIN_FOLDS_FOR_VERDICT` (3), from
     `erratic_naive_experiment.py`. Below three scored folds no switch is even
     considered; the item keeps its production method and is labelled
     "insufficient evidence". It is NOT dropped — its numbers still print.

  2. MATERIALITY BAR — `TIE_TOLERANCE_PP` (5.0), the same materiality bar the
     blend track and SARIMA's track 2 were judged on this week. A candidate has
     to beat the CURRENT PRODUCTION METHOD by more than five WAPE points. A
     nominal edge keeps the current method.

  3. TWO COLUMNS, NEVER ONE — `cv_winner` (the raw argmin, no bars) and
     `confident_switch` (both bars cleared) are separate columns and are never
     collapsed. The gap between them IS the overfitting measurement: if the raw
     winner differs from the current method on most items but almost none of
     those clear the bars, that is the finding.

Most items are expected to keep their current method under this bar. That is an
honest outcome of an honest test, not a failed experiment, and the report says
so in those words.

THE FOUR THINGS THIS DESIGN CANNOT DO, STATED BEFORE ANY NUMBER
───────────────────────────────────────────────────────────────
1. SELECTION IS PER FIT KEY, NOT PER ITEM, FOR POOLED FAMILIES. `pooled.data`
   is at family grain — that is what `family_pool.pool_for_training()` produces
   and what production fits — so items in a successor family share one series,
   one fold set and therefore one tournament. They cannot pick different tools
   from each other. `cv_key` and `cv_key_shared_with` name this on every row,
   and the aggregate is reported both per item and per distinct fit key, since
   199 items over 182 keys is not 199 independent selections.

2. THE SELECTION IS SCORED ON THE FOLDS THAT MADE IT. Track 1 picks the winner
   and track 1 also reports its WAPE, so `cv_winner_wape` is an in-sample number
   for the SELECTION even though each fold is out-of-sample for the FIT. Track 2
   is the only out-of-sample test of the selection itself, and it is the track
   the headline answer is read from for that reason.

3. TRACK 2 IS ONE WINDOW. Six months, one origin, per item. It is where BDM
   lives and it is the weaker design. See the two-track note below.

4. NOTHING HERE IS ADOPTED. `config.py`, `preprocessing/model_routing.py` and
   `main.py` are untouched, and no existing output CSV is rewritten.

THE TWO TRACKS, AND WHY THEY ARE STILL TWO
──────────────────────────────────────────
Solved already in `ets_sarima_experiment.py` and reused unchanged: CV folds are
rolling origins INSIDE the training window (<= TRAIN_END, 2026-01), BDM's manual
sheet is the held-out window (`config.BENCHMARK_START..END` = 2026-02..2026-07).
There is no month on which both exist, so they are never averaged.

  TRACK 1 (CV)  — every candidate on identical folds per item. Selection happens
                  here and ONLY here. BDM cannot appear.
  TRACK 2 (BDM) — the held-out window. The current production method is scored
                  as the forecast the pipeline ACTUALLY SHIPPED (read from
                  `benchmark_comparison.csv`, not refitted), the tournament
                  method is refit once on history <= TRAIN_END, and BDM is read
                  from the same file. All three on identical item-months.

THE GRAIN TRAP IN TRACK 2, WHICH THE BLEND TRACK ALREADY PAID FOR
─────────────────────────────────────────────────────────────────
`ets_sarima_experiment.blend_track()` records a real bug it caught: for a pooled
successor family, a level computed on `pooled.data` is the FAMILY's rate, while
the forecast in `test_validation.csv` has already been split back to the item by
`split_ratio`. Scoring one against the other blends grains and on U35309
(split_ratio 0.116) turned a 36.0 WAPE into 420.5.

Track 1 is immune — every candidate there is family-grain against family-grain
actuals, so the comparison is internally consistent. Track 2 is NOT: its actuals
are item-grain. So every REFIT candidate's window forecast is multiplied by that
item's `split_ratio` before scoring, and `window_identity_check()` proves the
reconstruction lands on the shipped forecast for the items whose routed model
this file can rebuild exactly. Singletons carry split_ratio 1.0, so the
multiplication is a no-op for most of the book.

Run:  python -m analysis.model_tournament --sample 5
      python -m analysis.model_tournament --cv-only
      python -m analysis.model_tournament
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
from analysis.changepoint_experiment import (  # noqa: E402  — reused, not re-derived
    CV_HORIZON,
    CV_HORIZON_MONTHS,
    CV_PERIOD_MONTHS,
    MAX_CUTOFFS,
    MIN_CV_TRAIN_MONTHS,
    make_cutoffs,
)
from analysis.croston_experiment import monthly_grid  # noqa: E402
from analysis.demand_classification_report import build_scope_input  # noqa: E402
from analysis.erratic_naive_experiment import (  # noqa: E402  — reused, not re-derived
    MIN_FOLDS_FOR_VERDICT,
    NEAR_ZERO_LEVEL,
    NEAR_ZERO_RATE_FRACTION,
    SHRINKAGE_MATERIAL,
    TIE_TOLERANCE_PP,
    naive_level,
)
from analysis.ets_sarima_experiment import (  # noqa: E402  — reused, not re-derived
    BDM,
    BLEND_WEIGHT,
    CAND_BLEND,
    CAND_NAIVE12,
    CAND_PROPHET,
    CAND_ROUTED,
    CAND_SARIMA,
    ETS_VARIANTS,
    NAIVE_VARIANTS,
    SARIMA_ORDER,
    SARIMA_SEASONAL_ORDER,
    SARIMA_SEASONAL_PERIOD,
    _statsmodels_yhat,
    load_bdm_window,
    shrinkage_verdict,
)
from models.prophet_model import TRAIN_END_TS, TRAIN_START_TS  # noqa: E402

logging.getLogger("cmdstanpy").setLevel(logging.ERROR)
logging.getLogger("prophet").setLevel(logging.ERROR)


# ── The candidate set ─────────────────────────────────────────────────────────

#: Every candidate the tournament can select, in a fixed order so a tie broken
#: by position is broken the same way in every table.
#:
#: The three incumbents first — they are what production already has and what a
#: new candidate has to displace — then the two fitted trend models, then the
#: seasonal one, then the blend. `CAND_BLEND` is last because it is not a model:
#: it is a rule applied to whichever model the item is already routed to, so it
#: cannot exist independently of that routing.
ALL_CANDIDATES = ([CAND_PROPHET] + NAIVE_VARIANTS + ETS_VARIANTS
                  + [CAND_SARIMA, CAND_BLEND])

#: SARIMA IS OFFERED TO EVERY ITEM, WITH NO CYCLE PRE-SCREEN. This is a
#: deliberate reversal of `ets_sarima_experiment.py`'s Shortlist A design, and
#: the reason is that file's own result: the ACF rule nominated a handful of
#: items and the one that looked MOST cyclical by eye (E35152, acf1 -0.72) did
#: not clear it either. Re-applying that screen here would exclude almost the
#: whole book from the seasonal candidate and turn an open tournament into the
#: same shortlist test run again. The CV folds decide, not a pre-screen — which
#: is the entire premise of a tournament. `SARIMA_SEASONAL_PERIOD` (3) and the
#: orders are imported unchanged, so a poor SARIMA result here is a result about
#: THAT spec, exactly as it was there. The spec itself is printed by
#: `print_design()` from the imported constants, so it can never drift from the
#: one `_statsmodels_yhat()` actually fits.

#: Prophet's gate. `config.MIN_TRAIN_MONTHS` (24), the SAME bar
#: `model_routing.route_one()` gate 6 applies and the same bar
#: `MIN_CV_TRAIN_MONTHS` applies to a fold — counted the same way, as DISTINCT
#: OBSERVED training months, because `prepare()` emits no zero-quantity rows and
#: `model_routing.train_month_counts()` counts exactly that.
#:
#: In practice this gate rarely fires independently: `make_cutoffs()` already
#: requires MIN_CV_TRAIN_MONTHS observed months before a cutoff can exist, so an
#: item below the bar has no folds to score anyone on. It is still written and
#: still logged, because "Prophet was not offered" and "Prophet was offered and
#: lost" are different rows and the CSV has to be able to tell them apart.
PROPHET_MIN_MONTHS = config.MIN_TRAIN_MONTHS

#: Fold membership provenance. `prophet_cv` is the week's convention — every
#: candidate's yhat is laid against the rows Prophet's own `cross_validation`
#: returned, so a cutoff Prophet drops is dropped for everyone and no two
#: candidates can be scored on different months. `manual` is the fallback for an
#: item Prophet is not offered on at all; it reproduces the same (cutoff, ds, y)
#: construction from the observed training rows so such an item still gets a
#: tournament between its remaining candidates rather than no row at all.
FOLD_BASIS_PROPHET = "prophet_cv"
FOLD_BASIS_MANUAL = "manual (Prophet not offered)"

#: The seven-item severe-bias cluster from `erratic_naive_experiment.py`.
#: CARRIED here, not excluded — see `load_population()` — and flagged so every
#: aggregate can be read both ways.
BIAS_CLUSTER = ["E35090", "E35231", "E35070", "E35152", "EN1540", "UN1350",
                "UN91171"]


# ── Population ────────────────────────────────────────────────────────────────

def load_population(out_dir: Path) -> pd.DataFrame:
    """
    Every item in the current book, each carrying its production routing.

    Straight from `test_validation.csv`, which is `main()`'s own held-out
    output: one row per item-month with the `model` that produced it, the
    `family_key` it was fitted under and the `split_ratio` it was split back out
    by. Reading the book from there rather than re-running `route_scope()` is
    deliberate — the question is what production ACTUALLY SHIPPED, and a
    re-derived route could differ from the delivered one without anything in
    this file noticing.

    This is the same population `ets_sarima_experiment.blend_track()` scores, and
    it is the whole book rather than a shortlist. No exclusions: the severe-bias
    cluster that file dropped from its SHORTLIST track is in here, for that
    file's own stated reason — the blend track "scores every item the pipeline
    shipped and would be a different population if it dropped seven". Those seven
    are flagged `bias_cluster` so any aggregate can be read with and without them.
    """
    tv = pd.read_csv(out_dir / "test_validation.csv")
    for col in ("item_code", "family_key", "model"):
        tv[col] = tv[col].astype(str).str.strip()
    tv["split_ratio"] = pd.to_numeric(tv["split_ratio"], errors="coerce").fillna(1.0)

    keep = ["item_code", "family_key", "model", "split_ratio"]
    if "family" in tv.columns:
        keep.append("family")
    pop = (tv.drop_duplicates("item_code")[keep]
           .rename(columns={"model": "current_method"})
           .sort_values("item_code").reset_index(drop=True))

    # A pooled family's items share one fitted series and therefore one
    # tournament. Naming that on the row is what stops N rows being read as N
    # independent selections.
    shared = pop.groupby("family_key")["item_code"].transform("size")
    pop["cv_key"] = pop["family_key"]
    pop["cv_key_shared_with"] = (shared - 1).astype(int)
    pop["is_pooled"] = pop["family_key"] != pop["item_code"]
    pop["bias_cluster"] = pop["item_code"].isin(BIAS_CLUSTER)
    return pop


# ── TRACK 1: identical folds per item, every viable candidate ─────────────────

def manual_cv_frame(train: pd.DataFrame, cutoffs: list[pd.Timestamp]
                    ) -> pd.DataFrame:
    """
    The (cutoff, ds, y) rows `cross_validation` would return, built by hand.

    Used only where Prophet is not offered. The construction is
    `cross_validation`'s own: for each cutoff, every OBSERVED month in
    (cutoff, cutoff + CV_HORIZON]. `yhat` is NaN — this frame carries fold
    membership and actuals, nothing else, and each candidate overwrites yhat on
    its own copy exactly as it does with the Prophet-derived frame.
    """
    horizon = pd.Timedelta(CV_HORIZON)
    parts = []
    for c in cutoffs:
        fold = train[(train["ds"] > c) & (train["ds"] <= c + horizon)].copy()
        if fold.empty:
            continue
        fold["cutoff"] = pd.Timestamp(c)
        parts.append(fold[["ds", "cutoff", "y"]])
    if not parts:
        return pd.DataFrame(columns=["ds", "cutoff", "y", "yhat"])
    out = pd.concat(parts, ignore_index=True)
    out["yhat"] = np.nan
    return out


def candidate_cv_frames(train: pd.DataFrame, grid: pd.DataFrame,
                        cutoffs: list[pd.Timestamp], current_method: str,
                        prophet_ok: bool
                        ) -> tuple[dict[str, pd.DataFrame], dict[str, str],
                                   str, int]:
    """
    One `cross_validation`-shaped frame per candidate, on IDENTICAL folds.

    `ets_sarima_experiment.candidate_cv_frames()`'s mechanism, extended with the
    blend and with a Prophet gate. There is ONE set of (cutoff, ds, y) rows and
    every candidate writes its yhat onto a copy of it, so no two candidates can
    end up scored on different months, different fold counts or different
    actuals. A candidate that cannot be computed is recorded in `failures` and is
    ABSENT from that item's tables rather than present with a fabricated number —
    the `fit_failures.csv` pattern this week already uses.

    THE BLEND IS BUILT LAST AND FROM THE ROUTED CANDIDATE'S OWN FRAME. It is
    `0.5 * (routed model's fold yhat) + 0.5 * (12-month trailing mean at that
    cutoff)`, i.e. `ets_sarima_experiment`'s blend definition transplanted from
    the single held-out window onto the fold structure. No `split_ratio` appears
    here and none should: both halves are family-grain in track 1, which is the
    whole reason track 1 is immune to the grain trap track 2 is not.

    For an item already routed to Naive-12mo the blend averages a number with
    itself and returns it unchanged — the identity case
    `ets_sarima_experiment.blend_track()` names. It is computed anyway and
    flagged downstream rather than suppressed, so the blend column is never
    silently absent for a subset of the book.

    Returns (frames, failures, fold_basis, n_fits).
    """
    failures: dict[str, str] = {}
    n_fits = 0
    frames: dict[str, pd.DataFrame] = {}

    if prophet_ok:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                model = Prophet(**config.PROPHET_PARAMS)
                model.fit(train)
                cv = cross_validation(model, horizon=CV_HORIZON, cutoffs=cutoffs,
                                      disable_tqdm=True)
            cv = cv[["ds", "cutoff", "y", "yhat"]].copy()
            frames[CAND_PROPHET] = cv
            fold_basis = FOLD_BASIS_PROPHET
            n_fits = 1 + len(cutoffs)
        except Exception as exc:
            # Prophet failing is not a reason for the item to leave the
            # tournament — the other candidates are still computable and the
            # item still has a production method to defend. Fall back to the
            # manual fold frame and record why Prophet is missing.
            failures[CAND_PROPHET] = f"{type(exc).__name__}: {exc}"
            cv = manual_cv_frame(train, cutoffs)
            fold_basis = FOLD_BASIS_MANUAL
    else:
        failures[CAND_PROPHET] = (
            f"not offered: {len(train)} observed training months, below "
            f"{PROPHET_MIN_MONTHS} (config.MIN_TRAIN_MONTHS — the same gate "
            f"model_routing applies)")
        cv = manual_cv_frame(train, cutoffs)
        fold_basis = FOLD_BASIS_MANUAL

    if cv.empty:
        return {}, failures, fold_basis, n_fits

    base = cv[["ds", "cutoff", "y"]].copy()

    for name in NAIVE_VARIANTS:
        months = config.NAIVE_WINDOW_MONTHS[name]
        levels = {c: naive_level(grid, c, months) for c in base["cutoff"].unique()}
        frame = base.copy()
        frame["yhat"] = frame["cutoff"].map(levels).astype(float)
        frames[name] = frame

    for name in ETS_VARIANTS + [CAND_SARIMA]:
        frame = base.copy()
        yhat = np.full(len(frame), np.nan)
        try:
            for cutoff, idx in frame.groupby("cutoff").groups.items():
                sub = frame.loc[idx]
                yhat[frame.index.get_indexer(idx)] = _statsmodels_yhat(
                    name, grid, pd.Timestamp(cutoff), sub["ds"])
                n_fits += 1
        except Exception as exc:
            failures[name] = f"{type(exc).__name__}: {exc}"
            continue
        if not np.isfinite(yhat).any():
            failures[name] = "fit produced no finite forecast"
            continue
        frame["yhat"] = yhat
        frames[name] = frame

    # ── The blend, from whichever model this item is actually routed to ───────
    routed = frames.get(current_method)
    if routed is None:
        failures[CAND_BLEND] = (
            f"routed model {current_method!r} is not among this item's "
            f"computable candidates, so there is no routed forecast to blend")
    else:
        months12 = config.NAIVE_WINDOW_MONTHS[CAND_NAIVE12]
        levels = {c: naive_level(grid, c, months12)
                  for c in base["cutoff"].unique()}
        frame = base.copy()
        frame["yhat"] = (BLEND_WEIGHT * routed["yhat"].to_numpy(dtype=float)
                         + (1 - BLEND_WEIGHT)
                         * frame["cutoff"].map(levels).astype(float))
        frames[CAND_BLEND] = frame

    return frames, failures, fold_basis, n_fits


def fold_rows(frames: dict[str, pd.DataFrame], item: str, cv_key: str,
              train: pd.DataFrame) -> list[dict]:
    """
    Every (cutoff x candidate) fold for one fit key, scored.

    `ets_sarima_experiment.fold_rows()`'s arithmetic unchanged, including the
    zero-clip (production clips, so scoring an unclipped forecast would score a
    model the pipeline does not ship) and `n_clipped_months` (a fitted trend can
    go negative where a naive level cannot, and both produce a 100% WAPE by
    different mechanisms). Rows whose yhat is NaN drop from that candidate's fold
    only, never from anyone else's.
    """
    rows: list[dict] = []
    for name, cv in frames.items():
        cv = cv.dropna(subset=["yhat"]).copy()
        if cv.empty:
            continue
        n_clip = int((cv["yhat"] < 0).sum())
        cv["yhat"] = cv["yhat"].clip(lower=0)
        cv["abs_err"] = (cv["y"] - cv["yhat"]).abs()

        for cutoff, fold in cv.groupby("cutoff"):
            denom = float(fold["y"].abs().sum())
            nz = fold[fold["y"] != 0]
            rows.append({
                "item_code": item,
                "cv_key": cv_key,
                "candidate": name,
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
                "n_clipped_months": int((fold["yhat"] <= 0).sum()) if n_clip else 0,
            })
    return rows


def item_scores(folds: pd.DataFrame) -> pd.DataFrame:
    """
    Collapse folds to one row per (item, candidate) — the tournament scorecard.

    `median_wape` IS THE SELECTION STATISTIC, and it is a median because the
    brief asks for one. That is a departure from the rest of this week, where
    `changepoint_experiment._fold_scores()` averages WAPE over cutoffs, so both
    are carried and the mean rides along under `mean_wape` rather than silently
    replacing or being replaced. They disagree whenever one fold is far worse
    than the others — exactly the case a median exists to handle and exactly the
    case a per-item selection is most likely to be fooled by. `scorer_delta_pp`
    is the size of the disagreement, so a reader can see which items rest on it.

    `bias_ratio` is the shrinkage statistic at (item, candidate) grain — mean
    forecast level over mean actual monthly rate on the same folds — so the
    per-item winner can be checked for shrinkage individually, not only in the
    population-level table.
    """
    out = []
    for (item, cand), grp in folds.groupby(["item_code", "candidate"]):
        scored = grp.dropna(subset=["wape"])
        pooled_den = float(grp["actual_sum"].sum())
        pooled_err = float(grp["abs_err_sum"].sum(skipna=True))
        rate = float((grp["actual_sum"] / grp["n_horizon_months"]).mean())
        level = float(grp["yhat_level_or_forecast"].mean(skipna=True))

        median_wape = float(scored["wape"].median()) if len(scored) else np.nan
        mean_wape = float(scored["wape"].mean()) if len(scored) else np.nan

        out.append({
            "item_code": item,
            "cv_key": str(grp["cv_key"].iloc[0]),
            "candidate": cand,
            "n_folds": int(len(grp)),
            "n_folds_scored": int(len(scored)),
            "n_folds_zero_demand": int((grp["actual_sum"] == 0).sum()),
            "median_wape": median_wape,
            "mean_wape": mean_wape,
            "scorer_delta_pp": (abs(median_wape - mean_wape)
                                if pd.notna(median_wape) and pd.notna(mean_wape)
                                else np.nan),
            "wape_pooled": ((pooled_err / pooled_den * 100)
                            if pooled_den > 0 else np.nan),
            "median_mape": (float(scored["mape"].median())
                            if scored["mape"].notna().any() else np.nan),
            "mean_yhat_level": level,
            "mean_actual_rate": rate,
            "bias_ratio": (level / rate) if rate else np.nan,
            "n_clipped_months": int(grp["n_clipped_months"].sum()),
        })
    return pd.DataFrame(out)


# ── The tournament verdict, and the two bars it has to clear ──────────────────

def tournament_verdict(scores: pd.DataFrame, population: pd.DataFrame
                       ) -> pd.DataFrame:
    """
    Per item: the raw CV winner, and separately whether a switch is defensible.

    THE TWO COLUMNS ARE NOT COLLAPSED, and this function is where that promise
    is kept:

      `cv_winner`        — the argmin of `median_wape` over every candidate that
                           produced a number. NO bars applied. This is the
                           overfitting-prone quantity and it is reported as such.
      `confident_switch` — True only when ALL of:
                             * the winner is not already the current method,
                             * n_folds_scored >= MIN_FOLDS_FOR_VERDICT (3),
                             * the winner beats the CURRENT METHOD's own median
                               WAPE by more than TIE_TOLERANCE_PP (5.0).

    `cv_winner_margin_pp` is the current method's WAPE minus the winner's — the
    ground a switch would gain. It is deliberately NOT the gap to the runner-up:
    a candidate can win the tournament outright and still be a fraction of a
    point better than what the item already has, and it is the second quantity
    that decides whether a switch is worth making. The runner-up gap is carried
    separately as context.

    An item whose current production method is not among its computable
    candidates cannot be given a confident switch: there is nothing measured to
    beat. `current_method_scorable` records that.
    """
    meta = population.set_index("item_code")
    rows = []
    for item, grp in scores.groupby("item_code"):
        m = meta.loc[item]
        current = str(m["current_method"])
        ranked = grp.dropna(subset=["median_wape"]).sort_values(
            ["median_wape", "candidate"])
        by = grp.set_index("candidate")

        n_scored = int(grp["n_folds_scored"].max()) if len(grp) else 0
        cur_w = (float(by["median_wape"].get(current, np.nan))
                 if current in by.index else np.nan)

        winner = str(ranked["candidate"].iloc[0]) if len(ranked) else ""
        winner_w = float(ranked["median_wape"].iloc[0]) if len(ranked) else np.nan
        runner = str(ranked["candidate"].iloc[1]) if len(ranked) > 1 else ""
        runner_w = (float(ranked["median_wape"].iloc[1])
                    if len(ranked) > 1 else np.nan)

        margin = (cur_w - winner_w
                  if pd.notna(cur_w) and pd.notna(winner_w) else np.nan)

        enough = n_scored >= MIN_FOLDS_FOR_VERDICT
        material = bool(pd.notna(margin) and margin > TIE_TOLERANCE_PP)
        differs = bool(winner) and winner != current
        confident = bool(differs and enough and material)

        if not enough:
            reason = (f"insufficient evidence ({n_scored} scored fold(s), needs "
                      f"{MIN_FOLDS_FOR_VERDICT}) — keeps {current}")
        elif not differs:
            reason = f"CV winner is already the current method ({current})"
        elif not pd.notna(cur_w):
            reason = (f"current method {current} could not be scored — no "
                      f"baseline to beat, keeps {current}")
        elif not material:
            reason = (f"{winner} wins by {margin:+.1f}pp, inside the "
                      f"{TIE_TOLERANCE_PP:.0f}pp tie tolerance — keeps {current}")
        else:
            reason = (f"{winner} beats {current} by {margin:+.1f}pp on "
                      f"{n_scored} folds")

        # The method the tournament actually selects, safeguards applied. Track 2
        # scores this AND the raw winner separately, so the cost of the
        # safeguards is measured rather than argued.
        selected = winner if confident else current

        wb = float(by["bias_ratio"].get(winner, np.nan)) if winner else np.nan
        sb = (float(by["bias_ratio"].get(selected, np.nan))
              if selected in by.index else np.nan)

        rows.append({
            "item_code": item,
            "current_method": current,
            "cv_key": str(m["cv_key"]),
            "cv_key_shared_with": int(m["cv_key_shared_with"]),
            "is_pooled": bool(m["is_pooled"]),
            "bias_cluster": bool(m["bias_cluster"]),
            "n_candidates_scored": int(len(ranked)),
            "n_folds_scored": n_scored,
            "current_method_scorable": bool(pd.notna(cur_w)),
            "current_method_wape": cur_w,
            "cv_winner": winner,
            "cv_winner_wape": winner_w,
            "cv_runner_up": runner,
            "cv_runner_up_wape": runner_w,
            "cv_winner_margin_pp": margin,
            "margin_over_runner_up_pp": (runner_w - winner_w
                                         if pd.notna(runner_w) else np.nan),
            "enough_evidence": enough,
            "material_margin": material,
            "confident_switch": confident,
            "tournament_method": selected,
            "verdict": reason,
            "cv_winner_bias_ratio": wb,
            "tournament_method_bias_ratio": sb,
            "winner_shrinkage_flag": bool(pd.notna(wb) and wb < SHRINKAGE_MATERIAL),
            **{f"wape_{c}": float(by["median_wape"].get(c, np.nan))
               for c in ALL_CANDIDATES},
        })
    return pd.DataFrame(rows).sort_values("item_code").reset_index(drop=True)


def shrinkage_table(folds: pd.DataFrame, label: str = "") -> pd.DataFrame:
    """
    Per candidate, over the whole book: how big is its forecast against demand.

    `erratic_naive_experiment.shrinkage_table()`'s four fingerprints with its
    constants imported, so a column here means what it means there. This is the
    population-level view; the per-item version the brief asks for — the
    shrinkage of whichever method WON that item — is `item_scores()`'s
    `bias_ratio`, surfaced per item by `tournament_verdict()` and aggregated by
    `winner_shrinkage()`.
    """
    f = folds.copy()
    f["actual_rate"] = f["actual_sum"] / f["n_horizon_months"]
    rows = []
    for name in [c for c in ALL_CANDIDATES if c in set(f["candidate"])]:
        sub = f[f["candidate"] == name]
        scored = sub.dropna(subset=["wape"])
        rate = float(sub["actual_rate"].mean())
        level = float(sub["yhat_level_or_forecast"].mean(skipna=True))
        rel = sub[sub["actual_rate"] > 0]
        rows.append({
            "population": label,
            "candidate": name,
            "n_items": int(sub["item_code"].nunique()),
            "n_folds": int(len(sub)),
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
            "pct_folds_clipped": float((sub["n_clipped_months"] > 0).mean() * 100),
        })
    return pd.DataFrame(rows)


def winner_shrinkage(verdict: pd.DataFrame) -> pd.DataFrame:
    """
    The per-item shrinkage check, aggregated by which candidate won the item.

    "Apply to whichever method wins per item" — so this groups items by their OWN
    `cv_winner` and reports the distribution of that winner's own bias ratio on
    that item's own folds. A candidate that wins ten items by forecasting a third
    of the demand on all ten shows up here as a median bias ratio near 0.33 and
    cannot be read as ten real wins.
    """
    rows = []
    for cand, g in verdict[verdict["cv_winner"] != ""].groupby("cv_winner"):
        br = g["cv_winner_bias_ratio"].dropna()
        rows.append({
            "cv_winner": cand,
            "n_items_won": int(len(g)),
            "median_winner_bias_ratio": float(br.median()) if len(br) else np.nan,
            "n_flagged_shrinkage": int(g["winner_shrinkage_flag"].sum()),
            "pct_flagged_shrinkage": float(g["winner_shrinkage_flag"].mean() * 100),
            "n_confident_switch": int(g["confident_switch"].sum()),
        })
    return (pd.DataFrame(rows).sort_values("n_items_won", ascending=False)
            .reset_index(drop=True))


# ── TRACK 2: the held-out window, where BDM lives ─────────────────────────────

def window_rows(item: str, grid: pd.DataFrame, train: pd.DataFrame,
                win: pd.DataFrame, split_ratio: float, current_method: str,
                prophet_ok: bool) -> tuple[list[dict], dict[str, str], int]:
    """
    Every candidate's held-out-window forecast for one item, plus BDM's.

    One fit per candidate on history <= TRAIN_END, forecast across the window,
    scored on the months BDM also covers — restricting to BDM-covered months is
    what makes this a comparison rather than two different exercises.

    THREE THINGS ARE NOT SYMMETRIC HERE AND ALL THREE ARE DELIBERATE:

    1. `CAND_ROUTED` IS READ, NOT REFIT. It is `model_forecast` from
       `benchmark_comparison.csv`, i.e. what the pipeline actually delivered for
       these months. "Does the tournament beat current production" has to mean
       beating the delivered forecast, not a fresh fit of the same model class
       that might land somewhere else. It also means the current method carries
       production's segmented `changepoint_prior_scale` while the refit Prophet
       candidate carries the config default — a real difference, named here so it
       is not later mistaken for a modelling result.

    2. EVERY REFIT CANDIDATE IS MULTIPLIED BY `split_ratio`. See the module
       docstring's grain note. `grid` is family-grain; the window's actuals are
       item-grain. Singletons carry 1.0, so this is a no-op for most of the book.

    3. THE BLEND USES THE SHIPPED ROUTED FORECAST, not a refit one, so
       `CAND_BLEND` here is the same object `ets_sarima_experiment.blend_track()`
       scores and the two files' blend numbers are comparable.
    """
    failures: dict[str, str] = {}
    win = win[win["bdm_forecast"].notna()].sort_values("ds")
    if win.empty:
        return [], {"__window__": "no BDM-covered month"}, 0

    target = win["ds"]
    preds: dict[str, np.ndarray] = {}
    n_fits = 0

    if prophet_ok:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                model = Prophet(**config.PROPHET_PARAMS)
                model.fit(train)
                fc = model.predict(pd.DataFrame({"ds": target}))
                preds[CAND_PROPHET] = fc["yhat"].to_numpy(dtype=float) * split_ratio
                n_fits += 1
            except Exception as exc:
                failures[CAND_PROPHET] = f"{type(exc).__name__}: {exc}"
    else:
        failures[CAND_PROPHET] = (
            f"not offered: {len(train)} observed training months, below "
            f"{PROPHET_MIN_MONTHS}")

    naive12 = naive_level(grid, TRAIN_END_TS,
                          config.NAIVE_WINDOW_MONTHS[CAND_NAIVE12]) * split_ratio
    for name in NAIVE_VARIANTS:
        level = naive_level(grid, TRAIN_END_TS,
                            config.NAIVE_WINDOW_MONTHS[name]) * split_ratio
        preds[name] = np.full(len(win), float(level))

    for name in ETS_VARIANTS + [CAND_SARIMA]:
        try:
            preds[name] = _statsmodels_yhat(name, grid, TRAIN_END_TS,
                                            target) * split_ratio
            n_fits += 1
        except Exception as exc:
            failures[name] = f"{type(exc).__name__}: {exc}"

    routed = win["model_forecast"].to_numpy(dtype=float)
    if pd.notna(routed).any():
        preds[CAND_ROUTED] = routed
        preds[CAND_BLEND] = (BLEND_WEIGHT * np.clip(routed, 0, None)
                             + (1 - BLEND_WEIGHT) * naive12)
    else:
        failures[CAND_ROUTED] = "no model_forecast in the benchmark window"
        failures[CAND_BLEND] = "no routed forecast to blend"

    rows = []
    for name, yhat in preds.items():
        for ds, month, actual, bdm, y in zip(
                win["ds"], win["month"], win["actual"], win["bdm_forecast"], yhat):
            rows.append({
                "item_code": item, "candidate": name, "ds": ds, "month": month,
                "actual": float(actual),
                "yhat_raw": float(y) if pd.notna(y) else np.nan,
                "yhat": float(max(y, 0.0)) if pd.notna(y) else np.nan,
                "bdm_forecast": float(bdm), "routed_model": current_method,
                "split_ratio": float(split_ratio),
            })
    for ds, month, actual, bdm in zip(win["ds"], win["month"], win["actual"],
                                      win["bdm_forecast"]):
        rows.append({
            "item_code": item, "candidate": BDM, "ds": ds, "month": month,
            "actual": float(actual), "yhat_raw": float(bdm),
            "yhat": float(max(bdm, 0.0)), "bdm_forecast": float(bdm),
            "routed_model": current_method, "split_ratio": float(split_ratio),
        })
    return rows, failures, n_fits


def window_scores(rows: pd.DataFrame) -> pd.DataFrame:
    """
    One WAPE per (item, candidate) over the single held-out window.

    A single six-month fold, and named as one: `n_months` is on every row so a
    reader can see this is one window and not a fold structure.
    """
    out = []
    for (item, cand), g in rows.dropna(subset=["yhat"]).groupby(
            ["item_code", "candidate"]):
        err = (g["actual"] - g["yhat"]).abs()
        den = float(g["actual"].abs().sum())
        nz = g[g["actual"] != 0]
        out.append({
            "item_code": item,
            "candidate": cand,
            "n_months": int(len(g)),
            "actual_sum": den,
            "yhat_sum": float(g["yhat"].sum()),
            "wape": float(err.sum() / den * 100) if den > 0 else np.nan,
            "mape": (float(((nz["actual"] - nz["yhat"]).abs()
                            / nz["actual"].abs()).mean() * 100)
                     if len(nz) else np.nan),
            "bias_ratio": (float(g["yhat"].mean() / g["actual"].mean())
                           if g["actual"].mean() else np.nan),
        })
    return pd.DataFrame(out)


#: Tolerance for the reconstruction self-check, in FORECAST UNITS per month.
#:
#: 0.01, because `test_validation.csv` and `benchmark_comparison.csv` store
#: `yhat` rounded to two decimals: a rebuilt level of 3.3333 against a shipped
#: 3.33 is the storage format, not a disagreement, and any real grain error is
#: orders of magnitude larger (the U35309 case the blend track caught was a
#: factor of 8.6).
#:
#: THE CHECK IS ON UNITS AND NOT ON WAPE POINTS, and that is the whole reason
#: this constant exists. WAPE divides by the window's actual demand, so on an
#: item selling ONE unit in six months a 0.003-unit rounding difference is 1.8
#: WAPE points — a number that looks alarming and means nothing. Judging the
#: reconstruction on the quantity that was actually reconstructed removes the
#: denominator from the question entirely.
IDENTITY_TOLERANCE_UNITS = 0.01


def window_identity_check(wrows: pd.DataFrame, verdict: pd.DataFrame,
                          population: pd.DataFrame) -> pd.DataFrame:
    """
    Self-test on the split_ratio reconstruction, not a result.

    For an item routed to a naive window, the refit candidate of that same name
    is rebuilt here from `pooled.data` and multiplied by `split_ratio`, while
    `CAND_ROUTED` is read from `benchmark_comparison.csv`. They are two paths to
    the same number and must agree. Disagreement means the grain correction is
    wrong — which is exactly the failure `ets_sarima_experiment.blend_track()`'s
    identity check caught before, and the reason this one exists rather than a
    comment asserting the multiplication is right.

    `is_pooled` is on every row because it is the column that says whether the
    check EXERCISED the split_ratio at all: a singleton carries a ratio of 1.0,
    so its agreement tests the naive arithmetic and nothing else. Only the pooled
    rows test the grain correction, and the caller reports how many there are
    rather than letting a pass on 28 singletons stand in for a pass on the thing
    the check is named after.

    Prophet-routed items are NOT checkable this way: `main()` fits Prophet with
    the segmented `changepoint_prior_scale` and this file refits at the config
    default, so the two are genuinely different models and a difference there is
    information rather than an error.
    """
    cur = verdict.set_index("item_code")["current_method"]
    pooled = population.set_index("item_code")["is_pooled"]
    ratio = population.set_index("item_code")["split_ratio"]
    rows = []
    for item, g in wrows.groupby("item_code"):
        method = str(cur.get(item, ""))
        if method not in NAIVE_VARIANTS:
            continue
        ship = g[g["candidate"] == CAND_ROUTED].set_index("month")["yhat"]
        built = g[g["candidate"] == method].set_index("month")["yhat"]
        common = ship.index.intersection(built.index)
        if not len(common):
            continue
        diff = (built[common] - ship[common]).abs()
        den = float(g[g["candidate"] == BDM]["actual"].abs().sum())
        err_s = float((g[g["candidate"] == CAND_ROUTED]["actual"]
                       - ship.to_numpy()).__abs__().sum())
        err_b = float((g[g["candidate"] == method]["actual"]
                       - built.to_numpy()).__abs__().sum())
        rows.append({
            "item_code": item,
            "routed_model": method,
            "is_pooled": bool(pooled.get(item, False)),
            "split_ratio": float(ratio.get(item, np.nan)),
            "max_abs_yhat_diff_units": float(diff.max()),
            "window_actual_sum": den,
            "wape_shipped": (err_s / den * 100) if den > 0 else np.nan,
            "wape_rebuilt": (err_b / den * 100) if den > 0 else np.nan,
            "abs_diff_pp": (abs(err_b - err_s) / den * 100) if den > 0 else np.nan,
        })
    return (pd.DataFrame(rows)
            .sort_values("max_abs_yhat_diff_units", ascending=False)
            .reset_index(drop=True))


def split_ratio_check(population: pd.DataFrame) -> pd.DataFrame:
    """
    Do a pooled family's split ratios sum to one?

    The second half of the grain proof, and the half the identity check cannot
    supply. `window_identity_check()` can only test items routed to a naive
    window, which may all be singletons; this tests the split itself on every
    pooled family in the book, whatever it is routed to. If the ratios sum to
    one, the item-grain forecasts this file multiplies out reconstitute the
    family total rather than inflating or deflating it.

    Returns the OFFENDING families — empty is the pass.
    """
    g = population.groupby("family_key")["split_ratio"].agg(["sum", "size"])
    bad = g[(g["size"] > 1) & ((g["sum"] - 1.0).abs() > 1e-6)]
    return bad.reset_index().rename(columns={"sum": "split_ratio_sum",
                                             "size": "n_items"})


def build_summary(verdict: pd.DataFrame, wscores: pd.DataFrame) -> pd.DataFrame:
    """
    One row per item — the headline table, and the brief's schema.

    `bdm_wape_current_method` is the SHIPPED forecast's WAPE (`CAND_ROUTED`).
    `bdm_wape_tournament_method` is the tournament's selection AFTER both
    safeguards, so it is identical to the current method's number wherever there
    is no confident switch — the honest consequence of a bar most items do not
    clear, not a bug. `bdm_wape_cv_winner` is the same measurement for the RAW
    winner with no bars, carried so the cost of the safeguards is measurable
    rather than asserted: it says what would have happened had the tournament
    been trusted naively.

    `current_beats_bdm` / `tournament_beats_bdm` use TIE_TOLERANCE_PP, the same
    materiality bar as the selection. A forecast half a point better than BDM has
    not beaten BDM.
    """
    ws = {(i, c): w for i, c, w in
          zip(wscores["item_code"], wscores["candidate"], wscores["wape"])}
    act = (wscores.groupby("item_code")["actual_sum"].max()
           if len(wscores) else pd.Series(dtype=float))

    rows = []
    for r in verdict.itertuples(index=False):
        item = r.item_code
        cur_bdm = ws.get((item, CAND_ROUTED), np.nan)
        bdm_w = ws.get((item, BDM), np.nan)
        tour_bdm = (cur_bdm if r.tournament_method == r.current_method
                    else ws.get((item, r.tournament_method), np.nan))
        win_bdm = (cur_bdm if r.cv_winner == r.current_method
                   else ws.get((item, r.cv_winner), np.nan))
        a = float(act.get(item, np.nan))

        rows.append({
            # ── The brief's schema ────────────────────────────────────────────
            "item_code": item,
            "current_method": r.current_method,
            "cv_winner": r.cv_winner,
            "cv_winner_margin_pp": r.cv_winner_margin_pp,
            "confident_switch": r.confident_switch,
            "n_folds_scored": r.n_folds_scored,
            "bdm_wape_current_method": cur_bdm,
            "bdm_wape_tournament_method": tour_bdm,
            "current_beats_bdm": bool(pd.notna(cur_bdm) and pd.notna(bdm_w)
                                      and cur_bdm < bdm_w - TIE_TOLERANCE_PP),
            "tournament_beats_bdm": bool(pd.notna(tour_bdm) and pd.notna(bdm_w)
                                         and tour_bdm < bdm_w - TIE_TOLERANCE_PP),
            # ── Context for the ten columns above ─────────────────────────────
            "tournament_method": r.tournament_method,
            "enough_evidence": r.enough_evidence,
            "material_margin": r.material_margin,
            "verdict": r.verdict,
            "cv_key": r.cv_key,
            "cv_key_shared_with": r.cv_key_shared_with,
            "bias_cluster": r.bias_cluster,
            "winner_shrinkage_flag": r.winner_shrinkage_flag,
            "cv_winner_bias_ratio": r.cv_winner_bias_ratio,
            "current_method_wape": r.current_method_wape,
            "cv_winner_wape": r.cv_winner_wape,
            "bdm_wape": bdm_w,
            "bdm_wape_cv_winner": win_bdm,
            "window_actual_sum": a,
            "window_note": ("zero demand in the whole window — WAPE undefined "
                            "for every candidate" if a == 0 else ""),
            "tournament_minus_current_pp": (tour_bdm - cur_bdm
                                            if pd.notna(tour_bdm)
                                            and pd.notna(cur_bdm) else np.nan),
            "current_gap_to_bdm_pp": (cur_bdm - bdm_w
                                      if pd.notna(cur_bdm) and pd.notna(bdm_w)
                                      else np.nan),
            "tournament_gap_to_bdm_pp": (tour_bdm - bdm_w
                                         if pd.notna(tour_bdm) and pd.notna(bdm_w)
                                         else np.nan),
        })
    return pd.DataFrame(rows).sort_values("item_code").reset_index(drop=True)


# ── Orchestration ─────────────────────────────────────────────────────────────

def run_tournament(population: pd.DataFrame, pooled_data: pd.DataFrame,
                   window_all: pd.DataFrame, cv_only: bool
                   ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame,
                              pd.DataFrame, float, int]:
    """
    Fit and score every candidate on every item.
    Returns (folds, window_rows, skipped, failures, elapsed, n_fits).

    THE CV WORK IS CACHED BY (FIT KEY, ROUTED MODEL). Two items in the same
    successor family are the same series to every candidate in track 1, so
    refitting per item would burn seven fits to reproduce a number already
    computed and would not make the two items' selections any more independent
    than they already are. The routed model is part of the key because the BLEND
    candidate depends on it — two items in one family routed differently get
    genuinely different blends and must not share a cache entry.

    A CACHE HIT REPLAYS THE FAILURE ROWS TOO, not only the fold rows. A candidate
    that could not be fitted on a family failed for every item in that family,
    and logging it against only the first one would make `fit_failures.csv`
    undercount exactly on the pooled items — the population where the shared fit
    makes the failure most consequential.

    Track 2 is NOT cached: its `split_ratio` and its shipped routed forecast are
    per item even where the underlying fit is not.

    An item that cannot produce a fold is recorded in `skipped` with its reason
    and never dropped quietly; a candidate that fails on an item is recorded in
    `failures`, per item and per candidate.
    """
    df = pooled_data.copy()
    df["item_code"] = df["item_code"].astype(str).str.strip()
    df["ds"] = pd.to_datetime(df["ds"])
    by_key = {str(k): g for k, g in df.groupby("item_code")}

    all_folds: list[dict] = []
    all_window: list[dict] = []
    skipped: list[dict] = []
    fails: list[dict] = []
    cache: dict[tuple[str, str], tuple[list[dict], dict[str, str]]] = {}
    n_fits = 0
    t0 = time.perf_counter()

    for n, row in enumerate(population.itertuples(index=False), start=1):
        item = str(row.item_code).strip()
        key = str(row.family_key).strip()
        current = str(row.current_method)
        series = by_key.get(key)
        if series is None or series.empty:
            skipped.append({"item_code": item, "cv_key": key, "track": "both",
                            "reason": "no rows in the prepared frame"})
            continue

        train = (series[(series["ds"] >= TRAIN_START_TS)
                        & (series["ds"] <= TRAIN_END_TS)]
                 [["ds", "y"]].dropna().sort_values("ds").reset_index(drop=True))
        grid = monthly_grid(train, TRAIN_END_TS)
        prophet_ok = len(train) >= PROPHET_MIN_MONTHS

        # ── Track 1 ──────────────────────────────────────────────────────────
        cache_key = (key, current)
        if cache_key in cache:
            cached_rows, cached_fails = cache[cache_key]
            all_folds.extend({**r, "item_code": item} for r in cached_rows)
            for cand, why in cached_fails.items():
                fails.append({"item_code": item, "cv_key": key, "track": "cv",
                              "candidate": cand, "reason": why})
        elif len(train) < MIN_CV_TRAIN_MONTHS:
            skipped.append({
                "item_code": item, "cv_key": key, "track": "cv",
                "reason": (f"{len(train)} observed training months, below "
                           f"MIN_CV_TRAIN_MONTHS ({MIN_CV_TRAIN_MONTHS})")})
        else:
            cutoffs = make_cutoffs(train)
            if not cutoffs:
                skipped.append({
                    "item_code": item, "cv_key": key, "track": "cv",
                    "reason": (f"no valid CV fold: {len(train)} observed months, "
                               f"needs {MIN_CV_TRAIN_MONTHS} before a cutoff "
                               f"with demand in the {CV_HORIZON} after it")})
            else:
                frames, cand_fails, basis, fits = candidate_cv_frames(
                    train, grid, cutoffs, current, prophet_ok)
                n_fits += fits
                rows = fold_rows(frames, item, key, train)
                for r in rows:
                    r["fold_basis"] = basis
                cache[cache_key] = (rows, cand_fails)
                all_folds.extend(rows)
                for cand, why in cand_fails.items():
                    fails.append({"item_code": item, "cv_key": key, "track": "cv",
                                  "candidate": cand, "reason": why})

        # ── Track 2 ──────────────────────────────────────────────────────────
        if not cv_only:
            win = (window_all[window_all["item_code"] == item]
                   if len(window_all) else pd.DataFrame())
            if len(win):
                wrows, wfails, wfits = window_rows(
                    item, grid, train, win, float(row.split_ratio), current,
                    prophet_ok)
                all_window.extend(wrows)
                n_fits += wfits
                for cand, why in wfails.items():
                    fails.append({"item_code": item, "cv_key": key,
                                  "track": "bdm_window", "candidate": cand,
                                  "reason": why})
            else:
                fails.append({"item_code": item, "cv_key": key,
                              "track": "bdm_window", "candidate": "__all__",
                              "reason": "item absent from benchmark_comparison.csv"})

        if n % 10 == 0 or n == len(population):
            print(f"  {n:>3}/{len(population)}  {item:<10} [{current}] "
                  f"{len(train)} train months   "
                  f"({(time.perf_counter() - t0) / 60:.1f} min elapsed)")

    return (pd.DataFrame(all_folds), pd.DataFrame(all_window),
            pd.DataFrame(skipped), pd.DataFrame(fails),
            time.perf_counter() - t0, n_fits)


# ── Reporting ─────────────────────────────────────────────────────────────────

SEP = "─" * 78


def print_design(population: pd.DataFrame) -> None:
    """The population and the rules, stated before any number."""
    print(f"\n{SEP}\nPOPULATION AND RULES — stated before any fit\n{SEP}")
    print(f"  Items in the current book (test_validation.csv) : "
          f"{len(population)}")
    print(f"  Distinct fit keys (pooled families share one)   : "
          f"{population['cv_key'].nunique()}")
    print(f"  Items sharing a fit key with another item       : "
          f"{int((population['cv_key_shared_with'] > 0).sum())}")
    print(f"  Severe-bias-cluster items (carried, flagged)    : "
          f"{int(population['bias_cluster'].sum())}")
    print(f"\n  Current production routing:")
    for m, c in population["current_method"].value_counts().items():
        print(f"    {m:<14}: {c:>4}")

    print(f"\n  CANDIDATES OFFERED TO EVERY ITEM:")
    print(f"    {', '.join(NAIVE_VARIANTS)}          — always computable, no gate")
    print(f"    {CAND_PROPHET:<24}— only where observed training months >= "
          f"{PROPHET_MIN_MONTHS} (config.MIN_TRAIN_MONTHS)")
    print(f"    {', '.join(ETS_VARIANTS):<24}— additive trend, no seasonal term; "
          f"both scored, neither picked between")
    print(f"    {CAND_SARIMA:<24}— order {SARIMA_ORDER} x "
          f"{SARIMA_SEASONAL_ORDER}, NO cycle pre-screen")
    print(f"    {CAND_BLEND:<24}— {BLEND_WEIGHT:.0%} routed forecast + "
          f"{1 - BLEND_WEIGHT:.0%} own 12-month trailing mean")

    print(f"\n  SCORING: identical rolling-origin folds per item — horizon "
          f"{CV_HORIZON} ({CV_HORIZON_MONTHS}")
    print(f"  monthly points), {CV_PERIOD_MONTHS} months between cutoffs, at "
          f"most {MAX_CUTOFFS} folds, and at least")
    print(f"  {MIN_CV_TRAIN_MONTHS} observed months before any cutoff. WAPE "
          f"primary, MAPE secondary; the")
    print(f"  selection statistic is the MEDIAN fold WAPE per candidate per "
          f"item.")

    print(f"\n  THE TWO SAFEGUARDS, BOTH IMPORTED:")
    print(f"    1. EVIDENCE  — {MIN_FOLDS_FOR_VERDICT}+ scored folds before any "
          f"switch is considered")
    print(f"                   (erratic_naive_experiment.MIN_FOLDS_FOR_VERDICT)")
    print(f"    2. MATERIALITY — the winner must beat the CURRENT method by "
          f">{TIE_TOLERANCE_PP:.0f}pp")
    print(f"                   (erratic_naive_experiment.TIE_TOLERANCE_PP, the "
          f"same bar the blend")
    print(f"                    and SARIMA track 2 were judged on this week)")
    print(f"    cv_winner (no bars) and confident_switch (both bars) are "
          f"reported as SEPARATE")
    print(f"    columns and are never collapsed into one verdict.")


def print_track1(population: pd.DataFrame, folds: pd.DataFrame,
                 scores: pd.DataFrame, verdict: pd.DataFrame,
                 skipped: pd.DataFrame, failures: pd.DataFrame,
                 shrink: pd.DataFrame, wshrink: pd.DataFrame) -> None:
    """Track 1: the tournament itself."""
    print(f"\n{SEP}\nTRACK 1 — CROSS-VALIDATION TOURNAMENT (folds inside the "
          f"training window)\n{SEP}")
    print(f"  Items with at least one scored fold : "
          f"{folds['item_code'].nunique()} of {len(population)}")
    if len(folds) and "fold_basis" in folds:
        for basis, n in folds.drop_duplicates("item_code")["fold_basis"] \
                .value_counts().items():
            print(f"    fold membership from {basis:<28}: {n:>4} item(s)")
    if len(skipped):
        print(f"\n  Skipped ({len(skipped)}) — reasons, not silence:")
        print(skipped.to_string(index=False))
    if len(failures):
        print(f"\n  Candidate failures / not-offered "
              f"({len(failures)} rows, {failures['item_code'].nunique()} items):")
        print(failures.groupby(["track", "candidate"]).size()
              .rename("n_items").reset_index().to_string(index=False))

    print(f"\n  Median CV WAPE by candidate, across every item it was "
          f"computable on:")
    for c in ALL_CANDIDATES:
        sub = scores[scores["candidate"] == c]["median_wape"].dropna()
        if len(sub):
            print(f"    {c:<14}: {sub.median():>8.2f}   (n={len(sub)} items)")

    print(f"\n  RAW CV WINNER (no bars applied) — how often each candidate wins:")
    for c, n in verdict["cv_winner"].value_counts().items():
        print(f"    {c:<14}: {n:>4} item(s)  "
              f"({n / len(verdict) * 100:>5.1f}%)")

    print(f"\n  CONFIDENT SWITCHES (both bars cleared) by candidate:")
    cs = verdict[verdict["confident_switch"]]
    if cs.empty:
        print(f"    none")
    else:
        for c, n in cs["cv_winner"].value_counts().items():
            print(f"    {c:<14}: {n:>4} item(s)")

    print(f"\n  Why items did NOT switch:")
    keep = verdict[~verdict["confident_switch"]]
    reasons = {
        "insufficient evidence (<3 scored folds)":
            int((~keep["enough_evidence"]).sum()),
        "CV winner is already the current method":
            int((keep["enough_evidence"]
                 & (keep["cv_winner"] == keep["current_method"])).sum()),
        f"winner leads by <={TIE_TOLERANCE_PP:.0f}pp (tie tolerance)":
            int((keep["enough_evidence"]
                 & (keep["cv_winner"] != keep["current_method"])
                 & ~keep["material_margin"]).sum()),
        "current method could not be scored":
            int((~keep["current_method_scorable"]).sum()),
    }
    for label, n in reasons.items():
        print(f"    {label:<48}: {n:>4}")

    print(f"\n{SEP}\nSHRINKAGE / BIAS CHECK\n{SEP}")
    print(f"  Per candidate, over every fold it produced:")
    print(shrink.round(3).to_string(index=False))
    print()
    for r in shrink.itertuples():
        print(f"  {r.candidate:<14}: {shrinkage_verdict(r.bias_ratio)}")
        if pd.notna(r.pct_folds_clipped) and r.pct_folds_clipped > 0:
            print(f"    {r.pct_folds_clipped:.0f}% of its folds contain a month "
                  f"the fitted trend drove NEGATIVE and production clips to zero.")

    print(f"\n  PER-ITEM WINNERS — is the winner winning by predicting small?")
    print(f"  (bias ratio of the winning candidate on that item's own folds; "
          f"flagged below {SHRINKAGE_MATERIAL})")
    print(wshrink.round(3).to_string(index=False))
    flagged = int(verdict["winner_shrinkage_flag"].sum())
    print(f"\n  Items whose RAW CV winner carries a shrinkage flag : "
          f"{flagged} of {len(verdict)}")
    cs_flag = int(cs["winner_shrinkage_flag"].sum()) if len(cs) else 0
    print(f"  CONFIDENT SWITCHES carrying a shrinkage flag       : "
          f"{cs_flag} of {len(cs)}")
    print(f"  These are FLAGGED, not removed — a switch to a shrunk forecast is "
          f"still counted in")
    print(f"  every tally above and has to be read with this column beside it.")


def print_track2(summary: pd.DataFrame, wscores: pd.DataFrame,
                 icheck: pd.DataFrame, srcheck: pd.DataFrame) -> None:
    """Track 2: the held-out window, against BDM. The actual answer."""
    print(f"\n{SEP}\nTRACK 2 — HELD-OUT WINDOW {config.BENCHMARK_START}.."
          f"{config.BENCHMARK_END}, AGAINST BDM\n{SEP}")
    if summary.empty or summary["bdm_wape"].notna().sum() == 0:
        print("  Nothing scorable.")
        return

    print(f"  ONE six-month window per item, not a fold structure. Current "
          f"production is the")
    print(f"  forecast the pipeline SHIPPED; the tournament method is refit on "
          f"history <= TRAIN_END")
    print(f"  and multiplied by split_ratio; BDM is read from "
          f"benchmark_comparison.csv. All three")
    print(f"  are scored on identical item-months.")

    print(f"\n  GRAIN / split_ratio SELF-CHECK — two independent tests, "
          f"neither a result:")
    print(f"    (a) do a pooled family's split ratios sum to 1? "
          f"{'PASS' if srcheck.empty else 'FAIL'} "
          f"— {len(srcheck)} offending family(ies)")
    if not srcheck.empty:
        print(srcheck.round(6).to_string(index=False))
    if len(icheck):
        n_pooled = int(icheck["is_pooled"].sum())
        worst = float(icheck["max_abs_yhat_diff_units"].max())
        print(f"    (b) does the rebuilt naive forecast land on the SHIPPED "
              f"one? {len(icheck)} naive-routed")
        print(f"        items are rebuildable exactly, {n_pooled} of them "
              f"pooled — and it is those {n_pooled}")
        print(f"        that actually exercise the split_ratio; a singleton "
              f"carries a ratio of 1.0.")
        print(f"        Max |rebuilt - shipped| = {worst:.4f} forecast units "
              f"per month.")
        if worst > IDENTITY_TOLERANCE_UNITS:
            print(f"        WARNING: above {IDENTITY_TOLERANCE_UNITS} units. "
                  f"The grain correction disagrees with the")
            print(f"        shipped forecast; read "
                  f"tournament_bdm_window_results.csv before trusting the rest.")
        else:
            print(f"        That is inside "
                  f"{IDENTITY_TOLERANCE_UNITS} units, i.e. the two-decimal "
                  f"rounding the shipped")
            print(f"        forecast is stored at. The reconstruction agrees.")
        worst_pp = float(icheck["abs_diff_pp"].max())
        if worst_pp > 1.0:
            top = icheck.sort_values("abs_diff_pp", ascending=False).iloc[0]
            print(f"        In WAPE POINTS the same rounding reads as up to "
                  f"{worst_pp:.2f}pp ({top['item_code']}, which sold")
            print(f"        {top['window_actual_sum']:.0f} unit(s) in the whole "
                  f"window). That is the denominator talking, not")
            print(f"        the reconstruction — which is why (b) is judged in "
                  f"units and not in points.")

    scored = summary[summary["bdm_wape"].notna()
                     & summary["bdm_wape_current_method"].notna()]
    print(f"\n  Items scorable against BDM: {len(scored)} of {len(summary)}")
    print(f"\n  MEDIAN WAPE over those items — one item, one vote:")
    print(f"    Current production routing : "
          f"{scored['bdm_wape_current_method'].median():>8.2f}")
    print(f"    Tournament selection       : "
          f"{scored['bdm_wape_tournament_method'].median():>8.2f}")
    print(f"    Raw CV winner (no bars)    : "
          f"{scored['bdm_wape_cv_winner'].median():>8.2f}")
    print(f"    BDM                        : "
          f"{scored['bdm_wape'].median():>8.2f}")

    print(f"\n  MEDIAN GAP TO BDM (negative = better than BDM):")
    print(f"    Current production routing : "
          f"{scored['current_gap_to_bdm_pp'].median():>+8.2f} pp")
    print(f"    Tournament selection       : "
          f"{scored['tournament_gap_to_bdm_pp'].median():>+8.2f} pp")

    print(f"\n  Items where current production beats BDM by "
          f">{TIE_TOLERANCE_PP:.0f}pp : "
          f"{int(scored['current_beats_bdm'].sum())} of {len(scored)}")
    print(f"  Items where the tournament beats BDM by "
          f">{TIE_TOLERANCE_PP:.0f}pp      : "
          f"{int(scored['tournament_beats_bdm'].sum())} of {len(scored)}")

    sw = scored[scored["confident_switch"]]
    print(f"\n  THE SWITCHED ITEMS ONLY ({len(sw)}) — the only rows where the "
          f"two columns can differ:")
    if sw.empty:
        print(f"    No confident switch cleared both bars, so the tournament "
              f"selection IS current")
        print(f"    production on every item and the two BDM columns are equal "
              f"by construction.")
    else:
        cols = ["item_code", "current_method", "cv_winner",
                "cv_winner_margin_pp", "n_folds_scored",
                "bdm_wape_current_method", "bdm_wape_tournament_method",
                "tournament_minus_current_pp", "bdm_wape",
                "winner_shrinkage_flag"]
        print(sw[cols].round(2).to_string(index=False))
        better = int((sw["tournament_minus_current_pp"] < 0).sum())
        mat = int((sw["tournament_minus_current_pp"] < -TIE_TOLERANCE_PP).sum())
        print(f"\n    Switches that improved the held-out window at all      : "
              f"{better} of {len(sw)}")
        print(f"    Switches that improved it by >{TIE_TOLERANCE_PP:.0f}pp"
              f"                     : {mat} of {len(sw)}")
        print(f"    Median held-out change from switching                 : "
              f"{sw['tournament_minus_current_pp'].median():+.2f} pp")


def print_answer(verdict: pd.DataFrame, summary: pd.DataFrame) -> None:
    """The three questions the brief ends on, answered in order."""
    print(f"\n{SEP}\nTHE ANSWER\n{SEP}")

    n = len(verdict)
    enough = verdict[verdict["enough_evidence"]]
    switched = verdict[verdict["confident_switch"]]
    print(f"  1. HOW MANY ITEMS HAD ENOUGH EVIDENCE TO BE CONSIDERED AT ALL?")
    print(f"     {len(enough)} of {n} items reached "
          f"{MIN_FOLDS_FOR_VERDICT}+ scored CV folds.")
    print(f"     {n - len(enough)} did not and keep their current method by "
          f"construction, labelled")
    print(f"     'insufficient evidence' rather than given a winner drawn from "
          f"noise.")
    n_keys = int(enough["cv_key"].nunique())
    if n_keys < len(enough):
        print(f"     Those {len(enough)} items sit on {n_keys} distinct fit "
              f"keys — pooled families share one")
        print(f"     series and therefore one tournament, so this is {n_keys} "
              f"independent selections,")
        print(f"     not {len(enough)}.")
    else:
        print(f"     Each of those {len(enough)} items has its own fit key, so "
              f"each selection is independent.")

    print(f"\n  2. OF THOSE, HOW MANY SWITCHED vs STAYED?")
    print(f"     Confident switch (beats current by >{TIE_TOLERANCE_PP:.0f}pp) : "
          f"{len(switched)} of {len(enough)}")
    print(f"     Stayed on current method                  : "
          f"{len(enough) - len(switched)} of {len(enough)}")
    raw_diff = int((enough["cv_winner"] != enough["current_method"]).sum())
    print(f"     For contrast, the RAW CV winner differed from the current "
          f"method on {raw_diff}")
    print(f"     of those {len(enough)} items.")
    if raw_diff > len(switched):
        print(f"     The {raw_diff - len(switched)} item(s) between {raw_diff} "
              f"and {len(switched)} are what the materiality bar removed —")
        print(f"     candidates that won the tournament nominally and could not "
              f"clear "
              f"{TIE_TOLERANCE_PP:.0f} points")
        print(f"     against what the item already has.")
    else:
        print(f"     Every nominal winner also cleared the "
              f"{TIE_TOLERANCE_PP:.0f}pp bar, so the materiality bar "
              f"removed nothing here.")

    scored = summary[summary["bdm_wape"].notna()
                     & summary["bdm_wape_current_method"].notna()]
    print(f"\n  3. DOES THE TOURNAMENT CLOSE MORE GROUND AGAINST BDM THAN "
          f"CURRENT ROUTING?")
    if scored.empty:
        print(f"     Not answerable — no item is scorable against BDM.")
        return
    cur = float(scored["bdm_wape_current_method"].median())
    tour = float(scored["bdm_wape_tournament_method"].median())
    bdm = float(scored["bdm_wape"].median())
    delta = tour - cur
    print(f"     Median held-out WAPE over {len(scored)} items — current "
          f"{cur:.2f}, tournament {tour:.2f},")
    print(f"     BDM {bdm:.2f}. The tournament moves the median by "
          f"{delta:+.2f} pp.")
    if abs(delta) <= TIE_TOLERANCE_PP:
        print(f"     That is inside the {TIE_TOLERANCE_PP:.0f}pp materiality "
              f"bar this file judges everything else by,")
        print(f"     so on the held-out window the per-item tournament does NOT "
              f"close more ground")
        print(f"     against BDM than the fixed routing rule does. The original "
              f"hypothesis — that")
        print(f"     letting each product pick its own tool beats a fixed rule — "
              f"is not supported")
        print(f"     by this test.")
    elif delta < 0:
        print(f"     That clears the {TIE_TOLERANCE_PP:.0f}pp bar in the "
              f"tournament's favour: per-item selection")
        print(f"     closes more ground against BDM than the fixed rule does, on "
              f"this one window.")
    else:
        print(f"     That clears the {TIE_TOLERANCE_PP:.0f}pp bar AGAINST the "
              f"tournament: per-item selection is")
        print(f"     WORSE on the held-out window than the fixed routing rule.")
    print(f"     Items beating BDM by >{TIE_TOLERANCE_PP:.0f}pp — current "
          f"{int(scored['current_beats_bdm'].sum())}, "
          f"tournament {int(scored['tournament_beats_bdm'].sum())}, "
          f"of {len(scored)}.")


def print_caveats(population: pd.DataFrame, verdict: pd.DataFrame) -> None:
    """What this run does NOT say."""
    print(f"\n{SEP}\nWHAT THIS DOES NOT SAY\n{SEP}")
    print(f"  NOTHING IS ADOPTED. config.py is unchanged, "
          f"preprocessing/model_routing.py is")
    print(f"  unchanged, main.py is unchanged, the fitting path is unchanged, "
          f"and no existing")
    print(f"  output CSV is rewritten. This is discovery, not a production "
          f"change.")
    print(f"  THE SELECTION IS PER FIT KEY, NOT PER ITEM. "
          f"{int((population['cv_key_shared_with'] > 0).sum())} items share a "
          f"fit key with")
    print(f"  another item and cannot choose differently from their family. "
          f"{len(population)} items sit on")
    print(f"  {population['cv_key'].nunique()} independent tournaments; every "
          f"count above should be read against both.")
    print(f"  THE SELECTION IS SCORED ON THE FOLDS THAT MADE IT. cv_winner_wape "
          f"is in-sample FOR")
    print(f"  THE SELECTION even though each fold is out-of-sample for the fit. "
          f"Track 2 is the")
    print(f"  only out-of-sample test of the selection itself, and it is one "
          f"six-month window.")
    print(f"  TRACK 1 AND TRACK 2 ARE NEVER AVERAGED. Track 1's folds end at "
          f"TRAIN_END ({config.TRAIN_END});")
    print(f"  track 2 is {config.BENCHMARK_START}..{config.BENCHMARK_END}. BDM "
          f"exists only in the second. A candidate that")
    print(f"  wins one and loses the other has been measured twice, not "
          f"contradicted.")
    print(f"  THE CURRENT METHOD'S TRACK-2 NUMBER IS THE SHIPPED FORECAST, "
          f"which carries production's")
    print(f"  segmented changepoint_prior_scale; the refit Prophet candidate "
          f"carries the config")
    print(f"  default. Where those two differ it is a fitting difference, not "
          f"a routing result.")
    print(f"  TWO ETS VARIANTS AND ONE FIXED SARIMA SPEC WERE RUN. Reporting "
          f"the better ETS as")
    print(f"  'ETS' would be two shots at one target scored as one, so they "
          f"carry separate names")
    print(f"  everywhere. SARIMA's order and its seasonal period "
          f"({SARIMA_SEASONAL_PERIOD}) are fixed and untuned;")
    print(f"  a poor SARIMA result is a result about THIS spec.")
    print(f"  SEVEN CANDIDATES OVER AT MOST {MAX_CUTOFFS} FOLDS IS A WIDE "
          f"SEARCH ON THIN EVIDENCE. That is")
    print(f"  what the two bars exist for, and the gap between cv_winner and "
          f"confident_switch is")
    print(f"  the measurement of how much of the raw result was noise — not a "
          f"side note to it.")
    print(f"  THE SEVERE-BIAS CLUSTER IS INCLUDED "
          f"({int(verdict['bias_cluster'].sum())} items, flagged "
          f"`bias_cluster`), because this")
    print(f"  is the whole book and dropping seven would make it a different "
          f"population. Every")
    print(f"  aggregate can be recomputed without them from the CSVs.")


# ── Output schemas ────────────────────────────────────────────────────────────

#: The brief's three columns first, then the decomposition that produced them.
FOLD_COLS = ["item_code", "candidate", "cutoff", "wape", "mape", "fold_note",
             "cv_key", "fold_basis", "n_train_months_at_cutoff",
             "yhat_level_or_forecast", "actual_sum", "n_horizon_months",
             "abs_err_sum", "n_clipped_months"]

RESULT_COLS = ["item_code", "candidate", "n_folds_scored", "median_wape",
               "bias_ratio", "cv_key", "current_method", "n_folds",
               "n_folds_zero_demand", "mean_wape", "scorer_delta_pp",
               "wape_pooled", "median_mape", "mean_yhat_level",
               "mean_actual_rate", "n_clipped_months"]

WINDOW_ROW_COLS = ["item_code", "candidate", "month", "actual", "yhat_raw",
                   "yhat", "bdm_forecast", "routed_model", "split_ratio"]


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    # The tables below are drawn with box characters, and a bare Windows console
    # is cp1252. Without this the run dies on its first heading rather than on
    # anything to do with forecasting.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--db", action="store_true",
                    help="Reload raw inputs from Azure SQL instead of the cached CSVs")
    ap.add_argument("--input-dir", default=None,
                    help="Directory holding the cached input CSVs and receiving "
                         "this experiment's output (default: config.OUTPUT_DIR, "
                         "the post-window-roll vintage)")
    ap.add_argument("--cv-only", action="store_true",
                    help="Run track 1 only; skip the held-out BDM window")
    ap.add_argument("--sample", type=int, default=0, metavar="N",
                    help="Run N items and stop without writing the CSVs")
    args = ap.parse_args()

    if args.input_dir:
        config.OUTPUT_DIR = Path(args.input_dir).resolve()
    out_dir = config.OUTPUT_DIR
    print(f"Input/output directory: {out_dir}")
    if not out_dir.exists():
        print(f"  ERROR: {out_dir} does not exist. Pass --input-dir, or --db to "
              f"reload from the database.")
        return 1
    for required in ("test_validation.csv", "benchmark_comparison.csv"):
        if not (out_dir / required).exists():
            print(f"  ERROR: {out_dir / required} is missing.")
            return 1

    population = load_population(out_dir)
    print_design(population)

    if args.sample:
        population = population.head(args.sample)
        print(f"\nSAMPLE MODE — running {len(population)} item(s), no CSV "
              f"written\n")

    pooled, _families, _cm, _fitted = build_scope_input(cached=not args.db)
    window_all = (pd.DataFrame() if args.cv_only else load_bdm_window(out_dir))

    print(f"\nRunning the tournament over {len(population)} item(s)...")
    folds, wrows, skipped, failures, elapsed, n_fits = run_tournament(
        population, pooled.data, window_all, args.cv_only)

    if folds.empty:
        print("Nothing could be evaluated.")
        return 1

    scores = item_scores(folds)
    verdict = tournament_verdict(scores, population)
    shrink = shrinkage_table(folds, "whole book")
    wshrink = winner_shrinkage(verdict)

    wscores = (window_scores(wrows) if len(wrows)
               else pd.DataFrame(columns=["item_code", "candidate", "wape",
                                          "actual_sum"]))
    icheck = (window_identity_check(wrows, verdict, population) if len(wrows)
              else pd.DataFrame())
    srcheck = split_ratio_check(population)
    summary = build_summary(verdict, wscores)

    results = (scores.merge(population[["item_code", "current_method"]],
                            on="item_code", how="left")
               .reindex(columns=RESULT_COLS)
               .sort_values(["item_code", "candidate"]))

    if not args.sample:
        for path, frame in [
            (out_dir / "tournament_folds.csv",
             folds.reindex(columns=FOLD_COLS).round(4)),
            (out_dir / "tournament_results.csv", results.round(4)),
            (out_dir / "tournament_summary.csv", summary.round(3)),
            (out_dir / "tournament_shrinkage.csv", shrink.round(4)),
            (out_dir / "tournament_winner_shrinkage.csv", wshrink.round(4)),
            (out_dir / "tournament_bdm_window_results.csv",
             wrows.reindex(columns=WINDOW_ROW_COLS).round(3) if len(wrows)
             else pd.DataFrame(columns=WINDOW_ROW_COLS)),
            (out_dir / "tournament_grain_check.csv", icheck.round(6)),
            (out_dir / "tournament_skipped.csv", skipped),
            (out_dir / "tournament_fit_failures.csv", failures),
        ]:
            frame.to_csv(path, index=False)
            print(f"Wrote {path}  ({len(frame):,} rows)")

    print_track1(population, folds, scores, verdict, skipped, failures,
                 shrink, wshrink)
    if not args.cv_only:
        print_track2(summary, wscores, icheck, srcheck)
    print_answer(verdict, summary)
    print_caveats(population, verdict)
    print(f"\nTotal wall clock: {elapsed / 60:.1f} min over {n_fits} fits")
    if args.sample:
        print(f"Sample mode: no CSV written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
