"""
Demand-pattern (Syntetos-Boylan) classification of active scope — read-only.

Runner for `preprocessing.demand_classification`. It rebuilds the frame
`main.main()` step 4 is handed, classifies every item in active scope, and
reports the mix. It writes one CSV and changes nothing the pipeline owns: no
config value moves, no existing output is rewritten, nothing is fitted, nothing
goes to the database, and `main.py` is not touched.

WHY IT REBUILDS THE PIPELINE INSTEAD OF READING model_metrics.csv
─────────────────────────────────────────────────────────────────
`model_metrics.csv` only lists items that were FITTED — i.e. items that already
cleared `config.MIN_TRAIN_MONTHS`. The whole point of this classification is to
see the items that did not, since those are the candidates for a Croston/TSB
route. So the scope is rebuilt from the pipeline's own functions, in `main()`'s
own order, stopping one filter short:

    main.load_all  →  scope.detect_channel_mismatch
                   →  scope.filter_amazon_channel  →  prepare.prepare
                   →  family_pool.build_successor_families
                   →  family_pool.pool_for_training
                   →  [active-product + channel-mismatch filters only]

`scope.forecastable_series()` is deliberately NOT applied — it is the boundary
this report exists to look across. It is still evaluated, separately, to label
each item `is_fitted`, so the two cohorts can be read apart in the output.

THE ACCURACY CROSS-CHECK
────────────────────────
The last section joins `model_metrics.csv` and flags items whose classification
and whose measured accuracy disagree — a 'Smooth' item with a very high MAPE, or
a 'Lumpy' item with a very low one. Neither is a bug by itself: MAPE is unstable
at low volume and a Smooth series can still be forecast badly for reasons that
have nothing to do with its demand pattern. The flags are a reading list, not a
defect list, and both thresholds are stated in the output.

Run:  python -m analysis.demand_classification_report
      python -m analysis.demand_classification_report --input-dir output_phase1
      python -m analysis.demand_classification_report --db
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import main as pipeline  # noqa: E402  — reused for load_all()'s CSV-cache logic
from preprocessing import demand_classification as dc  # noqa: E402
from preprocessing import family_pool, prepare, scope  # noqa: E402

# ── Cross-check thresholds (judgment calls, not codebase values) ──────────────
# Stated in the console output so the flagged lists can be re-cut. A Smooth
# series is the one the pipeline should handle WELL, so a very high MAPE there
# is the surprising direction; a Lumpy series scoring very low is the other.
SMOOTH_MAPE_CONCERN = 50.0
LUMPY_MAPE_UNEXPECTEDLY_GOOD = 20.0


# ── Rebuild active scope, one filter short of the fitted set ──────────────────

def build_scope_input(cached: bool = True):
    """
    Reproduce `main.main()` steps 1-3b and return everything needed to classify.

    Returns (pooled, families, channel_mismatch_excluded, fitted_items):
        pooled     — the `PooledTrainingSet` `scope.apply_scope()` receives.
        families   — resolved SuccessorFamilies.
        channel_mismatch_excluded — item codes suppressed for channel mismatch.
        fitted_items — item codes that WOULD survive `forecastable_series()`,
                       i.e. the Prophet-fitted set, expanded back to real item
                       codes. Used only to label rows, never to filter them.
    """
    raw, bdm_df, active, _master, successor = pipeline.load_all(cached)

    # Channel-mismatch detection needs BOTH channels, so it runs on raw before
    # the Amazon filter — same order as main().
    channel_mismatch_excluded, _diag = scope.detect_channel_mismatch(
        raw, config.TEST_END)

    # Amazon rows must go before prepare(): `channel` does not survive its
    # groupby. The excluded frame is dropped on the floor — writing it is
    # main()'s job, not this report's.
    raw, _excluded = scope.filter_amazon_channel(raw)

    prepared = prepare.prepare(raw)
    families = family_pool.build_successor_families(successor)
    pooled = family_pool.pool_for_training(prepared, families, active, bdm_df)

    # What main() would actually fit, for the is_fitted label only.
    scoped = scope.apply_scope(
        pooled.data, pooled.active_products, pooled.bdm_forecasts, pilot=False,
        channel_mismatch_excluded=channel_mismatch_excluded,
    )
    fitted_items = set(family_pool.scope_item_codes(
        scoped, families, pooled.eligible)) - (channel_mismatch_excluded or set())

    return pooled, families, channel_mismatch_excluded, fitted_items


def post_anchor_activity(pooled_data: pd.DataFrame, classes: pd.DataFrame,
                         anchor: pd.Timestamp) -> pd.Series:
    """
    Non-zero months each item's fit-key series has AFTER the anchor.

    The classification is anchored at TRAIN_END by design, so a SKU launched in
    2026 correctly scores 'No Data'. That label would be badly misread as "dead
    product" without this: a new launch and a discontinued line are the same
    thing from the training window's point of view and opposite things from a
    routing decision's.
    """
    df = pooled_data.copy()
    df["item_code"] = df["item_code"].astype(str).str.strip()
    df["ds"] = pd.to_datetime(df["ds"])
    after = df[(df["ds"] > anchor) & (df["y"] > 0)]
    counts = after.groupby("item_code")["ds"].nunique()
    return classes["family_key"].map(counts).fillna(0).astype(int)


def load_metrics(out_dir: Path) -> pd.DataFrame:
    """`model_metrics.csv` from the last run, or empty if it is not there."""
    path = out_dir / "model_metrics.csv"
    if not path.exists():
        return pd.DataFrame()
    m = pd.read_csv(path)
    m["item_code"] = m["item_code"].astype(str).str.strip()
    for col in ("mape_pct", "wape_pct", "bias_pct"):
        if col in m.columns:
            m[col] = pd.to_numeric(m[col], errors="coerce")
    keep = [c for c in ["item_code", "mape_pct", "wape_pct", "bias_pct",
                        "n_train_months", "split_method"] if c in m.columns]
    return m[keep]


# ── Reporting ─────────────────────────────────────────────────────────────────

def _mix_table(frame: pd.DataFrame, by: str | None = None) -> pd.DataFrame:
    """Category counts in the module's canonical order, optionally split by."""
    cats = [c for c in dc.CATEGORIES]
    if by is None:
        counts = frame["category"].value_counts()
        return pd.DataFrame({"category": cats,
                             "items": [int(counts.get(c, 0)) for c in cats]})
    tab = (pd.crosstab(frame["category"], frame[by])
           .reindex(cats, fill_value=0).reset_index())
    tab["total"] = tab.drop(columns=["category"]).sum(axis=1)
    return tab


def print_report(classes: pd.DataFrame, metrics: pd.DataFrame) -> None:
    sep = "=" * 96
    sub = "-" * 96

    print(f"\n{sep}\nSETUP\n{sep}")
    print(f"  anchor                 : actuals <= {config.TRAIN_END} "
          f"(the same data the Prophet fit sees)")
    print(f"  thresholds             : ADI >= {config.SB_ADI_THRESHOLD}, "
          f"CV^2 >= {config.SB_CV2_THRESHOLD}, "
          f"min {config.SB_MIN_MONTHS_TO_CLASSIFY} non-zero months to classify")
    print(f"  series classified      : the FIT KEY's series — the pooled family "
          f"series for a pooled item, its own series otherwise")
    print(f"  scope                  : active-product allow-list minus "
          f"channel-mismatch exclusions; MIN_TRAIN_MONTHS "
          f"({config.MIN_TRAIN_MONTHS}) deliberately NOT applied")

    n = len(classes)
    n_fit = int(classes["is_fitted"].sum())
    n_series = classes["family_key"].nunique()
    print(f"\n  {n:,} active-scope items over {n_series:,} distinct series | "
          f"{n_fit:,} currently Prophet-fitted, {n - n_fit:,} not")

    print(f"\n{sep}\nFULL CATEGORY BREAKDOWN — all active-scope items\n{sep}")
    tab = _mix_table(classes, by="is_fitted")
    tab = tab.rename(columns={c: ("fitted" if c is True else "not_fitted")
                              for c in tab.columns if isinstance(c, bool)})
    for col in ("fitted", "not_fitted"):
        if col not in tab.columns:
            tab[col] = 0
    tab = tab[["category", "fitted", "not_fitted", "total"]]
    tab["pct_of_all"] = (tab["total"] / max(n, 1) * 100).round(1)
    print(tab.to_string(index=False))

    print(f"\n{sub}\n  Pooled vs standalone (pooled items share their family's "
          f"verdict — one series, one classification)\n{sub}")
    pool_tab = _mix_table(classes, by="is_pooled")
    pool_tab = pool_tab.rename(columns={c: ("pooled" if c is True else "standalone")
                                        for c in pool_tab.columns if isinstance(c, bool)})
    for col in ("pooled", "standalone"):
        if col not in pool_tab.columns:
            pool_tab[col] = 0
    print(pool_tab[["category", "pooled", "standalone", "total"]].to_string(index=False))

    # ── The sanity check the brief names ──────────────────────────────────────
    fitted = classes[classes["is_fitted"]]
    print(f"\n{sep}\nSANITY CHECK — fitted items vs the independently computed "
          f"expectation\n{sep}")
    expected = {dc.SMOOTH: 50, dc.ERRATIC: 104, dc.LUMPY: 32, dc.INTERMITTENT: 0}
    got = fitted["category"].value_counts()
    rows = []
    for cat in dc.CATEGORIES:
        rows.append({"category": cat, "expected": expected.get(cat, 0),
                     "actual": int(got.get(cat, 0)),
                     "delta": int(got.get(cat, 0)) - expected.get(cat, 0)})
    print(pd.DataFrame(rows).to_string(index=False))
    print(f"\n  Zero Intermittent among fitted items is EXPECTED: intermittent "
          f"demand rarely accumulates {config.MIN_TRAIN_MONTHS} real "
          f"transaction-months, so those series are")
    print(f"  filtered out by MIN_TRAIN_MONTHS before they can be fitted. The "
          f"not-fitted column above is where they should appear instead.")

    # ── The two non-answers, told apart ───────────────────────────────────────
    unclassified = classes[classes["category"].isin(dc.UNCLASSIFIED)]
    if not unclassified.empty and "post_anchor_months" in unclassified.columns:
        print(f"\n{sub}\n  The {len(unclassified)} unclassified item(s), split by "
              f"whether they sold AFTER the {config.TRAIN_END} anchor\n"
              f"  (the anchor is a training-window boundary, so a 2026 launch and "
              f"a discontinued line both read as 'No Data' —\n"
              f"   they are opposite things for a routing decision)\n{sub}")
        u = unclassified.copy()
        u["status"] = u["post_anchor_months"].gt(0).map(
            {True: "selling after anchor (new launch / recent)",
             False: "silent after anchor (dormant or discontinued)"})
        print(pd.crosstab(u["category"], u["status"]).to_string())
        print()
        print(u[["item_code", "category", "n_nonzero_months", "tenure_months",
                 "post_anchor_months"]].sort_values(
                     ["category", "post_anchor_months", "item_code"]).to_string(index=False))

    print(f"\n{sep}\nBORDERLINE — series within {dc.NEAR_THRESHOLD_PCT:.0%} of a "
          f"cut-off (the quadrant is a coin-flip, not a verdict)\n{sep}")
    edge = classes[classes["near_threshold"]]
    if edge.empty:
        print("  (none)")
    else:
        print(f"  {len(edge)} item(s) over "
              f"{edge['family_key'].nunique()} series. CV^2 uses population "
              f"stdev (ddof={dc.CV2_DDOF}); the sample estimator moves CV^2 by "
              f"under 1.2% on these\n  series, which is enough to flip anything "
              f"in this table. See preprocessing.demand_classification.CV2_DDOF.\n")
        print(edge[["item_code", "family_key", "is_pooled", "is_fitted",
                    "category", "adi", "cv2", "n_nonzero_months"]]
              .sort_values(["category", "item_code"]).to_string(index=False))

    print(f"\n{sep}\nADI / CV^2 DISTRIBUTION BY CATEGORY\n{sep}")
    stats = (classes[~classes["category"].isin(dc.UNCLASSIFIED)]
             .groupby("category")
             .agg(items=("item_code", "nunique"),
                  adi_min=("adi", "min"), adi_med=("adi", "median"),
                  adi_max=("adi", "max"),
                  cv2_min=("cv2", "min"), cv2_med=("cv2", "median"),
                  cv2_max=("cv2", "max"),
                  nz_min=("n_nonzero_months", "min"),
                  nz_med=("n_nonzero_months", "median"))
             .round(2).reset_index())
    print(stats.to_string(index=False))

    # ── Classification vs measured accuracy ───────────────────────────────────
    print(f"\n{sep}\nCLASSIFICATION vs MEASURED ACCURACY (model_metrics.csv)\n{sep}")
    if metrics.empty or "mape_pct" not in metrics.columns:
        print("  model_metrics.csv not found in the input directory — skipped.")
        return

    joined = classes.merge(metrics, on="item_code", how="inner")
    if joined.empty:
        print("  No overlap between the classified scope and model_metrics.csv.")
        return

    print(f"  {len(joined):,} items carry both a classification and a measured "
          f"test-window MAPE.\n")
    acc = (joined.groupby("category")
           .agg(items=("item_code", "nunique"),
                median_mape=("mape_pct", "median"),
                median_wape=("wape_pct", "median") if "wape_pct" in joined else ("mape_pct", "median"),
                worst_mape=("mape_pct", "max"))
           .round(1).reset_index())
    print(acc.to_string(index=False))

    print(f"\n{sub}\n  FLAGGED — classification and accuracy point different ways\n"
          f"  (thresholds are judgment calls: Smooth with MAPE > {SMOOTH_MAPE_CONCERN:g}%, "
          f"Lumpy with MAPE < {LUMPY_MAPE_UNEXPECTEDLY_GOOD:g}%.\n"
          f"   Neither is a defect on its own — MAPE is unstable at low volume, and "
          f"a Smooth series can be\n   forecast badly for reasons unrelated to its "
          f"demand pattern. This is a reading list.)\n{sub}")

    cols = [c for c in ["item_code", "family_key", "is_pooled", "category",
                        "adi", "cv2", "n_nonzero_months", "mape_pct",
                        "wape_pct", "bias_pct", "split_method"]
            if c in joined.columns]

    smooth_bad = joined[(joined["category"] == dc.SMOOTH)
                        & (joined["mape_pct"] > SMOOTH_MAPE_CONCERN)]
    print(f"\n  Smooth, but MAPE > {SMOOTH_MAPE_CONCERN:g}%  "
          f"({len(smooth_bad)} item(s)) — the pipeline should be handling these well:")
    print(smooth_bad[cols].sort_values("mape_pct", ascending=False).to_string(index=False)
          if not smooth_bad.empty else "    (none)")

    lumpy_good = joined[(joined["category"] == dc.LUMPY)
                        & (joined["mape_pct"] < LUMPY_MAPE_UNEXPECTEDLY_GOOD)]
    print(f"\n  Lumpy, but MAPE < {LUMPY_MAPE_UNEXPECTEDLY_GOOD:g}%  "
          f"({len(lumpy_good)} item(s)) — a re-route would have to beat this, "
          f"not just be theoretically better suited:")
    print(lumpy_good[cols].sort_values("mape_pct").to_string(index=False)
          if not lumpy_good.empty else "    (none)")

    unclassified_fitted = joined[joined["category"].isin(dc.UNCLASSIFIED)]
    if not unclassified_fitted.empty:
        print(f"\n  Fitted but UNCLASSIFIED ({len(unclassified_fitted)} item(s)) — a "
              f"series with fewer than {config.SB_MIN_MONTHS_TO_CLASSIFY} non-zero "
              f"months that still cleared MIN_TRAIN_MONTHS is a contradiction "
              f"worth reading:")
        print(unclassified_fitted[cols].to_string(index=False))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--db", action="store_true",
                    help="Reload raw inputs from Azure SQL instead of the cached CSVs")
    ap.add_argument("--input-dir", default=None,
                    help="Directory holding the cached input CSVs and receiving "
                         "this report's output (default: config.OUTPUT_DIR)")
    args = ap.parse_args()

    if args.input_dir:
        # Read-only redirection of the cache location: load_all() and this
        # module's own write both resolve through config.OUTPUT_DIR, and the
        # repo's live output directory is not always the one the last run left
        # behind. Nothing main.py owns is written either way.
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
    classes["is_fitted"] = classes["item_code"].isin(fitted_items)
    classes["post_anchor_months"] = post_anchor_activity(
        pooled.data, classes, dc.train_end_ts())

    metrics = load_metrics(out_dir)

    path = out_dir / "demand_classification.csv"
    classes.to_csv(path, index=False)
    print(f"Wrote {path}  ({len(classes):,} rows)")

    print_report(classes, metrics)
    return 0


if __name__ == "__main__":
    sys.exit(main())
