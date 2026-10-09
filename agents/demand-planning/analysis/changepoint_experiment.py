"""
Changepoint experiment — does anything actually fix the rigid trend? (Phase 2)

WHAT THIS IS
────────────
`analysis/trend_audit.py` established the diagnosis: 38 of the 68 series with
`mape_pct > 100` carry the same signature — zero changepoints clearing
|delta| > 0.01, and a fitted trend sitting >30% away from the series' own recent
actual average. The mechanism is `changepoint_prior_scale = 0.05`, a Laplace
prior scale on each candidate rate adjustment, which for a representative case
(DN1380) permits a typical rate change of ~0.6 units/month against month-to-month
volatility with a standard deviation of ~195 units.

This module MEASURES two candidate remedies. It does not adopt either one.
Nothing here changes `config.py`, `preprocessing/`, `models/prophet_model.py`,
any existing `output/*.csv`, or the database. It writes two new CSVs and prints
a summary; whether any of it should be wired into the pipeline is a human
decision that this script deliberately does not make.

    remedy A — FORCED CHANGEPOINT.   For series in a pooled successor family
        that carries an `estimated_changeover` (from `dbo.product_successor_review`,
        reaching this code via `family_pool.SuccessorFamilies.changeover_by_family`),
        refit with `Prophet(changepoints=[that date])`. Every other config value
        is `config.PROPHET_PARAMS` verbatim. This substitutes a known business
        fact for a statistical rediscovery the prior is too tight to permit.

    remedy B — LOOSER PRIOR.   Automatic changepoint detection, but sweep
        `changepoint_prior_scale` over [0.05, 0.1, 0.25, 0.5]. Run for BOTH
        groups: for the no-known-date series it is the only option available,
        and for the known-date series it is the comparison that answers the
        question that actually matters — is wiring in known dates worth the
        work, or does a one-line global prior change get most of the benefit?

WHY NOT SCORE ON THE EXISTING TEST WINDOW
─────────────────────────────────────────
`model_metrics.csv` / `benchmark_comparison.csv` score on 2025-11 → 2026-04.
That window is what *selected* these 68 series as broken. Scoring a candidate
fix on it would be tuning against the test set — the same class of error as the
split-ratio look-ahead bug that `family_pool.compute_split_ratios` was rewritten
to remove. So the test window is never read here. Every model is fitted on data
ending at `config.TRAIN_END` and scored by `prophet.diagnostics.cross_validation`
at rolling cutoffs *inside* that training window: each fold refits on months
<= cutoff only and is scored on months the fold never saw. The held-out window
stays held out, still available to score whatever is adopted later.

CUTOFF GEOMETRY, AND WHY THE HORIZON IS 190 DAYS
────────────────────────────────────────────────
Prophet takes a horizon as a Timedelta, but this data is monthly, so the number
has to be chosen so that a fold contains exactly six monthly points regardless
of which months it lands on. The longest six-month span is 184 days (Jul→Jan)
and the shortest seven-month span is 212 days (Jan→Aug), so any horizon in
[184, 212) captures six points and never seven. 190 sits in that gap.

`cross_validation` also rejects any cutoff later than `max(ds) - horizon`, which
is why the newest cutoff is seven months before the series end rather than six:
at six months the gap (181-184 days) is shorter than the 190-day horizon and the
call raises. The last training month is therefore not scored by any fold. That
is the price of a horizon that is uniform across folds, and it is stated here
rather than hidden.

FAIRNESS OF THE FORCED-CHANGEPOINT COMPARISON
─────────────────────────────────────────────
`prophet.diagnostics.prophet_copy` retains explicit changepoints only where
`changepoint < last_history_date` at that fold's cutoff. A changeover dated after
a fold's cutoff is therefore silently dropped, and that fold's "forced
changepoint" model is really a *no changepoint at all* model — a flat line, which
would score terribly and would slander the remedy rather than test it. So for
each known-date series the fold set is restricted to folds where the changeover
is genuinely inside that fold's own history, and ALL variants (default included)
are then scored on that same restricted fold set. Per-series numbers are
therefore internally comparable; across series they are not, and `n_cutoffs` is
written to the detail file so that is visible.

A family whose changeover is too recent to be inside any honest fold gets no
forced-changepoint number at all, with the reason recorded. That is not a
failure of the method — it is the correct answer to "can this be validated
without look-ahead?", which is no.

Run:  python -m analysis.changepoint_experiment --sample 5   (timing probe)
      python -m analysis.changepoint_experiment               (full 68)
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
from prophet.diagnostics import cross_validation, performance_metrics  # noqa: E402

import config  # noqa: E402
from analysis.trend_audit import build_fit_input  # noqa: E402  — same rebuild, one definition
from models.prophet_model import TRAIN_END_TS, TRAIN_START_TS  # noqa: E402
from preprocessing import family_pool  # noqa: E402

logging.getLogger("cmdstanpy").setLevel(logging.ERROR)
logging.getLogger("prophet").setLevel(logging.ERROR)

# ── Experiment design constants (judgment calls, all stated in the summary) ────

#: Prior-scale grid. 0.05 is `config.PROPHET_PARAMS['changepoint_prior_scale']`,
#: i.e. the current default, and is included so the grid contains its own control.
PRIOR_GRID = [0.05, 0.1, 0.25, 0.5]
DEFAULT_PRIOR = config.PROPHET_PARAMS["changepoint_prior_scale"]

#: Forecast horizon per fold. Six months = the horizon the business is served
#: (config.FORECAST_START → FORECAST_END). See the docstring for the 190.
CV_HORIZON_MONTHS = 6
CV_HORIZON = "190 days"

#: Months between successive cutoffs, and the cap on how many folds to run.
#: Three is half the horizon (Prophet's own default relationship); the cap is a
#: runtime lever — every extra fold is one extra Prophet fit per variant per series.
CV_PERIOD_MONTHS = 3
MAX_CUTOFFS = 6

#: Minimum months of history a fold may train on. Same bar the pipeline itself
#: applies before it will fit a series at all (config.MIN_TRAIN_MONTHS).
MIN_CV_TRAIN_MONTHS = config.MIN_TRAIN_MONTHS

VARIANT_DEFAULT = "default"
VARIANT_FORCED = "forced_changepoint"
GROUP_KNOWN = "known_date"
GROUP_NO_DATE = "no_known_date"


# ── Target list and its partition ─────────────────────────────────────────────

def load_targets() -> pd.DataFrame:
    """The 68 audited series, straight from the audit's own output."""
    path = config.OUTPUT_DIR / "trend_diagnostic_audit.csv"
    df = pd.read_csv(path)
    df["item_code"] = df["item_code"].astype(str).str.strip()
    df["family_key"] = df["family_key"].astype(str).str.strip()
    return df


def partition(targets: pd.DataFrame,
              families: family_pool.SuccessorFamilies,
              eligible: frozenset[str]) -> pd.DataFrame:
    """
    Split the targets into the known-changeover-date group and everyone else.

    `known_date` requires BOTH that the series was fitted as a pooled family
    (family_key != item_code, i.e. the fit key is a real family) AND that the
    family carries a non-null `estimated_changeover`. A family that was pooled
    but whose review rows never got a date cannot have a date forced, so it
    belongs in the no-known-date group even though it is a family.
    """
    out = targets.copy()
    keys, dates = [], []
    for row in out.itertuples(index=False):
        key = families.family_key(row.item_code)
        is_pooled = key in eligible
        keys.append(key)
        dates.append(families.changeover_by_family.get(key, pd.NaT) if is_pooled else pd.NaT)

    out["fit_key"] = keys
    out["estimated_changeover"] = pd.to_datetime(pd.Series(dates, index=out.index))
    out["group"] = np.where(out["estimated_changeover"].notna(), GROUP_KNOWN, GROUP_NO_DATE)
    return out


# ── Cutoff construction ───────────────────────────────────────────────────────

def make_cutoffs(train: pd.DataFrame) -> list[pd.Timestamp]:
    """
    Rolling cutoffs inside the training window, newest first in construction and
    returned oldest-first (the order `cross_validation` expects).

    A cutoff survives only if it satisfies all three of:
      * at least MIN_CV_TRAIN_MONTHS observed months at or before it,
      * at least one observed month inside (cutoff, cutoff + horizon] — a sparse
        series can have a fold with nothing in it to score, and Prophet has no
        graceful answer for that,
      * strictly later than the first observed month (a `cross_validation`
        precondition).
    """
    if train.empty:
        return []

    max_ds = train["ds"].max()
    min_ds = train["ds"].min()
    horizon = pd.Timedelta(CV_HORIZON)

    # Newest permissible cutoff: cross_validation requires cutoff <= max_ds -
    # horizon, and 190 days is more than six calendar months, so this lands
    # seven months back. See docstring.
    newest = max_ds - pd.DateOffset(months=CV_HORIZON_MONTHS + 1)

    cutoffs: list[pd.Timestamp] = []
    for i in range(MAX_CUTOFFS):
        c = newest - pd.DateOffset(months=CV_PERIOD_MONTHS * i)
        if c <= min_ds:
            break
        if (train["ds"] <= c).sum() < MIN_CV_TRAIN_MONTHS:
            break
        if not ((train["ds"] > c) & (train["ds"] <= c + horizon)).any():
            continue
        cutoffs.append(c)

    return sorted(cutoffs)


def usable_for_changepoint(train: pd.DataFrame, cutoffs: list[pd.Timestamp],
                           changeover: pd.Timestamp) -> list[pd.Timestamp]:
    """
    The subset of `cutoffs` whose own history actually contains the changeover.

    Mirrors `prophet_copy`'s retention rule exactly (`changepoint <
    last_history_date`, where last_history_date is the newest observed month at
    or before the cutoff) so that a fold is only kept when the forced
    changepoint really survives into that fold's model.
    """
    keep = []
    for c in cutoffs:
        hist = train.loc[train["ds"] <= c, "ds"]
        if hist.empty:
            continue
        if train["ds"].min() < changeover < hist.max():
            keep.append(c)
    return keep


# ── Scoring ───────────────────────────────────────────────────────────────────

def _fold_scores(cv: pd.DataFrame) -> tuple[float, float, int]:
    """
    Per-cutoff MAPE and WAPE, averaged over cutoffs.

    `yhat` is clipped at zero first because `models.prophet_model.forecast_series`
    clips it in production — scoring an unclipped prediction would be scoring a
    model the pipeline does not actually ship.

    MAPE skips rows with y == 0 (undefined, not zero-error). `prepare()` emits no
    zero-quantity rows so this should never bite, but a fold whose every row was
    skipped contributes nothing rather than a NaN that would poison the mean.
    WAPE is the sum-normalised counterpart and is stable where MAPE is not, which
    matters here: several of these series average under two units a month, where
    MAPE is enormous for arithmetic reasons rather than modelling ones.
    """
    cv = cv.copy()
    cv["yhat"] = cv["yhat"].clip(lower=0)
    cv["abs_err"] = (cv["y"] - cv["yhat"]).abs()

    mapes, wapes = [], []
    for _, fold in cv.groupby("cutoff"):
        nz = fold[fold["y"] != 0]
        if len(nz):
            mapes.append(float((nz["abs_err"] / nz["y"].abs()).mean() * 100))
        denom = float(fold["y"].abs().sum())
        if denom > 0:
            wapes.append(float(fold["abs_err"].sum() / denom * 100))

    mape = float(np.mean(mapes)) if mapes else float("nan")
    wape = float(np.mean(wapes)) if wapes else float("nan")
    return mape, wape, len(cv)


def run_variant(train: pd.DataFrame, cutoffs: list[pd.Timestamp],
                prior: float, changepoints: list[pd.Timestamp] | None) -> dict:
    """
    Fit one variant and cross-validate it on `cutoffs`.

    `cross_validation` needs a model already fitted on the full history — it
    reads `model.history` and refits a copy per cutoff. Because `train` stops at
    `config.TRAIN_END`, that history never contains a test-window month, so no
    fold can see one.

    `performance_metrics(..., rolling_window=1)` is called alongside the
    per-fold numbers as an independent aggregate: it pools every horizon-row
    across every fold instead of averaging fold means, so a large gap between
    the two is a sign the folds disagree with each other and the headline number
    is being carried by one of them.
    """
    params = dict(config.PROPHET_PARAMS)
    params["changepoint_prior_scale"] = prior
    if changepoints is not None:
        params["changepoints"] = changepoints

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = Prophet(**params)
        model.fit(train)
        cv = cross_validation(
            model, horizon=CV_HORIZON, cutoffs=cutoffs, disable_tqdm=True,
        )

    mape, wape, n_points = _fold_scores(cv)

    pm_mape = float("nan")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            pm = performance_metrics(cv, metrics=["mape"], rolling_window=1)
        if not pm.empty:
            pm_mape = float(pm["mape"].iloc[0]) * 100
    except Exception:  # pragma: no cover — pooled variant is a cross-check only
        pass

    return {
        "cv_mape": mape,
        "cv_wape": wape,
        "cv_mape_pooled": pm_mape,
        "n_cutoffs": len(cutoffs),
        "n_scored_points": n_points,
        "n_model_changepoints": int(len(model.changepoints)),
    }


# ── Per-series experiment ─────────────────────────────────────────────────────

def run_series(fit_key: str, series: pd.DataFrame, group: str,
               changeover: pd.Timestamp) -> tuple[list[dict], dict]:
    """
    Run every applicable variant for one fitted series.

    Returns (detail_rows, status) where status carries the fold geometry and any
    reason a variant could not be evaluated. Pooled families are run ONCE here
    and attributed to each of their item codes by the caller — the pipeline fits
    one model per family, so running one per member would be scoring models that
    do not exist.
    """
    train = (
        series[(series["ds"] >= TRAIN_START_TS) & (series["ds"] <= TRAIN_END_TS)]
        [["ds", "y"]].dropna().sort_values("ds").reset_index(drop=True)
    )

    status = {
        "fit_key": fit_key,
        "group": group,
        "n_train_months": len(train),
        "estimated_changeover": changeover,
        "n_cutoffs": 0,
        "forced_status": "",
        "skip_reason": "",
    }

    cutoffs = make_cutoffs(train)
    if not cutoffs:
        status["skip_reason"] = (
            f"no valid CV fold: {len(train)} train months, needs "
            f"{MIN_CV_TRAIN_MONTHS} before a cutoff {CV_HORIZON_MONTHS + 1} "
            f"months from the series end"
        )
        return [], status

    forced_cutoffs: list[pd.Timestamp] = []
    if group == GROUP_KNOWN:
        forced_cutoffs = usable_for_changepoint(train, cutoffs, changeover)
        if not forced_cutoffs:
            status["forced_status"] = (
                f"changeover {changeover.date()} is not inside any honest fold "
                f"(newest cutoff {max(cutoffs).date()}) — cannot be validated "
                f"without look-ahead"
            )
        else:
            # Every variant is scored on the same folds, or the comparison is
            # between different questions rather than different models.
            cutoffs = forced_cutoffs
            status["forced_status"] = "evaluated"

    status["n_cutoffs"] = len(cutoffs)

    rows: list[dict] = []
    for prior in PRIOR_GRID:
        res = run_variant(train, cutoffs, prior, changepoints=None)
        res.update({
            "fit_key": fit_key,
            "variant": VARIANT_DEFAULT if prior == DEFAULT_PRIOR else f"prior_{prior}",
            "changepoint_prior_scale": prior,
            "forced_changepoint": "",
        })
        rows.append(res)

    if forced_cutoffs:
        res = run_variant(train, cutoffs, DEFAULT_PRIOR, changepoints=[changeover])
        res.update({
            "fit_key": fit_key,
            "variant": VARIANT_FORCED,
            "changepoint_prior_scale": DEFAULT_PRIOR,
            "forced_changepoint": changeover.date().isoformat(),
        })
        rows.append(res)

    return rows, status


# ── Assembly ──────────────────────────────────────────────────────────────────

def summarise_series(detail: pd.DataFrame) -> dict:
    """
    Collapse one series' variant rows into the verdict columns.

    Ties go to the incumbent: an alternative has to actually beat the default to
    be called a winner, because "no measurable difference" is an argument for
    changing nothing.
    """
    by_variant = detail.set_index("variant")["cv_mape"].to_dict()
    default = by_variant.get(VARIANT_DEFAULT, float("nan"))
    forced = by_variant.get(VARIANT_FORCED, float("nan"))

    priors = detail[detail["variant"] != VARIANT_FORCED]
    priors = priors.dropna(subset=["cv_mape"])
    if priors.empty:
        best_prior, best_prior_mape = float("nan"), float("nan")
    else:
        best = priors.loc[priors["cv_mape"].idxmin()]
        best_prior = float(best["changepoint_prior_scale"])
        best_prior_mape = float(best["cv_mape"])

    candidates = {VARIANT_DEFAULT: default}
    if pd.notna(forced):
        candidates[VARIANT_FORCED] = forced
    if pd.notna(best_prior_mape) and best_prior != DEFAULT_PRIOR:
        candidates["looser_prior"] = best_prior_mape

    winner = VARIANT_DEFAULT
    best_val = default
    for name, val in candidates.items():
        if name == VARIANT_DEFAULT or pd.isna(val):
            continue
        if pd.isna(best_val) or val < best_val:
            winner, best_val = name, val

    improvement = (
        (default - best_val) / default * 100
        if pd.notna(default) and default != 0 and pd.notna(best_val) else float("nan")
    )

    # Regression check (task item 4) — deliberately NOT computed from
    # `candidates`, which only carries the BEST prior. A series where 0.05 won
    # the grid has no entry in `candidates` beyond the default, yet it is
    # precisely the case being asked about: every looser prior was tried and
    # every one of them was worse. Score against every variant actually run.
    alternatives = detail[detail["variant"] != VARIANT_DEFAULT]["cv_mape"].dropna()
    all_worse = bool(len(alternatives)) and pd.notna(default) and bool(
        (alternatives > default).all()
    )
    n_worse = int((alternatives > default).sum()) if pd.notna(default) else 0

    wape = detail.set_index("variant")["cv_wape"].to_dict()
    return {
        "cv_mape_default": round(default, 2) if pd.notna(default) else np.nan,
        "cv_mape_forced_changepoint": round(forced, 2) if pd.notna(forced) else np.nan,
        "best_prior_scale_tested": best_prior,
        "cv_mape_best_prior_scale": round(best_prior_mape, 2) if pd.notna(best_prior_mape) else np.nan,
        "winner": winner if winner != VARIANT_DEFAULT else "default",
        "improvement_pct": round(improvement, 2) if pd.notna(improvement) else np.nan,
        "all_alternatives_worse": all_worse,
        "n_alternatives_tested": int(len(alternatives)),
        "n_alternatives_worse": n_worse,
        "cv_wape_default": round(wape.get(VARIANT_DEFAULT, float("nan")), 2),
        "cv_wape_forced_changepoint": (
            round(wape[VARIANT_FORCED], 2) if VARIANT_FORCED in wape else np.nan
        ),
    }


def run_experiment(targets: pd.DataFrame, scoped: pd.DataFrame
                   ) -> tuple[pd.DataFrame, pd.DataFrame, float]:
    """Fit every distinct series once, attribute results to every item code."""
    by_key = {k: g for k, g in scoped.groupby("item_code")}

    detail_frames: list[pd.DataFrame] = []
    result_rows: list[dict] = []
    cache: dict[str, tuple[pd.DataFrame, dict]] = {}
    started = time.perf_counter()

    for i, row in enumerate(targets.itertuples(index=False), start=1):
        key = row.fit_key
        if key not in by_key:
            result_rows.append({
                "item_code": row.item_code, "family_key": key, "group": row.group,
                "cv_mape_default": np.nan, "cv_mape_forced_changepoint": np.nan,
                "best_prior_scale_tested": np.nan, "cv_mape_best_prior_scale": np.nan,
                "winner": "not_evaluated", "improvement_pct": np.nan,
                "all_alternatives_worse": False,
                "n_alternatives_tested": 0, "n_alternatives_worse": 0,
                "cv_wape_default": np.nan, "cv_wape_forced_changepoint": np.nan,
                "n_cutoffs": 0, "n_train_months": 0,
                "estimated_changeover": "", "note": "absent from the rebuilt scope frame",
            })
            continue

        if key not in cache:
            elapsed = time.perf_counter() - started
            print(f"  [{i:>2}/{len(targets)}] {key} ({row.group}) … "
                  f"{elapsed:6.1f}s elapsed", flush=True)
            rows, status = run_series(key, by_key[key], row.group, row.estimated_changeover)
            cache[key] = (pd.DataFrame(rows), status)

        detail, status = cache[key]

        if detail.empty:
            result_rows.append({
                "item_code": row.item_code, "family_key": key, "group": row.group,
                "cv_mape_default": np.nan, "cv_mape_forced_changepoint": np.nan,
                "best_prior_scale_tested": np.nan, "cv_mape_best_prior_scale": np.nan,
                "winner": "not_evaluated", "improvement_pct": np.nan,
                "all_alternatives_worse": False,
                "n_alternatives_tested": 0, "n_alternatives_worse": 0,
                "cv_wape_default": np.nan, "cv_wape_forced_changepoint": np.nan,
                "n_cutoffs": status["n_cutoffs"],
                "n_train_months": status["n_train_months"],
                "estimated_changeover": (
                    status["estimated_changeover"].date().isoformat()
                    if pd.notna(status["estimated_changeover"]) else ""
                ),
                "note": status["skip_reason"],
            })
            continue

        verdict = summarise_series(detail)
        result_rows.append({
            "item_code": row.item_code, "family_key": key, "group": row.group,
            **verdict,
            "n_cutoffs": status["n_cutoffs"],
            "n_train_months": status["n_train_months"],
            "estimated_changeover": (
                status["estimated_changeover"].date().isoformat()
                if pd.notna(status["estimated_changeover"]) else ""
            ),
            "note": status["forced_status"] if status["forced_status"] != "evaluated" else "",
        })

        tagged = detail.copy()
        tagged.insert(0, "item_code", row.item_code)
        detail_frames.append(tagged)

    results = pd.DataFrame(result_rows)
    detail_all = (pd.concat(detail_frames, ignore_index=True)
                  if detail_frames else pd.DataFrame())
    return results, detail_all, time.perf_counter() - started


# ── Reporting ─────────────────────────────────────────────────────────────────

RESULT_COLS = [
    "item_code", "family_key", "group", "cv_mape_default",
    "cv_mape_forced_changepoint", "best_prior_scale_tested",
    "cv_mape_best_prior_scale", "winner", "improvement_pct",
    "all_alternatives_worse", "n_alternatives_tested", "n_alternatives_worse",
    "cv_wape_default", "cv_wape_forced_changepoint",
    "n_cutoffs", "n_train_months", "estimated_changeover", "note",
]


def print_summary(results: pd.DataFrame, detail: pd.DataFrame) -> None:
    sep = "-" * 78
    print(f"\n{sep}\nEXPERIMENT DESIGN (every number below is a judgment call, not a codebase value)\n{sep}")
    print(f"  validation        : prophet.diagnostics.cross_validation, rolling cutoffs")
    print(f"                      INSIDE the training window (<= {config.TRAIN_END}). The")
    print(f"                      {config.TEST_START}-{config.TEST_END} held-out window is never read.")
    print(f"  horizon / period  : {CV_HORIZON} ({CV_HORIZON_MONTHS} monthly points) / "
          f"{CV_PERIOD_MONTHS} months, max {MAX_CUTOFFS} folds")
    print(f"  fold minimum      : {MIN_CV_TRAIN_MONTHS} observed months before a cutoff")
    print(f"  prior grid        : {PRIOR_GRID}   (default = {DEFAULT_PRIOR})")
    print(f"  score             : per-fold MAPE, averaged over folds; yhat clipped at 0")
    print(f"                      as models/prophet_model.py does in production")

    evaluated = results[results["winner"] != "not_evaluated"]
    skipped = results[results["winner"] == "not_evaluated"]

    print(f"\n{sep}\nCOVERAGE\n{sep}")
    print(f"  targets                : {len(results)}")
    for g, grp in results.groupby("group"):
        ev = (grp["winner"] != "not_evaluated").sum()
        print(f"    {g:<16}     : {len(grp):>3}  ({ev} evaluated, {len(grp) - ev} not)")
    if len(skipped):
        print(f"\n  NOT EVALUATED — {len(skipped)} series, insufficient history for an honest fold:")
        for row in skipped.itertuples(index=False):
            print(f"    {row.item_code:<10} {row.note}")

    if evaluated.empty:
        print("\nNothing could be evaluated.")
        return

    forced_ok = evaluated[evaluated["cv_mape_forced_changepoint"].notna()]
    known = evaluated[evaluated["group"] == GROUP_KNOWN]
    forced_blocked = known[known["cv_mape_forced_changepoint"].isna()].drop_duplicates("family_key")
    if len(forced_blocked):
        print(f"\n  FORCED CHANGEPOINT NOT TESTABLE — {len(forced_blocked)} known-date "
              f"families ({int(known['cv_mape_forced_changepoint'].isna().sum())} item codes):")
        for row in forced_blocked.itertuples(index=False):
            print(f"    {row.family_key:<10} {row.note}")

    print(f"\n{sep}\nWINNERS ({len(evaluated)} evaluated series)\n{sep}")
    tab = (evaluated.groupby(["group", "winner"]).size()
           .unstack(fill_value=0))
    print(tab.to_string())

    print(f"\n{sep}\nREGRESSIONS — every tested alternative worse than the current default\n{sep}")
    for g, grp in evaluated.groupby("group"):
        n = int(grp["all_alternatives_worse"].sum())
        print(f"  {g:<16} : {n:>3} / {len(grp)}")
    worse = evaluated[evaluated["all_alternatives_worse"]]
    if len(worse):
        print("\n  " + ", ".join(sorted(worse["item_code"].unique())))

    print(f"\n{sep}\nFORCED CHANGEPOINT vs LOOSER PRIOR (series where both were tested)\n{sep}")
    if forced_ok.empty:
        print("  none — no known-date series had a fold its changeover was inside.")
    else:
        cmp = forced_ok.drop_duplicates("family_key").copy()
        cmp["forced_beats_prior"] = cmp["cv_mape_forced_changepoint"] < cmp["cv_mape_best_prior_scale"]
        cmp["forced_beats_default"] = cmp["cv_mape_forced_changepoint"] < cmp["cv_mape_default"]
        print(f"  distinct series tested both ways : {len(cmp)}")
        print(f"  forced beats current default     : {int(cmp['forced_beats_default'].sum())}")
        print(f"  forced beats the best prior      : {int(cmp['forced_beats_prior'].sum())}")
        gap = (cmp["cv_mape_best_prior_scale"] - cmp["cv_mape_forced_changepoint"])
        print(f"  median MAPE gap (prior - forced)  : {gap.median():+.2f} pp "
              f"(positive = forced is better)")
        print()
        print(cmp[["family_key", "cv_mape_default", "cv_mape_forced_changepoint",
                   "best_prior_scale_tested", "cv_mape_best_prior_scale",
                   "winner", "n_cutoffs"]].to_string(index=False))

    print(f"\n{sep}\nPRIOR-SCALE GRID — which value won, across all evaluated series\n{sep}")
    dedup = evaluated.drop_duplicates("family_key")
    counts = dedup["best_prior_scale_tested"].value_counts().sort_index()
    for val, n in counts.items():
        tag = "  (= current default)" if val == DEFAULT_PRIOR else ""
        print(f"  {val:<6} : {n:>3} series{tag}")

    edge = int(counts.get(max(PRIOR_GRID), 0))
    if edge:
        print(f"\n  CAVEAT: {edge} of {len(dedup)} series pick {max(PRIOR_GRID)}, the LARGEST value")
        print(f"  in the grid. That is a boundary solution — the grid does not bracket their")
        print(f"  optimum, so {max(PRIOR_GRID)} is a floor on what they want, not an estimate of it.")
        print(f"  Any adoption decision that turns on the specific value needs a wider grid first.")

    print(f"\n{sep}\nLARGEST IMPROVEMENTS (distinct fitted series)\n{sep}")
    top = (dedup[dedup["winner"] != "default"]
           .sort_values("improvement_pct", ascending=False)
           .head(15))
    if top.empty:
        print("  none — the current default was never beaten.")
    else:
        print(top[["family_key", "group", "cv_mape_default", "cv_mape_forced_changepoint",
                   "cv_mape_best_prior_scale", "winner", "improvement_pct"]]
              .to_string(index=False))


def print_timing(results: pd.DataFrame, detail: pd.DataFrame,
                 elapsed: float, n_full: int) -> None:
    sep = "-" * 78
    n_series = detail["fit_key"].nunique() if not detail.empty else 0
    n_fits = 0
    if not detail.empty:
        # one full-history fit + one refit per fold, per variant
        n_fits = int((detail["n_cutoffs"] + 1).sum())

    print(f"\n{sep}\nSAMPLE TIMING\n{sep}")
    print(f"  sampled item codes    : {len(results)}")
    print(f"  distinct series fitted: {n_series}")
    print(f"  variants run          : {len(detail)}")
    print(f"  Prophet fits          : ~{n_fits}")
    print(f"  wall clock            : {elapsed:.1f}s")
    if n_fits:
        print(f"  per fit               : {elapsed / n_fits:.2f}s")
    if n_series:
        per_series = elapsed / n_series
        print(f"  per distinct series   : {per_series:.1f}s")
        print(f"\n  EXTRAPOLATION to the full target list ({n_full} item codes):")
        print(f"    the 68 codes collapse to fewer distinct fitted series (pooled")
        print(f"    families are fitted once), so this is an upper bound.")
        print(f"    {n_full} x {per_series:.1f}s = {n_full * per_series / 60:.1f} min "
              f"(worst case, no pooling)")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--db", action="store_true",
                    help="Reload raw inputs from Azure SQL instead of the cached CSVs")
    ap.add_argument("--sample", type=int, default=0, metavar="N",
                    help="Time a sample of N item codes spanning both groups and stop "
                         "without writing the result CSV")
    args = ap.parse_args()

    targets = load_targets()
    print(f"Targets from trend_diagnostic_audit.csv: {len(targets)} item codes")

    scoped, families, eligible = build_fit_input(cached=not args.db)
    targets = partition(targets, families, eligible)

    n_known = (targets["group"] == GROUP_KNOWN).sum()
    n_no = (targets["group"] == GROUP_NO_DATE).sum()
    print(f"\nPartition by known changeover date (joined via family_key):")
    print(f"  known_date    : {n_known:>3} item codes "
          f"({targets.loc[targets['group'] == GROUP_KNOWN, 'fit_key'].nunique()} distinct families)")
    print(f"  no_known_date : {n_no:>3} item codes "
          f"({targets.loc[targets['group'] == GROUP_NO_DATE, 'fit_key'].nunique()} distinct series)")

    full_n = len(targets)
    if args.sample:
        # Half from each group where possible, so the timing covers the forced
        # -changepoint path (which runs one extra variant) as well as the grid.
        per = max(1, args.sample // 2)
        known = targets[targets["group"] == GROUP_KNOWN].drop_duplicates("fit_key").head(per)
        rest = targets[targets["group"] == GROUP_NO_DATE].drop_duplicates("fit_key").head(
            args.sample - len(known))
        targets = pd.concat([known, rest], ignore_index=True)
        print(f"\nSAMPLE MODE — {len(targets)} item codes "
              f"({len(known)} known_date, {len(rest)} no_known_date)")

    print()
    results, detail, elapsed = run_experiment(targets, scoped)

    if args.sample:
        print_timing(results, detail, elapsed, full_n)
        print("\nSample mode: no CSV written. Re-run without --sample for the full experiment.")
        return 0

    results = results.reindex(columns=RESULT_COLS)
    out = config.OUTPUT_DIR
    for path, frame in [
        (out / "changepoint_experiment_results.csv", results),
        (out / "changepoint_experiment_detail.csv", detail),
    ]:
        frame.to_csv(path, index=False)
        print(f"\nWrote {path}  ({len(frame):,} rows)")

    print_summary(results, detail)
    print(f"\nTotal wall clock: {elapsed / 60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
