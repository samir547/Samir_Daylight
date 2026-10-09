"""
Phase 0 deployment validation — does the shipped config actually beat the old one?

WHAT THIS SCORES
────────────────
Three changes went into production together on 2026-08-03:

    1. the date window rolled forward two months (config.TEST_END 2026-04 →
       2026-06, anchored on analysis/month_completeness.py, not the calendar)
    2. `changepoint_prior_scale` 0.05 → 0.25 for the known-changeover SEGMENT
       only (preprocessing.family_pool.known_changeover_keys)
    3. `yearly_seasonality` True (order 10) → 3, globally

This module measures 2 and 3 against the model they replaced, on the window from
1. It changes nothing: no config value, no pipeline output, no database row.

    BEFORE   yearly_seasonality = True (order 10), changepoint_prior_scale = 0.05
             for every series — i.e. exactly production as it stood before today.
    AFTER    config.PROPHET_PARAMS verbatim, plus
             config.CHANGEPOINT_PRIOR_KNOWN_CHANGEOVER on the segment.

Both are fitted on the SAME series, over the SAME folds, so the comparison is
between two models and not between two datasets.

WHY THE CV FOLDS AND NOT JUST THE TEST WINDOW
─────────────────────────────────────────────
Both are reported, and they answer different questions.

The CV numbers are the primary result. They use `analysis.changepoint_experiment`'s
cutoff geometry and fold scorer — imported, not reimplemented, so this cannot
drift from the harness the parameters were originally chosen on. Every fold
refits on months <= its own cutoff, strictly inside the training window, and is
scored on months that fold never saw. See that module's docstring for why the
horizon is 190 days and why the newest cutoff sits seven months back.

The test-window numbers (`--test-window`, run separately, see main()) are a
genuine bonus holdout: the parameters were selected on CV cutoffs inside a
training window that ended 2025-10, so 2026-01..2026-06 was never read by the
selection. That will not be true at the next roll-forward, when today's test
window has become training data — at which point the CV numbers are the only
honest ones and this note is the reason why.

THE STUBBORN RE-PULL
────────────────────
`changepoint_experiment.summarise_series` flagged 13 item codes where EVERY
tested alternative scored worse than the then-default — genuinely unresponsive
series rather than ones the sweep simply had not reached. That check is re-run
here against the PHASE-0 baseline, which for a known-changeover item is now 0.25
rather than 0.05, so "every alternative is worse" is being asked of a different
incumbent than it was the first time. An item that drops off the list did so
because Phase 0 moved it, not because the bar moved.

Run:  python -m analysis.phase0_validation
      python -m analysis.phase0_validation --sample 5   (timing probe)
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
    _fold_scores,
    make_cutoffs,
    usable_for_changepoint,
)
from analysis.trend_audit import build_fit_input  # noqa: E402
from models.prophet_model import TRAIN_END_TS, TRAIN_START_TS  # noqa: E402
from preprocessing import family_pool  # noqa: E402

logging.getLogger("cmdstanpy").setLevel(logging.ERROR)
logging.getLogger("prophet").setLevel(logging.ERROR)

# ── The two production configurations under comparison ────────────────────────

#: Production before 2026-08-03. Written out literally rather than reconstructed
#: from config, because config no longer holds these values — that is the point.
BEFORE_PARAMS = dict(
    yearly_seasonality      = True,          # Prophet's default = Fourier order 10
    weekly_seasonality      = False,
    daily_seasonality       = False,
    seasonality_mode        = "multiplicative",
    changepoint_prior_scale = 0.05,
    seasonality_prior_scale = 10.0,
    interval_width          = 0.95,
)

VARIANT_BEFORE = "before"
VARIANT_AFTER = "after"

#: The 13 item codes `changepoint_experiment` flagged with all_alternatives_worse
#: on the 2025-10 window. Hard-coded because the file they came from is a
#: snapshot of that run and will be overwritten by the next one.
ORIGINAL_STUBBORN = [
    "A25090", "A25100", "A62001", "A91707", "AN1180", "AN1460", "DN1380",
    "U35070", "U35090", "U35501", "U62001", "UN1180", "UN91171",
]

#: Alternatives swept for the stubborn re-pull. The same prior grid as the
#: original experiment; each series' own Phase-0 baseline value is skipped, since
#: an alternative identical to the incumbent is not an alternative.
PRIOR_GRID = [0.05, 0.1, 0.25, 0.5]


def after_params(fit_key: str, known_changeover: frozenset[str]) -> dict:
    """Production as shipped, for this series' segment."""
    params = dict(config.PROPHET_PARAMS)
    if fit_key in known_changeover:
        params["changepoint_prior_scale"] = config.CHANGEPOINT_PRIOR_KNOWN_CHANGEOVER
    return params


# ── One fit, cross-validated ──────────────────────────────────────────────────

def score_variant(train: pd.DataFrame, cutoffs: list[pd.Timestamp],
                  params: dict) -> dict:
    """
    Fit `params` on the full training window and cross-validate on `cutoffs`.

    `cross_validation` needs a model already fitted on the full history — it
    reads `model.history` and refits a copy per cutoff. `train` stops at
    config.TRAIN_END, so no fold's history can contain a test-window month.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = Prophet(**params)
        model.fit(train)
        cv = cross_validation(model, horizon=CV_HORIZON, cutoffs=cutoffs,
                              disable_tqdm=True)

    mape, wape, n_points = _fold_scores(cv)
    return {
        "cv_mape": mape,
        "cv_wape": wape,
        "n_cutoffs": len(cutoffs),
        "n_scored_points": n_points,
        "fourier_order": params["yearly_seasonality"],
        "changepoint_prior_scale": params["changepoint_prior_scale"],
        "seasonality_prior_scale": params["seasonality_prior_scale"],
    }


def training_window(series: pd.DataFrame) -> pd.DataFrame:
    return (
        series[(series["ds"] >= TRAIN_START_TS) & (series["ds"] <= TRAIN_END_TS)]
        [["ds", "y"]].dropna().sort_values("ds").reset_index(drop=True)
    )


# ── Part 1: before vs after, every fitted series ──────────────────────────────

def compare_all(scoped: pd.DataFrame, known_changeover: frozenset[str],
                limit: int = 0) -> tuple[pd.DataFrame, float]:
    """One row per fitted series with both variants' CV scores."""
    keys = sorted(scoped["item_code"].astype(str).str.strip().unique())
    if limit:
        keys = keys[::max(1, len(keys) // limit)][:limit]

    by_key = {k: g for k, g in scoped.groupby("item_code")}
    rows: list[dict] = []
    started = time.perf_counter()

    for i, key in enumerate(keys, start=1):
        train = training_window(by_key[key])
        segment = "known_changeover" if key in known_changeover else "standard"

        cutoffs = make_cutoffs(train)
        if not cutoffs:
            rows.append({
                "fit_key": key, "segment": segment, "n_train_months": len(train),
                "n_cutoffs": 0, "note": "no valid CV fold",
            })
            continue

        elapsed = time.perf_counter() - started
        print(f"  [{i:>3}/{len(keys)}] {key:<10} {segment:<16} "
              f"{len(cutoffs)} folds … {elapsed:6.1f}s", flush=True)

        before = score_variant(train, cutoffs, BEFORE_PARAMS)
        after = score_variant(train, cutoffs, after_params(key, known_changeover))

        rows.append({
            "fit_key": key,
            "segment": segment,
            "n_train_months": len(train),
            "n_cutoffs": len(cutoffs),
            "cv_mape_before": round(before["cv_mape"], 2),
            "cv_mape_after": round(after["cv_mape"], 2),
            "cv_wape_before": round(before["cv_wape"], 2),
            "cv_wape_after": round(after["cv_wape"], 2),
            "mape_delta": round(after["cv_mape"] - before["cv_mape"], 2),
            "wape_delta": round(after["cv_wape"] - before["cv_wape"], 2),
            "mape_improved": bool(after["cv_mape"] < before["cv_mape"]),
            "wape_improved": bool(after["cv_wape"] < before["cv_wape"]),
            "changepoint_prior_after": after["changepoint_prior_scale"],
            "fourier_order_after": after["fourier_order"],
            "note": "",
        })

    return pd.DataFrame(rows), time.perf_counter() - started


# ── Part 2: the stubborn re-pull ──────────────────────────────────────────────

#: Variant-name prefixes belonging to the ORIGINAL check's alternative set.
#: changepoint_experiment swept the prior grid and the forced changepoint, and
#: nothing else — so "still stubborn, original definition" must be recomputed
#: over exactly these, or a series would look rescued purely because this module
#: offered it more ways to move.
ORIGINAL_ALTERNATIVES = ("prior_", "forced_changepoint")


def repull_stubborn(scoped: pd.DataFrame, families: family_pool.SuccessorFamilies,
                    known_changeover: frozenset[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Re-run `all_alternatives_worse` for the original 13, against Phase 0.

    The baseline is what the item is ACTUALLY fitted with today (segment-aware),
    not a fixed 0.05 — otherwise a known-changeover item would be tested against
    an incumbent it no longer has.

    Returns (summary, detail). Two verdicts are reported per item:

      `still_stubborn_original` — recomputed over ONLY the alternatives the
          original check swept (the changepoint prior grid + forced changepoint).
          This is the like-for-like number: same question, new baseline.
      `still_stubborn_wide` — over those PLUS the seasonality levers Phase 0
          introduced (Fourier order, seasonality prior). Strictly easier to
          escape, because there are more ways to beat the baseline.

    Both are carried because the wide answer alone would overstate how much
    Phase 0 moved these series, and the original answer alone would ignore the
    levers that are now actually available.
    """
    by_key = {k: g for k, g in scoped.groupby("item_code")}
    rows: list[dict] = []
    detail_rows: list[dict] = []

    for item in ORIGINAL_STUBBORN:
        key = families.family_key(item)
        fit_key = key if key in by_key else item

        if fit_key not in by_key:
            rows.append({"item_code": item, "fit_key": fit_key,
                         "still_stubborn": False,
                         "note": "absent from the rebuilt scope frame — not fitted "
                                 "on this window"})
            continue

        train = training_window(by_key[fit_key])
        cutoffs = make_cutoffs(train)
        if not cutoffs:
            rows.append({"item_code": item, "fit_key": fit_key,
                         "still_stubborn": False, "n_train_months": len(train),
                         "note": "no valid CV fold on this window"})
            continue

        segment = "known_changeover" if fit_key in known_changeover else "standard"
        base_params = after_params(fit_key, known_changeover)
        base_prior = base_params["changepoint_prior_scale"]
        print(f"  stubborn: {item:<10} (fit {fit_key}, {segment}, "
              f"baseline prior {base_prior}) …", flush=True)

        baseline = score_variant(train, cutoffs, base_params)

        alts: list[dict] = []
        for prior in PRIOR_GRID:
            if prior == base_prior:
                continue
            p = dict(base_params)
            p["changepoint_prior_scale"] = prior
            r = score_variant(train, cutoffs, p)
            alts.append({"variant": f"prior_{prior}", **r})

        # Seasonality alternatives, so "unresponsive" means unresponsive to the
        # levers Phase 0 actually has, not just to the trend prior.
        for order in [5, 10]:
            p = dict(base_params)
            p["yearly_seasonality"] = order
            alts.append({"variant": f"order_{order}",
                         **score_variant(train, cutoffs, p)})
        for sprior in [3.0, 1.0]:
            p = dict(base_params)
            p["seasonality_prior_scale"] = sprior
            alts.append({"variant": f"seas_prior_{sprior}",
                         **score_variant(train, cutoffs, p)})

        # Forced changepoint, where the family's changeover is inside an honest
        # fold — same eligibility rule as the original experiment.
        changeover = families.changeover_by_family.get(fit_key, pd.NaT)
        if pd.notna(changeover):
            forced_cutoffs = usable_for_changepoint(train, cutoffs, changeover)
            if forced_cutoffs:
                p = dict(base_params)
                p["changepoints"] = [changeover]
                # Scored on the same restricted folds as its own re-baseline, or
                # the comparison is between different questions.
                fb = score_variant(train, forced_cutoffs, base_params)
                fa = score_variant(train, forced_cutoffs, p)
                alts.append({"variant": "forced_changepoint",
                             **fa, "_own_baseline": fb["cv_mape"]})

        scored = [a for a in alts if pd.notna(a["cv_mape"])]
        for a in scored:
            own_base = a.get("_own_baseline", baseline["cv_mape"])
            detail_rows.append({
                "item_code": item, "fit_key": fit_key, "variant": a["variant"],
                "cv_mape": round(a["cv_mape"], 2),
                "cv_wape": round(a["cv_wape"], 2),
                "cv_mape_baseline": round(own_base, 2),
                "worse_than_baseline": bool(a["cv_mape"] > own_base),
                "in_original_alternative_set": a["variant"].startswith(ORIGINAL_ALTERNATIVES),
            })

        def verdict(pool: list[dict]) -> tuple[bool, int, int]:
            w = [a for a in pool
                 if a["cv_mape"] > a.get("_own_baseline", baseline["cv_mape"])]
            return bool(pool) and len(w) == len(pool), len(pool), len(w)

        orig_pool = [a for a in scored
                     if a["variant"].startswith(ORIGINAL_ALTERNATIVES)]
        wide_worse, n_wide, n_wide_worse = verdict(scored)
        orig_worse, n_orig, n_orig_worse = verdict(orig_pool)
        best = min(scored, key=lambda a: a["cv_mape"]) if scored else None
        best_orig = min(orig_pool, key=lambda a: a["cv_mape"]) if orig_pool else None

        rows.append({
            "item_code": item,
            "fit_key": fit_key,
            "segment": segment,
            "n_train_months": len(train),
            "n_cutoffs": len(cutoffs),
            "baseline_prior": base_prior,
            "cv_mape_phase0": round(baseline["cv_mape"], 2),
            "cv_wape_phase0": round(baseline["cv_wape"], 2),
            "n_alt_original": n_orig,
            "n_alt_original_worse": n_orig_worse,
            "still_stubborn_original": orig_worse,
            "best_alternative_original": best_orig["variant"] if best_orig else "",
            "n_alt_wide": n_wide,
            "n_alt_wide_worse": n_wide_worse,
            "still_stubborn_wide": wide_worse,
            "best_alternative": best["variant"] if best else "",
            "cv_mape_best_alternative": round(best["cv_mape"], 2) if best else np.nan,
            "note": "",
        })

    return pd.DataFrame(rows), pd.DataFrame(detail_rows)


# ── Reporting ─────────────────────────────────────────────────────────────────

def print_comparison(cmp: pd.DataFrame) -> None:
    sep = "-" * 78
    ev = cmp.dropna(subset=["cv_mape_before"])

    print(f"\n{sep}\nBEFORE vs AFTER — rolling-origin CV inside the training window\n{sep}")
    print(f"  window            : train <= {config.TRAIN_END}  "
          f"(test {config.TEST_START}..{config.TEST_END} never read)")
    print(f"  before            : order 10, seasonality prior 10.0, "
          f"changepoint prior 0.05 everywhere")
    print(f"  after             : order {config.PROPHET_PARAMS['yearly_seasonality']}, "
          f"seasonality prior {config.PROPHET_PARAMS['seasonality_prior_scale']}, "
          f"changepoint prior "
          f"{config.PROPHET_PARAMS['changepoint_prior_scale']}"
          f"/{config.CHANGEPOINT_PRIOR_KNOWN_CHANGEOVER} by segment")
    print(f"  series scored     : {len(ev)} of {len(cmp)} "
          f"({len(cmp) - len(ev)} had no valid CV fold)")

    if ev.empty:
        print("\nNothing could be scored.")
        return

    for label, grp in [("ALL", ev), *sorted(ev.groupby("segment"))]:
        n = len(grp)
        mi = int(grp["mape_improved"].sum())
        wi = int(grp["wape_improved"].sum())
        print(f"\n  {label} ({n} series)")
        print(f"    MAPE improved : {mi:>3}/{n}  ({mi / n * 100:.0f}%)   "
              f"median {grp['cv_mape_before'].median():8.2f} -> "
              f"{grp['cv_mape_after'].median():8.2f}  "
              f"(median delta {grp['mape_delta'].median():+.2f}pp)")
        print(f"    WAPE improved : {wi:>3}/{n}  ({wi / n * 100:.0f}%)   "
              f"median {grp['cv_wape_before'].median():8.2f} -> "
              f"{grp['cv_wape_after'].median():8.2f}  "
              f"(median delta {grp['wape_delta'].median():+.2f}pp)")

    print(f"\n{sep}\nLARGEST REGRESSIONS (after worse than before, by MAPE)\n{sep}")
    worst = ev.sort_values("mape_delta", ascending=False).head(10)
    print(worst[["fit_key", "segment", "cv_mape_before", "cv_mape_after",
                 "mape_delta", "cv_wape_before", "cv_wape_after"]]
          .to_string(index=False))

    print(f"\n{sep}\nLARGEST IMPROVEMENTS\n{sep}")
    best = ev.sort_values("mape_delta").head(10)
    print(best[["fit_key", "segment", "cv_mape_before", "cv_mape_after",
                "mape_delta", "cv_wape_before", "cv_wape_after"]]
          .to_string(index=False))


def print_stubborn(st: pd.DataFrame) -> None:
    sep = "-" * 78
    print(f"\n{sep}\nSTUBBORN RE-PULL — the original 13, against the Phase 0 baseline\n{sep}")
    print(f"  original list : {len(ORIGINAL_STUBBORN)} item codes")
    for col, label in [("still_stubborn_original",
                        "LIKE-FOR-LIKE (prior grid + forced changepoint only)"),
                       ("still_stubborn_wide",
                        "WIDE (also Fourier order + seasonality prior)")]:
        still = st[st[col].fillna(False)]
        moved = st[~st[col].fillna(False)]
        print(f"\n  {label}")
        print(f"    still stubborn    : {len(still)}"
              + (f"  — {', '.join(sorted(still['item_code']))}" if len(still) else ""))
        print(f"    no longer stubborn: {len(moved)}"
              + (f"  — {', '.join(sorted(moved['item_code']))}" if len(moved) else ""))
    print()
    cols = [c for c in ["item_code", "fit_key", "segment", "baseline_prior",
                        "cv_mape_phase0", "n_alt_original", "n_alt_original_worse",
                        "still_stubborn_original", "n_alt_wide", "n_alt_wide_worse",
                        "still_stubborn_wide", "best_alternative",
                        "cv_mape_best_alternative", "note"]
            if c in st.columns]
    print(st[cols].to_string(index=False))


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--db", action="store_true",
                    help="Reload raw inputs from Azure SQL instead of the cached CSVs")
    ap.add_argument("--sample", type=int, default=0, metavar="N",
                    help="Score only N series spanning the list, and skip the "
                         "stubborn re-pull (timing probe; writes no CSV)")
    ap.add_argument("--stubborn-only", action="store_true",
                    help="Re-run only the stubborn re-pull, leaving an existing "
                         "phase0_before_after_cv.csv in place")
    args = ap.parse_args()

    scoped, families, eligible = build_fit_input(cached=not args.db)
    known_changeover = family_pool.known_changeover_keys(families, eligible)

    n_keys = scoped["item_code"].nunique()
    n_seg = len(set(scoped["item_code"].astype(str).str.strip()) & known_changeover)
    print(f"Fitted series on this window: {n_keys} "
          f"({n_seg} known-changeover, {n_keys - n_seg} standard)")
    print()

    out = config.OUTPUT_DIR

    if args.stubborn_only:
        stubborn, detail = repull_stubborn(scoped, families, known_changeover)
        for path, frame in [(out / "phase0_stubborn_repull.csv", stubborn),
                            (out / "phase0_stubborn_repull_detail.csv", detail)]:
            frame.to_csv(path, index=False)
            print(f"\nWrote {path}  ({len(frame):,} rows)")
        print_stubborn(stubborn)
        return 0

    cmp, elapsed = compare_all(scoped, known_changeover, limit=args.sample)

    if args.sample:
        n_fits = int((cmp["n_cutoffs"].fillna(0) + 1).sum()) * 2
        print(f"\n  sampled series : {len(cmp)}")
        print(f"  Prophet fits   : ~{n_fits}")
        print(f"  wall clock     : {elapsed:.1f}s "
              f"({elapsed / max(n_fits, 1):.2f}s per fit)")
        print(f"  EXTRAPOLATION to {n_keys} series: "
              f"{elapsed / max(len(cmp), 1) * n_keys / 60:.1f} min")
        print("\nSample mode: no CSV written, stubborn re-pull skipped.")
        return 0

    stubborn, detail = repull_stubborn(scoped, families, known_changeover)

    for path, frame in [
        (out / "phase0_before_after_cv.csv", cmp),
        (out / "phase0_stubborn_repull.csv", stubborn),
        (out / "phase0_stubborn_repull_detail.csv", detail),
    ]:
        frame.to_csv(path, index=False)
        print(f"\nWrote {path}  ({len(frame):,} rows)")

    print_comparison(cmp)
    print_stubborn(stubborn)
    print(f"\nTotal wall clock: {elapsed / 60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
