"""
Calendar vs occurrence-based split-ratio windows (validation pass - read-only).

OUTCOME: NEGATIVE - THE CHANGE WAS MEASURED, THEN ABANDONED
───────────────────────────────────────────────────────────
Window length is not the constraint; pre-test-boundary sales VOLUME is. The
occurrence window changed nothing for any pooled family in the dataset: the two
affected families it was written for (A35030 among them) have current codes with
exactly one non-zero month each before the test-window boundary, and no lookback
of any length can find sales that do not exist. Both families' codes are routed
to Croston/TSB by demand pattern regardless, so even a working fix would not have
reached the Prophet-fitted side. A secondary idea - lowering MIN_RATIO_MONTHS
rather than extending the window - was probed on the same 2 families and produced
weak, inconclusive evidence (the implied mix is close to the forward ratio, but
n=2 families at 1 observed month each is not a basis for moving a global gate),
so it was not pursued.

`family_pool.MAX_RATIO_LOOKBACK_MONTHS` and `family_pool._ratios_as_of_occurrence`
were therefore REVERTED out of production rather than merged, and this module is
kept as the record of why - so the idea is not re-attempted without its result.
It consequently does not run against current HEAD: the two `_ratios_as_of_
occurrence` calls in `compare()` and the constant read in `print_report()` refer
to code that no longer exists. Restore them from this commit's parent-side diff
if the experiment is ever revisited.

WHAT IS BEING TESTED
────────────────────
`family_pool._ratios_as_of()` decides how a pooled successor family's forecast
divides between its current codes, from a CALENDAR trailing window
(`TRAILING_WINDOW_MONTHS = 6`) gated on `MIN_RATIO_MONTHS = 3` non-zero months
per code. `family_pool._ratios_as_of_occurrence()` asks the same question from an
OCCURRENCE window: reach back until each code has been seen selling
MIN_RATIO_MONTHS times, capped at `MAX_RATIO_LOOKBACK_MONTHS`.

The claim under test is that this is a strict generalisation — identical for
frequently-selling families, better only for sparse ones. This module measures
that; it changes nothing. It calls both functions on the same pooled families
built by the pipeline's own code, and writes two CSVs. `main.py` still calls
`compute_split_ratios()`, which still calls the calendar version only; no config
value moves and no pipeline output is rewritten.

THE SPLIT THE RESULT IS CUT BY
──────────────────────────────
The split-ratio computation is shared by every pooled family regardless of which
model its current codes are eventually routed to, so the fix could be adopted
universally or only for the intermittent-demand cohort. Both cuts are reported
separately so the blast radius on the Prophet-routed side is visible before that
choice is made. Cohort membership is Syntetos-Boylan, computed here (nothing in
the codebase classifies demand yet) — see the constants below.

A FINDING THAT ARRIVED BEFORE THE CODE DID
──────────────────────────────────────────
The motivating case, family A35030, is NOT fixed by this change, and the reason
is worth stating in the module rather than leaving in a console line. Within the
test anchor (actuals <= TRAIN_END) its two current codes have exactly one
non-zero month each: A35152 sells 400 in 2025-07, A35122 sells 76 in 2025-11.
The 11/89 forward ratio is computed almost entirely from 2026-01..2026-06 — data
inside the test window, which the test anchor may not see and must not see.

So the calendar window was never A35030's problem: `[-6:]` of two observed months
is both of them. What forces `equal_fallback` is the per-code MIN_RATIO_MONTHS
gate, and no lookback window of any length can satisfy it from history that does
not exist. The occurrence window reports the mix those two months do imply in
`window_share` (16/84 — close to the forward 11/89) while still refusing to
trust it at MIN_RATIO_MONTHS = 3. Whether that refusal is right is a threshold
decision, not a window decision, and it is left open here.

Run:  python -m analysis.split_ratio_window_compare
      python -m analysis.split_ratio_window_compare --db
      python -m analysis.split_ratio_window_compare --input-dir output_phase1
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import main as pipeline  # noqa: E402  — reused for load_all()'s CSV-cache logic
from preprocessing import family_pool, prepare, scope  # noqa: E402

# ── Demand classification (Syntetos-Boylan) ───────────────────────────────────
# Not codebase values and not derived here: the standard SBC cut-offs, quoted in
# the brief this pass was written against. Both detail CSVs carry the raw `adi`
# and `cv_squared` per code so the cohorts can be re-cut at other thresholds
# without a rerun.
#
#   ADI  = calendar months from a code's first sale to the anchor, divided by
#          the number of months it actually sold in. 1.0 = sells every month.
#   CV^2 = squared coefficient of variation of the NON-ZERO monthly sizes
#          (sample std, ddof=1). Undefined below two non-zero months.
SBC_ADI_THRESHOLD = 1.32
SBC_CV2_THRESHOLD = 0.49

# The classification is computed on actuals <= TRAIN_END, matching the anchor of
# the test_ratio the before/after table compares. Classifying on data running
# through TEST_END would judge a code's demand pattern using months the
# test-anchored ratio is forbidden to see.
CLASS_ANCHOR = pd.Timestamp(config.TRAIN_END + "-01")

SBC_LUMPY = "Lumpy"
SBC_INTERMITTENT = "Intermittent"
SBC_ERRATIC = "Erratic"
SBC_SMOOTH = "Smooth"
SBC_NO_DEMAND = "NoDemand"          # no non-zero month at or before the anchor

# The cohort the brief names — "Lumpy or Intermittent" — is exactly ADI >=
# threshold, at any CV^2. `non_smooth` (ADI >= threshold OR CV^2 >= threshold)
# additionally sweeps in Erratic; both are carried so either reading of the
# grouping can be applied to the same numbers.


# ── Rebuild the pooled families the pipeline actually produces ────────────────

def build_pool_input(cached: bool = True):
    """
    Reproduce `main.main()` steps 1-3b and return what step 5a would divide.

    Returns (prepared, families, eligible):
        prepared  — item-level (item_code, ds, y), Amazon-excluded. This is the
                    frame `compute_split_ratios` is handed in main(), at
                    item_code grain: the ratios come from each code's OWN
                    actuals, never from the pooled family total.
        families  — resolved SuccessorFamilies.
        eligible  — the family_keys that were actually pooled in this run.

    `scope.apply_scope` is deliberately NOT applied. main() computes the split
    ratios from `prepared`, not from `scoped`, so scoping here would compare the
    two windows on a frame neither one is ever given.
    """
    raw, bdm_df, active, _master, successor = pipeline.load_all(cached)

    # Amazon rows must go before prepare(): `channel` does not survive its
    # groupby. The excluded frame is dropped on the floor — writing it is
    # main()'s job, not this audit's.
    raw, _excluded = scope.filter_amazon_channel(raw)

    prepared = prepare.prepare(raw)
    families = family_pool.build_successor_families(successor)
    pooled = family_pool.pool_for_training(prepared, families, active, bdm_df)
    return prepared, families, pooled.eligible


# ── Syntetos-Boylan classification ────────────────────────────────────────────

def classify_demand(prepared: pd.DataFrame, codes: list[str]) -> pd.DataFrame:
    """
    ADI / CV^2 / SBC class for each item_code, from its own actuals <= anchor.

    Months with no sales row are absent from `prepared` (prepare() emits no
    zero-quantity rows), so the denominator of ADI is built from the calendar
    span rather than from the row count — otherwise every code would score
    ADI = 1.0 and nothing would ever look intermittent.

    The span starts at the code's FIRST non-zero month, not at TRAIN_START: a
    successor launched in 2025 has not been failing to sell since 2018, it did
    not exist. Ending it at the anchor (not at the last sale) is the other half
    of that — a code that stopped selling two years ago should read as
    intermittent, because it is.
    """
    act = prepared[["item_code", "ds", "y"]].copy()
    act["item_code"] = act["item_code"].astype(str).str.strip()
    act["ds"] = pd.to_datetime(act["ds"])
    act = act[act["ds"] <= CLASS_ANCHOR]

    rows: list[dict] = []
    for code in codes:
        own = act[act["item_code"] == code]
        nz = own[own["y"] > 0]

        if nz.empty:
            rows.append({
                "item_code": code, "n_nonzero_months": 0, "span_months": 0,
                "adi": np.nan, "cv_squared": np.nan, "cv2_undefined": True,
                "sbc_class": SBC_NO_DEMAND, "lumpy_or_intermittent": False,
                "non_smooth": False, "first_sale": pd.NaT, "last_sale": pd.NaT,
            })
            continue

        first, last = nz["ds"].min(), nz["ds"].max()
        span = ((CLASS_ANCHOR.year - first.year) * 12
                + (CLASS_ANCHOR.month - first.month) + 1)
        n_nz = int(nz["ds"].nunique())
        adi = span / n_nz

        sizes = nz["y"]
        # Sample std: with a single observation the dispersion of demand sizes
        # is not "zero", it is unmeasured. Reporting 0.0 would classify a code
        # that has sold exactly once as Smooth on the CV^2 axis.
        if len(sizes) >= 2 and sizes.mean() != 0:
            cv2 = float((sizes.std(ddof=1) / sizes.mean()) ** 2)
            cv2_undefined = False
        else:
            cv2 = np.nan
            cv2_undefined = True

        hi_adi = adi >= SBC_ADI_THRESHOLD
        # An undefined CV^2 cannot clear the threshold, so such a code lands on
        # the low-CV^2 side (Intermittent rather than Lumpy). Both are inside the
        # cohort under test, so the grouping is unaffected either way — the
        # `cv2_undefined` flag is carried so the LABEL is not read as measured.
        hi_cv2 = bool(cv2 >= SBC_CV2_THRESHOLD) if not cv2_undefined else False

        if hi_adi and hi_cv2:
            klass = SBC_LUMPY
        elif hi_adi:
            klass = SBC_INTERMITTENT
        elif hi_cv2:
            klass = SBC_ERRATIC
        else:
            klass = SBC_SMOOTH

        rows.append({
            "item_code": code, "n_nonzero_months": n_nz, "span_months": span,
            "adi": round(adi, 3), "cv_squared": round(cv2, 3) if not cv2_undefined else np.nan,
            "cv2_undefined": cv2_undefined, "sbc_class": klass,
            "lumpy_or_intermittent": bool(hi_adi),
            "non_smooth": bool(hi_adi or hi_cv2),
            "first_sale": first, "last_sale": last,
        })

    return pd.DataFrame(rows)


# ── Compare the two windows ───────────────────────────────────────────────────

def calendar_window_diagnostics(
    families: family_pool.SuccessorFamilies, eligible: frozenset[str],
    act: pd.DataFrame, cutoff: pd.Timestamp,
) -> pd.DataFrame:
    """
    What the CALENDAR window actually saw, per code — the evidence behind any
    claim that the occurrence window did or did not have more to find.

    `_ratios_as_of` returns only a ratio and a method, and is left that way (it
    is production code and `compute_split_ratios` depends on its shape), so its
    internals are re-derived here rather than exported. This must therefore
    mirror that function's window rule exactly: the last TRAILING_WINDOW_MONTHS
    *observed* months of the post-changeover frame, not the last six calendar
    months, and the same `_post_changeover` boundary.

        old_nonzero_in_window   times this code sold inside the calendar window
        old_nonzero_all_post    times it sold anywhere post-changeover within
                                the cutoff — the span the CURRENT gate is
                                evaluated over, which is not the window it then
                                computes the ratio from
        post_observed_months    observed months available post-changeover; when
                                this is <= the window length there is no older
                                history for any window to reach into, and the
                                two functions cannot differ by construction
    """
    act = act[act["ds"] <= cutoff]
    rows: list[dict] = []
    for key in sorted(eligible):
        current = families.current_codes_by_family[key]
        fam_act = act[act["item_code"].isin(current)]
        post = family_pool._post_changeover(
            fam_act, families.changeover_by_family.get(key, pd.NaT))
        months = sorted(pd.to_datetime(post["ds"].unique()))
        window = months[-family_pool.TRAILING_WINDOW_MONTHS:]
        span = ((window[-1].year - window[0].year) * 12
                + (window[-1].month - window[0].month) + 1) if window else 0
        for code in current:
            own = post[post["item_code"] == code]
            nz = own[own["y"] > 0]
            rows.append({
                "family_key": key, "item_code": code,
                "old_test_window_months": len(window),
                "old_test_window_span_months": span,
                "old_test_nonzero_in_window": int(nz[nz["ds"].isin(window)]["ds"].nunique()),
                "old_test_nonzero_all_post": int(nz["ds"].nunique()),
                "post_observed_months": len(months),
            })
    return pd.DataFrame(rows)


def compare(families: family_pool.SuccessorFamilies, eligible: frozenset[str],
            prepared: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Run both window functions over the same families and join the results.

    Both anchors are computed for each window — the test anchor is what the
    before/after table is about, but a fix that only moved the test ratio while
    leaving the forward ratio untouched would be worth knowing about, and vice
    versa.
    """
    act = prepared[["item_code", "ds", "y"]].copy()
    act["item_code"] = act["item_code"].astype(str).str.strip()
    act["ds"] = pd.to_datetime(act["ds"])
    train_end = pd.Timestamp(config.TRAIN_END + "-01")

    old_test = family_pool._ratios_as_of(
        families, eligible, act, train_end, family_pool.BASIS_TEST)
    old_fwd = family_pool._ratios_as_of(
        families, eligible, act, None, family_pool.BASIS_FORWARD)
    new_test = family_pool._ratios_as_of_occurrence(
        families, eligible, act, train_end, family_pool.BASIS_TEST)
    new_fwd = family_pool._ratios_as_of_occurrence(
        families, eligible, act, None, family_pool.BASIS_FORWARD)

    out = (
        old_test.rename(columns={"ratio": "old_test_ratio",
                                 "method": "old_test_split_method"})
        .merge(old_fwd.rename(columns={"ratio": "old_forward_ratio",
                                       "method": "old_forward_split_method"}),
               on=["family_key", "item_code"], how="outer")
        .merge(new_test[["family_key", "item_code", "ratio", "method",
                         "window_months", "window_span_months", "nonzero_months",
                         "lookback_capped", "window_share"]]
               .rename(columns={"ratio": "new_test_ratio",
                                "method": "new_test_split_method",
                                "window_months": "new_test_window_months",
                                "window_span_months": "new_test_window_span_months",
                                "nonzero_months": "new_test_nonzero_months",
                                "lookback_capped": "new_test_lookback_capped",
                                "window_share": "new_test_window_share"}),
               on=["family_key", "item_code"], how="outer")
        .merge(new_fwd[["family_key", "item_code", "ratio", "method",
                        "window_span_months"]]
               .rename(columns={"ratio": "new_forward_ratio",
                                "method": "new_forward_split_method",
                                "window_span_months": "new_forward_window_span_months"}),
               on=["family_key", "item_code"], how="outer")
    )

    out = out.merge(
        calendar_window_diagnostics(families, eligible, act, train_end),
        on=["family_key", "item_code"], how="left")

    # Per-code demand class, then the family's verdict = ANY current code.
    classes = classify_demand(prepared, sorted(out["item_code"].unique()))
    out = out.merge(classes, on="item_code", how="left")

    fam = (
        out.groupby("family_key")
        .agg(family_lumpy_or_intermittent=("lumpy_or_intermittent", "any"),
             family_non_smooth=("non_smooth", "any"))
        .reset_index()
    )
    out = out.merge(fam, on="family_key", how="left")

    out["n_current_codes"] = out["family_key"].map(
        {k: len(families.current_codes_by_family[k]) for k in eligible}
    )
    out["is_split_family"] = out["n_current_codes"] > 1
    out["test_ratio_delta"] = (out["new_test_ratio"] - out["old_test_ratio"]).round(6)
    out["test_method_changed"] = (
        out["new_test_split_method"] != out["old_test_split_method"])
    out["test_ratio_changed"] = out["test_ratio_delta"].abs() > 1e-9

    order = ["family_key", "item_code", "n_current_codes", "is_split_family",
             "sbc_class", "adi", "cv_squared", "cv2_undefined",
             "n_nonzero_months", "span_months",
             "lumpy_or_intermittent", "non_smooth",
             "family_lumpy_or_intermittent", "family_non_smooth",
             "old_test_split_method", "old_test_ratio",
             "new_test_split_method", "new_test_ratio",
             "test_ratio_delta", "test_method_changed", "test_ratio_changed",
             "old_test_window_months", "old_test_window_span_months",
             "old_test_nonzero_in_window", "old_test_nonzero_all_post",
             "post_observed_months",
             "new_test_window_months", "new_test_window_span_months",
             "new_test_nonzero_months", "new_test_lookback_capped",
             "new_test_window_share",
             "old_forward_split_method", "old_forward_ratio",
             "new_forward_split_method", "new_forward_ratio",
             "new_forward_window_span_months",
             "first_sale", "last_sale"]
    out = out[order].sort_values(["family_key", "item_code"]).reset_index(drop=True)
    return out, classes


# ── Reporting ─────────────────────────────────────────────────────────────────

TABLE_COLS = ["family_key", "item_code", "old_test_split_method", "old_test_ratio",
              "new_test_split_method", "new_test_ratio", "old_forward_ratio"]


def _fmt(frame: pd.DataFrame) -> str:
    view = frame[TABLE_COLS].copy()
    for col in ("old_test_ratio", "new_test_ratio", "old_forward_ratio"):
        view[col] = view[col].map(lambda v: f"{v:.4f}" if pd.notna(v) else "")
    view = view.rename(columns={
        "old_test_split_method": "old_method", "old_test_ratio": "old_ratio",
        "new_test_split_method": "new_method", "new_test_ratio": "new_ratio",
        "old_forward_ratio": "fwd_ratio(ref)"})
    return view.to_string(index=False)


def _cohort_stats(frame: pd.DataFrame) -> dict:
    split = frame[frame["is_split_family"]]
    return {
        "families": frame["family_key"].nunique(),
        "split_families": split["family_key"].nunique(),
        "codes": len(frame),
        "old_equal_fallback_fams": split.loc[
            split["old_test_split_method"] == family_pool.SPLIT_EQUAL,
            "family_key"].nunique(),
        "new_equal_fallback_fams": split.loc[
            split["new_test_split_method"] == family_pool.SPLIT_EQUAL,
            "family_key"].nunique(),
        "codes_method_changed": int(split["test_method_changed"].sum()),
        "codes_ratio_changed": int(split["test_ratio_changed"].sum()),
        "max_abs_ratio_delta": (round(split["test_ratio_delta"].abs().max(), 4)
                                if not split.empty else 0.0),
    }


def print_report(cmp_df: pd.DataFrame) -> None:
    sep = "=" * 100
    sub = "-" * 100

    print(f"\n{sep}\nSETUP\n{sep}")
    print(f"  calendar window (current) : last {family_pool.TRAILING_WINDOW_MONTHS} "
          f"observed months, gated on >= {family_pool.MIN_RATIO_MONTHS} non-zero "
          f"months per code over ALL post-changeover history")
    print(f"  occurrence window (new)   : reach back until every code has "
          f">= {family_pool.MIN_RATIO_MONTHS} non-zero months, never shorter than "
          f"{family_pool.TRAILING_WINDOW_MONTHS} observed months,")
    print(f"                              capped at "
          f"{family_pool.MAX_RATIO_LOOKBACK_MONTHS} calendar months")
    print(f"  test anchor               : actuals <= {config.TRAIN_END} "
          f"(unchanged by this work - the occurrence window moves how far BACK "
          f"we look, never the cutoff)")
    print(f"  demand class              : Syntetos-Boylan on each code's own "
          f"actuals <= {config.TRAIN_END}; ADI >= {SBC_ADI_THRESHOLD}, "
          f"CV^2 >= {SBC_CV2_THRESHOLD}")

    groups = [
        ("GROUP A - families with >=1 current code classified Lumpy or Intermittent "
         f"(ADI >= {SBC_ADI_THRESHOLD})", cmp_df[cmp_df["family_lumpy_or_intermittent"]]),
        ("GROUP B - every other pooled family (the Prophet-routed side)",
         cmp_df[~cmp_df["family_lumpy_or_intermittent"]]),
    ]

    for title, grp in groups:
        print(f"\n{sep}\n{title}\n{sep}")
        if grp.empty:
            print("  (no families in this group)")
            continue
        s = _cohort_stats(grp)
        print(f"  {s['families']} pooled families ({s['split_families']} with >1 "
              f"current code, {s['codes']} item codes)")
        print(f"  equal_fallback split families : {s['old_equal_fallback_fams']} old "
              f"-> {s['new_equal_fallback_fams']} new")
        print(f"  item codes whose METHOD changed: {s['codes_method_changed']}")
        print(f"  item codes whose RATIO changed : {s['codes_ratio_changed']} "
              f"(max |delta| {s['max_abs_ratio_delta']})")

        split = grp[grp["is_split_family"]]
        if split.empty:
            print("\n  No split families here - every family is a one-for-one "
                  "rename (ratio 1.0, method 'na'), which neither window can change.")
            continue
        print(f"\n{sub}\n  Split families - before / after "
              f"(fwd_ratio is the no-cutoff mix, shown as a reference for what "
              f"the test anchor is trying to approximate)\n{sub}")
        print(_fmt(split))

        renames = grp[~grp["is_split_family"]]
        if not renames.empty:
            n_id = int((renames["old_test_ratio"] == renames["new_test_ratio"]).sum())
            print(f"\n  + {len(renames)} one-for-one rename code(s), all "
                  f"1.0 / 'na' under both windows ({n_id}/{len(renames)} identical).")

    # The evidence for whatever the two tables above did or did not show. A
    # no-change result is only worth anything if it is visible WHY nothing
    # changed, so the inputs to both window decisions are printed side by side.
    print(f"\n{sep}\nWHY - what each window had available, per split-family code "
          f"(test anchor)\n{sep}")
    print(f"  old_nz_win   non-zero months inside the calendar window\n"
          f"  old_nz_post  non-zero months anywhere post-changeover - what the "
          f"CURRENT gate is judged on\n"
          f"  post_obs     observed months available post-changeover; when this "
          f"is <= old_win the\n"
          f"               occurrence window has no older history to reach into "
          f"and cannot differ\n"
          f"  new_nz_win   non-zero months inside the occurrence window "
          f"(>= old_nz_win by construction)")
    diag = cmp_df[cmp_df["is_split_family"]][[
        "family_key", "item_code", "family_lumpy_or_intermittent",
        "old_test_window_months", "old_test_nonzero_in_window",
        "old_test_nonzero_all_post", "post_observed_months",
        "new_test_window_months", "new_test_nonzero_months",
        "new_test_lookback_capped"]].rename(columns={
            "family_lumpy_or_intermittent": "grpA",
            "old_test_window_months": "old_win",
            "old_test_nonzero_in_window": "old_nz_win",
            "old_test_nonzero_all_post": "old_nz_post",
            "post_observed_months": "post_obs",
            "new_test_window_months": "new_win",
            "new_test_nonzero_months": "new_nz_win",
            "new_test_lookback_capped": "capped"})
    print()
    print(diag.to_string(index=False))

    short = diag[diag["old_nz_win"] < family_pool.MIN_RATIO_MONTHS]
    extendable = short[short["post_obs"] > short["old_win"]]
    print(f"\n  codes short of MIN_RATIO_MONTHS inside the calendar window : "
          f"{len(short)} / {len(diag)}")
    print(f"  ...of those, with older post-changeover history to reach into: "
          f"{len(extendable)}")
    print(f"  codes whose non-zero count the occurrence window improved     : "
          f"{int((diag['new_nz_win'] > diag['old_nz_win']).sum())}")

    # Both readings of the grouping, since the brief's parenthetical (ADI >= x
    # and/or CV^2 >= x) also sweeps in Erratic while the named classes do not.
    print(f"\n{sep}\nCOHORT SENSITIVITY - the two readings of the grouping\n{sep}")
    rows = []
    for label, col in (("Lumpy or Intermittent  (ADI >= 1.32)", "family_lumpy_or_intermittent"),
                       ("non-Smooth  (ADI >= 1.32 or CV^2 >= 0.49)", "family_non_smooth")):
        inn, outn = cmp_df[cmp_df[col]], cmp_df[~cmp_df[col]]
        rows.append({
            "grouping": label,
            "in_families": inn["family_key"].nunique(),
            "in_split": inn[inn["is_split_family"]]["family_key"].nunique(),
            "in_codes_changed": int(inn["test_ratio_changed"].sum()),
            "out_families": outn["family_key"].nunique(),
            "out_split": outn[outn["is_split_family"]]["family_key"].nunique(),
            "out_codes_changed": int(outn["test_ratio_changed"].sum()),
        })
    print(pd.DataFrame(rows).to_string(index=False))

    print(f"\n{sep}\nSBC CLASS MIX ACROSS ALL POOLED CURRENT CODES\n{sep}")
    mix = (cmp_df.groupby("sbc_class")
           .agg(codes=("item_code", "nunique"),
                undefined_cv2=("cv2_undefined", "sum"))
           .reset_index().sort_values("codes", ascending=False))
    print(mix.to_string(index=False))

    print(f"\n{sep}\nA35030 - the motivating case\n{sep}")
    a = cmp_df[cmp_df["family_key"] == "A35030"]
    if a.empty:
        print("  A35030 is not in the pooled set for this run.")
    else:
        cols = ["item_code", "sbc_class", "adi", "n_nonzero_months",
                "old_test_split_method", "old_test_ratio",
                "new_test_split_method", "new_test_ratio",
                "new_test_nonzero_months", "new_test_window_span_months",
                "new_test_window_share", "old_forward_ratio"]
        print(a[cols].to_string(index=False))
        print("\n  Not resolved to observed_mix, and the window is not why. Within "
              "the test anchor each code has ONE non-zero month, so the "
              f"{family_pool.MIN_RATIO_MONTHS}-month per-code gate cannot be met "
              "from any lookback length -")
        print("  the sales that produce the 11/89 forward ratio are in 2026, "
              "inside the test window the test anchor is forbidden to see. "
              "`new_test_window_share` is the mix those two")
        print("  months do imply; it is close to the forward ratio, and the gate "
              "is what rejects it. That is a MIN_RATIO_MONTHS decision, not a "
              "window decision - see the module docstring.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--db", action="store_true",
                    help="Reload raw inputs from Azure SQL instead of the cached CSVs")
    ap.add_argument("--input-dir", default=None,
                    help="Directory holding the cached input CSVs and receiving "
                         "this audit's output (default: config.OUTPUT_DIR)")
    args = ap.parse_args()

    if args.input_dir:
        # Read-only redirection of the cache location: load_all() and this
        # module's own writes both resolve through config.OUTPUT_DIR, and the
        # repo's live output directory is not always the one the last run left
        # behind. Nothing main.py owns is written either way.
        config.OUTPUT_DIR = Path(args.input_dir).resolve()
    out_dir = config.OUTPUT_DIR
    print(f"Input/output directory: {out_dir}")
    if not out_dir.exists():
        print(f"  ERROR: {out_dir} does not exist. Pass --input-dir, or --db to "
              f"reload from the database.")
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)

    prepared, families, eligible = build_pool_input(cached=not args.db)
    print(f"Pooled families in this run: {len(eligible)}")

    cmp_df, classes = compare(families, eligible, prepared)

    paths = [
        (out_dir / "split_ratio_window_comparison.csv", cmp_df),
        (out_dir / "split_ratio_demand_classes.csv", classes),
    ]
    for path, frame in paths:
        frame.to_csv(path, index=False)
        print(f"Wrote {path}  ({len(frame):,} rows)")

    print_report(cmp_df)
    return 0


if __name__ == "__main__":
    sys.exit(main())
