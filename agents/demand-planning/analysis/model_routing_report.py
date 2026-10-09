"""
Model routing across active scope — read-only.

Runner for `preprocessing.model_routing`. It rebuilds the frame `main.main()`
step 4 is handed, classifies every item in active scope, routes each one to
Prophet / a naive trailing average / Blocked, and reports the result. It writes
one CSV and changes nothing the pipeline owns: no config value moves, no existing output is
rewritten, nothing is fitted, and `main.py` is not touched.

It reuses `analysis.demand_classification_report.build_scope_input()` rather
than rebuilding the load → channel-mismatch → Amazon-filter → prepare → pool
sequence a second time. Two copies of that sequence would be two things to keep
in step with `main()`, and the routing verdict is only as trustworthy as its
agreement with what the pipeline actually does.

WHAT THE OUTPUT IS FOR
──────────────────────
Sheet 9 of the reporting workbook, as a file. Same decision, but reproducible
and carrying its own reasons — `block_reason` says WHY each unrouted item is
unrouted, and `n_qualifying_months` / `qualifying_basis` say what number the
gate was measured against.

Run:  python -m analysis.model_routing_report
      python -m analysis.model_routing_report --input-dir output_phase1
      python -m analysis.model_routing_report --db
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from analysis.demand_classification_report import build_scope_input  # noqa: E402
from preprocessing import demand_classification as dc  # noqa: E402
from preprocessing import model_routing as mr  # noqa: E402

# The independently derived expectation this run is checked against. Approximate
# by construction — it was assembled by hand from separate diagnostics, which is
# the thing model_routing exists to replace — so a small delta is a difference of
# method, and a large one is a finding.
EXPECTED_PROPHET_FITTED = 154   # currently-fitted Smooth + Erratic
EXPECTED_NAIVE_FITTED = 32      # currently-fitted Lumpy
EXPECTED_BLOCKED_FITTED = 0     # a fitted item should never come out Blocked

#: The naive expectation is checked on the two windows COMBINED. 32 was derived
#: when gate 7 had one label, and the 3mo/12mo split of it is new information
#: from analysis/croston_experiment.py — a target to observe, not to hit. The
#: split is printed below the check, unchecked, for exactly that reason.
NAIVE_ROUTES = (mr.NAIVE_SHORT, mr.NAIVE_LONG)


def print_report(routing: pd.DataFrame, classes: pd.DataFrame) -> None:
    sep = "=" * 96
    sub = "-" * 96

    print(f"\n{sep}\nSETUP\n{sep}")
    print(f"  items routed        : {len(routing):,} active-scope items over "
          f"{routing['family_key'].nunique():,} distinct fitted series")
    print(f"  Prophet gate        : MIN_TRAIN_MONTHS = {config.MIN_TRAIN_MONTHS} "
          f"CALENDAR months on the fit key's series")
    print(f"  naive-route gate    : CROSTON_MIN_OCCURRENCES = "
          f"{config.CROSTON_MIN_OCCURRENCES} NON-ZERO months  "
          f"** PROVISIONAL — a placeholder pending rolling-origin CV, not a "
          f"validated value **")
    print(f"  not included        : trend-disconnect status (both sides of it "
          f"route to Prophet); any Amazon rule other than "
          f"scope.detect_channel_mismatch()")

    # ── The headline table ────────────────────────────────────────────────────
    print(f"\n{sep}\nROUTE x CURRENTLY_FITTED\n{sep}")
    tab = pd.crosstab(routing["route"], routing["currently_fitted"])
    tab = tab.reindex([r for r in mr.ROUTES if r in tab.index], fill_value=0)
    tab = tab.rename(columns={True: "currently_fitted", False: "not_fitted"})
    for col in ("currently_fitted", "not_fitted"):
        if col not in tab.columns:
            tab[col] = 0
    tab = tab[["currently_fitted", "not_fitted"]]
    tab["total"] = tab.sum(axis=1)
    tab.loc["TOTAL"] = tab.sum()
    tab.index.name = "route"
    tab.columns.name = None
    print(tab.to_string())

    print(f"\n{sub}\n  Route x category — which pattern sent each item where\n{sub}")
    cat_tab = pd.crosstab(routing["category"], routing["route"])
    cat_tab = cat_tab.reindex([c for c in dc.CATEGORIES if c in cat_tab.index])
    for r in mr.ROUTES:
        if r not in cat_tab.columns:
            cat_tab[r] = 0
    print(cat_tab[list(mr.ROUTES)].to_string())

    print(f"\n{sub}\n  Block reasons\n{sub}")
    blocked = routing[routing["route"] == mr.BLOCKED]
    if blocked.empty:
        print("  (none)")
    else:
        reasons = (blocked.groupby("block_reason")
                   .agg(items=("item_code", "nunique"),
                        currently_fitted=("currently_fitted", "sum"))
                   .reset_index())
        reasons["clears_with_time"] = reasons["block_reason"].isin(
            mr.TEMPORARY_REASONS)
        print(reasons.to_string(index=False))

    # ── Sanity check ──────────────────────────────────────────────────────────
    fitted = routing[routing["currently_fitted"]]
    print(f"\n{sep}\nSANITY CHECK — currently-fitted items vs the independently "
          f"derived expectation\n{sep}")
    got = fitted["route"].value_counts()
    naive_actual = int(sum(got.get(r, 0) for r in NAIVE_ROUTES))
    check = pd.DataFrame([
        {"route": mr.PROPHET, "expected": EXPECTED_PROPHET_FITTED,
         "actual": int(got.get(mr.PROPHET, 0)),
         "delta": int(got.get(mr.PROPHET, 0)) - EXPECTED_PROPHET_FITTED},
        {"route": " + ".join(NAIVE_ROUTES), "expected": EXPECTED_NAIVE_FITTED,
         "actual": naive_actual, "delta": naive_actual - EXPECTED_NAIVE_FITTED},
        {"route": mr.BLOCKED, "expected": EXPECTED_BLOCKED_FITTED,
         "actual": int(got.get(mr.BLOCKED, 0)),
         "delta": int(got.get(mr.BLOCKED, 0)) - EXPECTED_BLOCKED_FITTED},
    ])
    print(check.to_string(index=False))
    print()
    print("  the naive split, reported not checked: "
          + ", ".join(f"{r} {int(got.get(r, 0))}" for r in NAIVE_ROUTES)
          + "  (window chosen by decline_ratio < 1.0)")

    fb = mr.fitted_but_blocked(routing)
    print(f"\n{sub}\n  Fitted BUT blocked — expected to be empty\n{sub}")
    if fb.empty:
        print("  (none) — every item the last run fitted still routes to a model.")
    else:
        print(f"  {len(fb)} item(s). Each one was fitted by the last real pipeline "
              f"run but is blocked by current data, which means something about "
              f"them\n  changed in between (left the active list, tipped over the "
              f"channel-mismatch thresholds, or lost history). Worth chasing "
              f"before moving on:\n")
        print(fb[["item_code", "family_key", "is_pooled", "category",
                  "near_threshold", "block_reason", "n_qualifying_months",
                  "qualifying_basis", "threshold_applied"]].to_string(index=False))

    # ── The 'once mature' population, by name ─────────────────────────────────
    om = mr.once_mature(routing)
    print(f"\n{sep}\nTHE 'ONCE MATURE' POPULATION — Smooth/Erratic, blocked only "
          f"by calendar history\n{sep}")
    if om.empty:
        print("  (none)")
    else:
        print(f"  {len(om)} item(s). The demand pattern is already the one "
              f"Prophet handles; the only thing missing is months on the clock.")
        print(f"  Nothing needs to be done to any of these — each starts getting "
              f"a forecast on its own once its fit key reaches "
              f"{config.MIN_TRAIN_MONTHS}\n  training months. `months_short` is "
              f"how many more it needs.\n")
        view = om[["item_code", "family_key", "is_pooled", "category",
                   "near_threshold", "n_qualifying_months"]].copy()
        view["months_short"] = (config.MIN_TRAIN_MONTHS
                                - view["n_qualifying_months"]).astype(int)
        # Non-zero months alongside the calendar count, from the classification:
        # the two differ, and the gap is how sparse a "short history" item is
        # underneath. E62001 has 12 of each; D25095 has 7 of each; an item with
        # 20 calendar months and 7 sales would be a different proposition.
        nz = classes.set_index("item_code")["n_nonzero_months"]
        view["n_nonzero_months"] = view["item_code"].map(nz)
        print(view.sort_values(["months_short", "item_code"]).to_string(index=False))

    # ── Near-threshold carried through ────────────────────────────────────────
    print(f"\n{sep}\nNEAR-THRESHOLD ROUTES — carried through, never applied\n{sep}")
    edge = routing[routing["near_threshold"]]
    if edge.empty:
        print("  (none)")
    else:
        print(f"  {len(edge)} item(s) sit within {dc.NEAR_THRESHOLD_PCT:.0%} of an "
              f"SBC cut-off. Their route is a close call: a nudge in ADI or CV^2 "
              f"would move them to\n  the other branch of the ladder. The flag "
              f"changed no route — it is on the row so the route is not read "
              f"with more confidence than it earns.\n")
        print(pd.crosstab(edge["category"], edge["route"]).to_string())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--db", action="store_true",
                    help="Reload raw inputs from Azure SQL instead of the cached CSVs")
    ap.add_argument("--input-dir", default=None,
                    help="Directory holding the cached input CSVs and receiving "
                         "this report's output (default: config.OUTPUT_DIR)")
    args = ap.parse_args()

    if args.input_dir:
        config.OUTPUT_DIR = Path(args.input_dir).resolve()
    out_dir = config.OUTPUT_DIR
    print(f"Input/output directory: {out_dir}")
    if not out_dir.exists():
        print(f"  ERROR: {out_dir} does not exist. Pass --input-dir, or --db to "
              f"reload from the database.")
        return 1

    pooled, families, cm_excluded, _fitted_now = build_scope_input(cached=not args.db)
    successor = pd.read_csv(out_dir / "successor_map.csv", dtype=str)

    classes = dc.classify_scope(
        pooled.data, pooled.active_products, families, pooled.eligible,
        channel_mismatch_excluded=cm_excluded,
    )

    routing = mr.route_scope(
        classes, pooled.data, pooled.active_products, families,
        channel_mismatch_excluded=cm_excluded,
        # From the LAST RUN's metrics, deliberately — see
        # model_routing.fitted_items_from_metrics().
        fitted_items=mr.fitted_items_from_metrics(out_dir),
        # Diagnostic only — surfaces items routed on their own short history
        # because family_pool declined to pool their family.
        successor_map=successor,
    )

    path = out_dir / "model_routing.csv"
    routing.to_csv(path, index=False)
    print(f"Wrote {path}  ({len(routing):,} rows)")

    print_report(routing, classes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
