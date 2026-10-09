"""
Out-of-sample scoring of the delivered forward forecast (Phase 1 — analysis only).

WHY THIS IS THE HONEST NUMBER
─────────────────────────────
`model_metrics.csv` scores the Nov 2025 – Apr 2026 test window. For a *split*
successor family those numbers are mildly optimistic, because the mix ratio used
to break the pooled family forecast back into item codes is computed from
actuals that run up to `TEST_END` — i.e. from inside the window being scored.
(That is what family_pool's `test_ratio` / `forward_ratio` split fixes.)

The forward forecast has no such problem. `prepare()` truncates at `TEST_END`
(2026-04), so May/Jun/Jul 2026 actuals were *not* in the data when
`forecast_may_oct_2026.csv` was produced — not in the fit, not in the split
ratios, not anywhere. Scoring against them is a genuine out-of-sample test, and
it is the only clean read we have on whether the split methodology itself holds
up as opposed to the window question.

PARTIAL-MONTH GUARD
───────────────────
The raw extract's last month is partial (2026-07 carries ~42% of a normal
month's transactions). Scoring a partial month as if it were a real miss would
manufacture a huge false over-forecast bias, so months that fail the
completeness check are written to the CSV but EXCLUDED from the headline
summary. Both cuts are reported.

Run:  python -m analysis.oos_scoring        (reads cached CSVs in output/)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import main as main_pipeline  # noqa: E402  — for the window-derived forecast filename
from analysis.month_completeness import month_completeness  # noqa: E402

FORECAST_MONTHS = ["2026-05", "2026-06", "2026-07"]

# Cohort labels.
COHORT_SINGLETON = "singleton"          # never pooled — plain per-item Prophet fit
COHORT_RENAME = "pooled_rename"         # pooled 1-for-1; forecast passed through whole
COHORT_SPLIT = "split_family"           # pooled AND divided by a mix ratio


def _actuals(raw: pd.DataFrame) -> pd.DataFrame:
    """item_code x month actuals, Amazon-filtered to match the training basis."""
    df = raw.copy()
    df["monthly_qty"] = pd.to_numeric(df["monthly_qty"], errors="coerce").fillna(0.0)
    df = df[df["channel"].astype(str).str.strip() != "Amazon"]
    df["item_code"] = df["item_code"].astype(str).str.strip()
    df["ds"] = df["year_month"].astype(str).str.strip()
    return (
        df.groupby(["item_code", "ds"], as_index=False)["monthly_qty"]
        .sum()
        .rename(columns={"monthly_qty": "actual"})
    )


def _cohorts(ratios: pd.DataFrame) -> pd.DataFrame:
    """item_code → cohort, derived from output/successor_split_ratios.csv.

    A family that resolved to a single current code (`split_method='na'`) was
    pooled for the fit but never divided, so it carries no ratio risk; it is
    reported separately from the genuinely split families rather than being
    lumped in with either side.
    """
    if ratios.empty:
        return pd.DataFrame(columns=["item_code", "cohort", "family_key", "split_method"])
    out = ratios.copy()
    out["item_code"] = out["item_code"].astype(str).str.strip()

    # The forward forecast is split by `forward_ratio`, so that is the ratio this
    # scoring must attribute error to. Fall back to the legacy `split_ratio`
    # column for ratio files written before the two-ratio change (they are the
    # same number — the old single ratio had no cutoff either).
    if "forward_ratio" in out.columns:
        out["split_ratio"] = out["forward_ratio"]
        out["split_method"] = out["forward_split_method"]
    n_codes = out.groupby("family_key")["item_code"].transform("nunique")
    out["cohort"] = np.where(n_codes > 1, COHORT_SPLIT, COHORT_RENAME)
    return out[["item_code", "cohort", "family_key", "split_method", "split_ratio"]]


def score(raw: pd.DataFrame, forecast: pd.DataFrame, ratios: pd.DataFrame,
          complete: pd.DataFrame) -> pd.DataFrame:
    """One row per item-month: forecast, actual, and the error decomposition."""
    fc = forecast.copy()
    fc["item_code"] = fc["item_code"].astype(str).str.strip()
    fc["ds"] = fc["ds"].astype(str).str.strip()
    fc = fc[fc["ds"].isin(FORECAST_MONTHS)]
    fc = fc[["item_code", "ds", "yhat", "yhat_lower", "yhat_upper", "family"]]

    act = _actuals(raw)
    df = fc.merge(act, on=["item_code", "ds"], how="left")

    # A missing row means the item simply did not sell that month, not missing
    # data — forecast_training_data has no zero-quantity rows. Recorded as an
    # observed zero, and flagged, because a zero actual is excluded from MAPE.
    df["actual_observed"] = df["actual"].notna()
    df["actual"] = df["actual"].fillna(0.0)

    coh = _cohorts(ratios)
    df = df.merge(coh, on="item_code", how="left")
    df["cohort"] = df["cohort"].fillna(COHORT_SINGLETON)
    df["split_method"] = df["split_method"].fillna("na")
    df["family_key"] = df["family_key"].fillna(df["item_code"])

    df["error"] = df["yhat"] - df["actual"]
    df["abs_error"] = df["error"].abs()
    df["ape_pct"] = np.where(
        df["actual"] != 0, df["abs_error"] / df["actual"].replace(0, np.nan) * 100, np.nan
    ).round(2)
    df["in_interval"] = (df["actual"] >= df["yhat_lower"]) & (df["actual"] <= df["yhat_upper"])

    comp = complete.set_index("month")["is_complete"]
    df["month_complete"] = df["ds"].map(comp).fillna(False)

    cols = ["item_code", "ds", "month_complete", "cohort", "family_key", "split_method",
            "split_ratio", "family", "yhat", "yhat_lower", "yhat_upper", "actual",
            "actual_observed", "error", "abs_error", "ape_pct", "in_interval"]
    return df[cols].sort_values(["ds", "cohort", "item_code"]).reset_index(drop=True)


def summarize(scored: pd.DataFrame, label: str) -> pd.DataFrame:
    """Cohort-level MAPE / WAPE / bias. Same metric definitions as evaluation.metrics."""
    rows = []
    for cohort, grp in list(scored.groupby("cohort")) + [("ALL", scored)]:
        nz = grp[grp["actual"] != 0]
        denom = grp["actual"].sum()
        rows.append({
            "window": label,
            "cohort": cohort,
            "n_items": grp["item_code"].nunique(),
            "n_item_months": len(grp),
            "n_scorable_mape": len(nz),          # non-zero actuals only
            "total_actual": round(grp["actual"].sum(), 1),
            "total_yhat": round(grp["yhat"].sum(), 1),
            "mape_pct": round(nz["ape_pct"].mean(), 2) if len(nz) else np.nan,
            "median_ape_pct": round(nz["ape_pct"].median(), 2) if len(nz) else np.nan,
            "wape_pct": round(grp["abs_error"].sum() / denom * 100, 2) if denom else np.nan,
            "bias_pct": round(grp["error"].sum() / denom * 100, 2) if denom else np.nan,
            "coverage_pct": round(grp["in_interval"].mean() * 100, 1),
        })
    return pd.DataFrame(rows)


def decompose_split_families(scored: pd.DataFrame) -> pd.DataFrame:
    """
    Separate the two things that can go wrong for a split family.

    A split family's item-level error has exactly two sources:

      1. the POOLED FAMILY TOTAL that Prophet predicted, and
      2. the MIX RATIO used to divide that total between the current codes.

    They are separable. Because a family's ratios sum to 1, the split is
    bias-neutral by construction — item-level bias always equals family-level
    bias, so bias alone says nothing about the ratio. The ratio's real cost
    shows up in WAPE, and is measured here by re-splitting the same family
    total with the PERFECT hindsight share (each code's actual share of realised
    family demand) and asking how much WAPE that recovers.

    Whatever WAPE remains under the perfect ratio is the family-level forecast's
    own error and cannot be fixed by any change to the ratio.
    """
    sf = scored[(scored["cohort"] == COHORT_SPLIT) & scored["month_complete"]].copy()
    if sf.empty:
        return pd.DataFrame()

    # Recover the family total Prophet actually predicted (yhat = total x ratio).
    sf["family_yhat"] = sf["yhat"] / sf["split_ratio"].replace(0, np.nan)
    fam_actual = sf.groupby(["family_key", "ds"])["actual"].transform("sum")
    sf["perfect_share"] = np.where(fam_actual > 0, sf["actual"] / fam_actual, sf["split_ratio"])
    sf["yhat_perfect_ratio"] = sf["family_yhat"] * sf["perfect_share"]

    denom = sf["actual"].sum()
    as_is = (sf["yhat"] - sf["actual"]).abs().sum() / denom * 100
    perfect = (sf["yhat_perfect_ratio"] - sf["actual"]).abs().sum() / denom * 100

    fam = sf.groupby(["family_key", "ds"], as_index=False).agg(
        actual=("actual", "sum"), yhat=("yhat", "sum"))
    fam_wape = (fam["yhat"] - fam["actual"]).abs().sum() / fam["actual"].sum() * 100

    return pd.DataFrame([
        {"basis": "as delivered (computed split ratio)", "wape_pct": round(as_is, 1),
         "note": "what the business received"},
        {"basis": "same family total, PERFECT hindsight ratio", "wape_pct": round(perfect, 1),
         "note": "best any ratio could ever do"},
        {"basis": "  -> attributable to the ratio", "wape_pct": round(as_is - perfect, 1),
         "note": "the split methodology's own cost"},
        {"basis": "  -> attributable to the family-level fit", "wape_pct": round(perfect, 1),
         "note": "irreducible by any ratio change"},
        {"basis": "family total vs family actual (ratio removed)", "wape_pct": round(fam_wape, 1),
         "note": "the pooled Prophet forecast on its own terms"},
    ])


def main() -> int:
    out = config.OUTPUT_DIR
    raw = pd.read_csv(out / "raw_data.csv", dtype=str)
    # The forward-forecast filename tracks the window (main.forecast_filename).
    # The legacy fixed name is still accepted so this script can score a
    # forecast published before the window rolled forward.
    fc_path = out / main_pipeline.forecast_filename()
    if not fc_path.exists():
        legacy = out / "forecast_may_oct_2026.csv"
        if not legacy.exists():
            raise FileNotFoundError(
                f"No forward forecast to score: neither {fc_path.name} nor "
                f"{legacy.name} exists in {out}/. Run `python main.py` first."
            )
        print(f"NOTE: {fc_path.name} not found — scoring the legacy "
              f"{legacy.name}, which was published under a different window.")
        fc_path = legacy
    forecast = pd.read_csv(fc_path, dtype={"ds": str})
    ratios = pd.read_csv(out / "successor_split_ratios.csv")

    complete = month_completeness(raw)
    scored = score(raw, forecast, ratios, complete)

    path = out / "oos_scoring_may_jul_2026.csv"
    scored.to_csv(path, index=False)
    print(f"Wrote {path}  ({len(scored):,} item-months)\n")

    print("-- Month completeness (ex-Amazon) " + "-" * 40)
    print(complete[complete["month"] >= "2026-01"].to_string(index=False))

    good = sorted(scored.loc[scored["month_complete"], "ds"].unique())
    bad = sorted(scored.loc[~scored["month_complete"], "ds"].unique())
    print(f"\nComplete months scored: {good}")
    print(f"Partial months EXCLUDED from headline: {bad}\n")

    primary = summarize(scored[scored["month_complete"]], "+".join(good))
    print("-- HEADLINE: complete months only " + "-" * 39)
    print(primary.to_string(index=False))

    allm = summarize(scored, "+".join(sorted(scored["ds"].unique())) + " (incl. partial)")
    print("\n-- Reference: all months incl. partial " + "-" * 34)
    print(allm.to_string(index=False))

    print("\n-- Per-month, per-cohort bias " + "-" * 43)
    per_month = []
    for (ds, cohort), grp in scored.groupby(["ds", "cohort"]):
        denom = grp["actual"].sum()
        per_month.append({
            "ds": ds, "cohort": cohort, "complete": bool(grp["month_complete"].iloc[0]),
            "n": len(grp), "actual": round(denom, 1), "yhat": round(grp["yhat"].sum(), 1),
            "bias_pct": round(grp["error"].sum() / denom * 100, 1) if denom else np.nan,
            "mape_pct": round(grp.loc[grp["actual"] != 0, "ape_pct"].mean(), 1),
        })
    print(pd.DataFrame(per_month).to_string(index=False))

    print("\n-- Split families: is it the RATIO or the FAMILY FORECAST? " + "-" * 14)
    dec = decompose_split_families(scored)
    if not dec.empty:
        print(dec.to_string(index=False))

    print("\n-- Split families, per family " + "-" * 43)
    sf = scored[(scored["cohort"] == COHORT_SPLIT) & scored["month_complete"]]
    if not sf.empty:
        fam = sf.groupby(["family_key", "item_code", "split_method"]).apply(
            lambda g: pd.Series({
                "n": len(g), "actual": round(g["actual"].sum(), 1),
                "yhat": round(g["yhat"].sum(), 1),
                "bias_pct": round(g["error"].sum() / g["actual"].sum() * 100, 1)
                if g["actual"].sum() else np.nan,
                "mape_pct": round(g.loc[g["actual"] != 0, "ape_pct"].mean(), 1),
            }), include_groups=False
        ).reset_index()
        print(fam.to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
