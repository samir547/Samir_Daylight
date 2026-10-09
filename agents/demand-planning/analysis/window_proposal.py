"""
Phase 3 — what the date window WOULD become if it rolled forward. Report only.

This script does not, and must not, edit config.py. Moving the forecast window
changes which months the business is asked to trust and invalidates the
currently-published forecast; that is a human decision. This produces the
evidence for it.

Writes output/proposed_window_report.md.

Run:  python -m analysis.window_proposal
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from analysis.month_completeness import (  # noqa: E402
    MIN_SEASONAL_TXN_RATIO, latest_complete_month, month_completeness,
)

# The window is five constants but only one degree of freedom: every other date
# is a fixed offset from TEST_END in today's config. Preserving these offsets is
# what "roll the window forward" means.
#   TRAIN_END      = TEST_END - 6   (2025-10 <- 2026-04)
#   TEST_START     = TEST_END - 5   (2025-11)
#   FORECAST_START = TEST_END + 1   (2026-05)
#   FORECAST_END   = TEST_END + 6   (2026-10)
# Single source of truth lives in config.py (it derives the static constants
# from the same table), so this module and config can no longer disagree.
OFFSETS_FROM_TEST_END = config.OFFSETS_FROM_TEST_END


def _shift(ym: str, months: int) -> str:
    return (pd.Timestamp(ym + "-01") + pd.DateOffset(months=months)).strftime("%Y-%m")


def _months(start: str, end: str) -> list[str]:
    return [d.strftime("%Y-%m")
            for d in pd.date_range(start + "-01", end + "-01", freq="MS")]


def verify_offsets() -> list[str]:
    """Confirm today's config really does follow OFFSETS_FROM_TEST_END.

    If someone has already hand-edited one constant out of step, the "same
    relative offsets" premise is false and the proposal below would silently
    encode the drift. Better to say so than to propose dates built on it.
    """
    problems = []
    for name, off in OFFSETS_FROM_TEST_END.items():
        expected = _shift(config.TEST_END, off)
        actual = getattr(config, name)
        if expected != actual:
            problems.append(f"config.{name} is {actual}, but TEST_END{off:+d} = {expected}")
    return problems


def propose(anchor: str) -> dict[str, str]:
    """The five window constants implied by anchoring TEST_END on `anchor`."""
    return {name: _shift(anchor, off) for name, off in OFFSETS_FROM_TEST_END.items()}


def bdm_coverage(bdm_df: pd.DataFrame, months: list[str],
                 scope_items: set[str]) -> pd.DataFrame:
    """
    Per-month BDM coverage for a candidate benchmark window.

    `evaluation.benchmark._bdm_month_col()` now keys each BDM row's month off
    its OWN `forecast_year` column rather than a single hand-maintained
    constant, so a month here is "OK" purely on whether the sheet has ANY row
    for it — there is no more "which single year is covered" question, now that
    the sheet is read in full (queries.BDM_FORECASTS / loader.load_bdm_forecasts
    no longer filter to one year).
    """
    from evaluation import benchmark as bm

    bdm = bdm_df.copy()
    bdm["item_code"] = bdm["item_code"].astype(str).str.strip()
    bdm["bdm_forecast_qty"] = pd.to_numeric(bdm["bdm_forecast_qty"], errors="coerce")
    mapped = bm._bdm_month_col(bdm)
    by_month = mapped.groupby("month")["item_code"].agg(set)

    rows = []
    for m in months:
        items = by_month.get(m, set())
        hit = items & scope_items
        rows.append({
            "month": m,
            "bdm_items": len(items),
            "in_scope_items_covered": len(hit),
            "in_scope_items": len(scope_items),
            "coverage_pct": round(100 * len(hit) / len(scope_items), 1) if scope_items else 0.0,
            "status": "OK" if hit else f"EMPTY — no BDM data for {m}",
        })
    return pd.DataFrame(rows)


def _fmt_table(df: pd.DataFrame) -> str:
    """Minimal markdown table."""
    head = "| " + " | ".join(df.columns) + " |"
    rule = "| " + " | ".join("---" for _ in df.columns) + " |"
    body = ["| " + " | ".join(str(v) for v in row) + " |"
            for row in df.itertuples(index=False)]
    return "\n".join([head, rule, *body])


def build_report() -> str:
    out = config.OUTPUT_DIR
    raw = pd.read_csv(out / "raw_data.csv", dtype=str)
    bdm_df = pd.read_csv(out / "bdm_forecasts.csv", dtype=str)
    active = pd.read_csv(out / "active_products.csv", dtype=str)

    anchor, evidence = latest_complete_month(raw)
    max_month = evidence["month"].max()
    proposed = propose(anchor)
    current = {k: getattr(config, k) for k in OFFSETS_FROM_TEST_END}
    offset_problems = verify_offsets()

    scope_items = set(active["item_code"].astype(str).str.strip())

    from evaluation import benchmark as bm
    bdm_months = bm.bdm_coverage_months(bdm_df)

    cur_bench = _months(config.BENCHMARK_START, config.BENCHMARK_END)
    cur_test = _months(config.TEST_START, config.TEST_END)
    # Benchmark window = the part of the test window the BDM sheet can reach.
    # "Can reach" now means "has at least one real row for that month" rather
    # than "falls inside config.BDM_FORECAST_YEAR", which no longer exists.
    prop_test = _months(proposed["TEST_START"], proposed["TEST_END"])
    bdm_months_set = set(bdm_months)
    prop_bench = [m for m in prop_test if m in bdm_months_set]

    cur_test_cov = bdm_coverage(bdm_df, cur_test, scope_items)
    cur_cov = bdm_coverage(bdm_df, cur_bench, scope_items)
    prop_cov = bdm_coverage(bdm_df, prop_test, scope_items)
    fwd_cov = bdm_coverage(
        bdm_df, _months(proposed["FORECAST_START"], proposed["FORECAST_END"]), scope_items)

    tail = evidence[evidence["month"] >= _shift(max_month, -11)][
        ["month", "rows", "items", "txns", "qty", "yoy_txn_ratio",
         "seasonal_txn_ratio", "item_ratio", "regular_cover", "is_complete", "reason"]
    ]
    rejected = evidence[(~evidence["is_complete"]) & (evidence["month"] > anchor)]

    cmp_rows = pd.DataFrame([
        {"constant": k, "current": current[k], "proposed": proposed[k],
         "shift_months": round(
             (pd.Timestamp(proposed[k] + "-01").to_period("M")
              - pd.Timestamp(current[k] + "-01").to_period("M")).n)}
        for k in ["TRAIN_END", "TEST_START", "TEST_END", "FORECAST_START", "FORECAST_END"]
    ])

    lines = [
        "# Proposed forecast window — recommendation only",
        "",
        "**Nothing in `config.py` has been changed by this analysis, and nothing should be "
        "changed as a side effect of reading it.** Rolling the window forward "
        "invalidates the currently-published `forecast_may_oct_2026.csv` and changes "
        "which months the business is being asked to trust. That is a call for a human, "
        "not for the pipeline. This document is the evidence for making it.",
        "",
        f"_Generated from `output/raw_data.csv` (extract max month **{max_month}**)._",
        "",
        "---",
        "",
        "## 1. The proposal",
        "",
        f"Latest **complete** month in `forecast_training_data`: **{anchor}** "
        f"(extract runs to {max_month}; see §2 for why the last month is not it).",
        "",
        "Anchoring `TEST_END` on that month and preserving today's offsets exactly:",
        "",
        _fmt_table(cmp_rows),
        "",
        f"The whole window moves forward **{cmp_rows['shift_months'].iloc[0]} months**. "
        f"That recovers {cmp_rows['shift_months'].iloc[0]} months of actuals that the "
        "current config discards, and — more importantly — it stops the pipeline "
        "presenting already-elapsed months as a forward forecast.",
        "",
        "### Why `TEST_END` is the anchor, not `TRAIN_END`",
        "",
        f"Anchoring `TRAIN_END` on {anchor} instead would put the test window at "
        f"{_shift(anchor, 1)}..{_shift(anchor, 6)} — months with no actuals yet. The test "
        "window has to be scoreable, so the newest complete month is the newest possible "
        "`TEST_END`, and everything else follows from it. This is the anchoring that uses "
        "every available actual while keeping a full 6-month held-out window.",
        "",
        "### What this does NOT fix",
        "",
        "Rolling the window forward **does not remove the split-ratio look-ahead**. It only "
        "moves where `TRAIN_END` sits. A single mix ratio computed from data running to "
        "`TEST_END` would peek into the scored window at 2026-06 exactly as it did at "
        "2026-04 — the same bug, relocated. What actually fixes it is the two-ratio split "
        "in `preprocessing/family_pool.py` (`test_ratio` / `forward_ratio`), which is "
        "already in place and is independent of this proposal. **Adopt or reject the window "
        "change on its own merits; the look-ahead fix stands either way.**",
        "",
        "---",
        "",
        "## 2. Completeness evidence — why the extract's last month is not the anchor",
        "",
        f"`MAX(year_month)` is **{max_month}**, and taking it at face value is the failure "
        "mode this check exists to prevent. The upstream pull lands mid-month, so the "
        "newest month is routinely present but under-filled. Rolling `TRAIN_END` onto a "
        "partial month would train every series with a fabricated cliff at its most recent "
        "point — the region Prophet's trend fit is most sensitive to.",
        "",
    ]

    if not rejected.empty:
        lines += [
            f"**{len(rejected)} month{'s' if len(rejected) > 1 else ''} after {anchor} "
            f"{'were' if len(rejected) > 1 else 'was'} rejected:**",
            "",
            _fmt_table(rejected[["month", "rows", "items", "txns",
                                 "seasonal_txn_ratio", "regular_cover", "reason"]]),
            "",
        ]

    assessed = evidence.dropna(subset=["seasonal_txn_ratio"])
    worst_genuine = assessed.loc[assessed["is_complete"], "seasonal_txn_ratio"].min()
    n_flagged = int((~assessed["is_complete"]).sum())
    lines += [
        "The test compares each month to the **same calendar month a year earlier** "
        "(cancelling seasonality) divided by the business's overall growth level, using "
        "only windows strictly before the candidate. A plain \"vs. the prior 6 months\" "
        "threshold was tried first and rejected: this business has a hard Apr–Jun trough, "
        "so genuine fully-loaded months sit at 52–70% of their own trailing median and "
        "that rule flagged 11 real months alongside the one true partial.",
        "",
        f"Across {assessed['month'].min()}..{max_month} — the "
        f"{len(assessed)} months with enough prior history to assess (the signal needs 24 "
        f"months of run-up, so {evidence['month'].min()}..{_shift(assessed['month'].min(), -1)} "
        f"is not scored) — the adopted test flags "
        f"**{n_flagged} month{'s' if n_flagged != 1 else ''}**. The lowest-scoring month it "
        f"accepts is {worst_genuine:.2f}; {max_month} scores "
        f"{evidence.loc[evidence['month'] == max_month, 'seasonal_txn_ratio'].iloc[0]:.2f}. "
        f"The threshold ({MIN_SEASONAL_TXN_RATIO}) sits in that gap, and no month in the "
        "assessed history falls between the two.",
        "",
        f"### Trailing 12 months",
        "",
        _fmt_table(tail.round(3)),
        "",
        f"**{anchor} is trustworthy as the new `TEST_END`:** transactions "
        f"{evidence.loc[evidence['month'] == anchor, 'seasonal_txn_ratio'].iloc[0]:.2f}x "
        "seasonal expectation, "
        f"{evidence.loc[evidence['month'] == anchor, 'item_ratio'].iloc[0]:.2f}x the "
        "trailing median item count, and "
        f"{evidence.loc[evidence['month'] == anchor, 'regular_cover'].iloc[0]:.0%} of its "
        "regular-trading SKUs present.",
        "",
        "---",
        "",
        "## 3. BDM benchmark coverage",
        "",
        "`benchmark.build_comparison()` keys the BDM sheet's `Jan`..`Dec` columns off "
        "each row's own `forecast_year` (via `_bdm_month_col`), so the sheet's multiple "
        "planning years (2026 and 2027, as of 2026-08) are all usable — there is no "
        "longer a single hand-maintained year limiting which months can match. A "
        "benchmark month with genuinely no BDM row behind it still joins to nothing and "
        "is **dropped silently** — it surfaces as a short or empty benchmark table, not "
        "as an error — which is what the coverage tables below are for.",
        "",
        f"### Current TEST window — {config.TEST_START}..{config.TEST_END}",
        "",
        "This is where the gap actually is today, and it is the reason "
        "`BENCHMARK_START` exists as a separate constant:",
        "",
        _fmt_table(cur_test_cov),
        "",
        f"### Current BENCHMARK window — {config.BENCHMARK_START}..{config.BENCHMARK_END}",
        "",
        "`BENCHMARK_START`/`END` already clip the test window down to the months the "
        "sheet can reach, so the benchmark that actually runs today is clean — the "
        f"{len(cur_test_cov[cur_test_cov['status'] != 'OK'])} uncovered month(s) above are "
        "discarded before `build_comparison()` ever sees them:",
        "",
        _fmt_table(cur_cov),
        "",
        f"### Proposed window — test window {proposed['TEST_START']}..{proposed['TEST_END']}",
        "",
        _fmt_table(prop_cov),
        "",
    ]

    bad_cur_test = cur_test_cov[cur_test_cov["status"] != "OK"]
    bad_cur = cur_cov[cur_cov["status"] != "OK"]
    bad_prop = prop_cov[prop_cov["status"] != "OK"]
    lines += [
        "**Finding — neither window hits an unhandled gap, but for different reasons.**",
        "",
        (f"- The current **test** window has {len(bad_cur_test)} month(s) the BDM sheet "
         f"has no data for ({', '.join(bad_cur_test['month'])}). This is already handled: "
         "`BENCHMARK_START`/`BENCHMARK_END` clip them off."
         if not bad_cur_test.empty else
         "- The current **test** window is fully covered by the BDM sheet; "
         "nothing is clipped."),
        (f"- The current **benchmark** window is fully covered — {len(cur_cov)} months, all "
         f"joining to BDM data at ≥{cur_cov['coverage_pct'].min():.0f}% of in-scope items."
         if bad_cur.empty else
         f"- The current **benchmark** window has {len(bad_cur)} uncovered month(s): "
         f"{', '.join(bad_cur['month'])} — these produce empty comparison rows."),
        (f"- The **proposed** test window is fully covered — all {len(prop_cov)} months have "
         f"real BDM data behind them, at ≥{prop_cov['coverage_pct'].min():.0f}% of "
         "in-scope items. **No clipping needed and no empty-benchmark risk.**"
         if bad_prop.empty else
         f"- The **proposed** test window has **{len(bad_prop)} uncovered month(s)**: "
         f"{', '.join(bad_prop['month'])} — `build_comparison()` would silently drop them."),
        "",
        (
            f"The proposed window would in fact **improve** the benchmark: "
            f"`BENCHMARK_START`/`BENCHMARK_END` could widen from "
            f"{config.BENCHMARK_START}..{config.BENCHMARK_END} ({len(cur_bench)} months) to "
            f"{prop_bench[0]}..{prop_bench[-1]} ({len(prop_bench)} months), since every "
            "month in the proposed test window now has real BDM data behind it — the sheet "
            "carries multiple planning years at once, so there is no year boundary to run "
            "past any more. Note `BENCHMARK_START`/`BENCHMARK_END` are **not** in the offset "
            "set above; `--auto-window` computes them separately, as the overlap between the "
            "rolled TEST window and whatever the BDM sheet actually covers — see "
            "main.apply_auto_window()."
            if prop_bench else
            "The proposed test window has **no** BDM coverage at all right now — widening "
            "BENCHMARK_START/END to match it would produce an empty benchmark, not a wider "
            "one. Re-run this report once the BDM sheet has been extended to cover the "
            "proposed window."
        ),
        "",
        "### The cliff after this one",
        "",
        (
            f"The BDM sheet's actual coverage today runs **{min(bdm_months)}.."
            f"{max(bdm_months)}**. The proposed `FORECAST_END` is "
            f"**{proposed['FORECAST_END']}**, "
            + (
                "still inside that range, so the benchmark has real data to compare "
                "against through this window."
                if bdm_months and proposed["FORECAST_END"] <= max(bdm_months) else
                "past the sheet's own coverage. **A future roll-forward will eventually "
                "outrun whatever the sheet currently holds, and the benchmark will go "
                "empty for those months** — no longer because of a hardcoded year "
                "(queries.BDM_FORECASTS / loader.load_bdm_forecasts() now pull every "
                "planning year present), but because the sheet itself has not been "
                "extended that far. That is a hard dependency on an input this pipeline "
                "does not own, and evaluation.benchmark.check_bdm_coverage() will flag it "
                "per missing month when it happens, rather than silently. Worth raising "
                "with whoever maintains the BDM sheet before that cycle arrives."
            )
        ),
        "",
        "For reference, BDM coverage of the proposed **forward** window "
        f"({proposed['FORECAST_START']}..{proposed['FORECAST_END']}) — not used by the "
        "benchmark, but it is what the forecast would be delivered against:",
        "",
        _fmt_table(fwd_cov),
        "",
        "---",
        "",
        "## 4. Other things a window change would touch",
        "",
        f"- **`prepare()` truncates at `TEST_END`.** Moving it to {proposed['TEST_END']} "
        f"pulls {cmp_rows['shift_months'].iloc[0]} more months into `prepared`, which is "
        "also the source of the naive baseline and of `forward_ratio`. Both improve; "
        "neither needs a code change.",
        f"- **`ACTIVE_SINCE` is {config.ACTIVE_SINCE}** and is not part of the offset set. "
        "It is a recency gate on the active-product list, not a window boundary, so it "
        "does not move automatically — but it drifts further into the past with every "
        "roll and is worth a look.",
        "- **The published forecast changes.** `forecast_may_oct_2026.csv` becomes "
        f"`{proposed['FORECAST_START']}..{proposed['FORECAST_END']}`, and the filename "
        "(hard-coded in `main.py`) would no longer describe its contents.",
        "- **Re-run required.** Every output artifact is window-dependent; none can be "
        "carried over.",
        "",
    ]

    if offset_problems:
        lines += [
            "---", "",
            "## ⚠ Offset premise check FAILED", "",
            "This proposal assumes today's constants follow one consistent offset set. "
            "They do not:", "",
            *[f"- {p}" for p in offset_problems], "",
            "Resolve this before using the proposed dates — they were derived from the "
            "offsets above and would encode the drift.", "",
        ]
    else:
        lines += [
            "---", "",
            "_Offset premise verified: all five of today's constants follow the offset set "
            "used to derive the proposal._", "",
        ]

    return "\n".join(lines)


def main() -> int:
    report = build_report()
    path = config.OUTPUT_DIR / "proposed_window_report.md"
    path.write_text(report, encoding="utf-8")
    print(f"Wrote {path}  ({len(report.splitlines())} lines)")

    raw = pd.read_csv(config.OUTPUT_DIR / "raw_data.csv", dtype=str)
    anchor, ev = latest_complete_month(raw)
    print(f"\nExtract max month     : {ev['month'].max()}")
    print(f"Latest COMPLETE month : {anchor}")
    print(f"Proposed window       : {propose(anchor)}")
    print(f"config.py unchanged   : TRAIN_END={config.TRAIN_END} "
          f"TEST_END={config.TEST_END} FORECAST_END={config.FORECAST_END}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
