"""
Syntetos-Boylan demand-pattern classification, per active-scope item.

WHAT THIS IS FOR
────────────────
Prophet is a trend + seasonality model. It has nothing to say about a series
that is mostly zeros, and the pipeline currently discovers that only indirectly:
`config.MIN_TRAIN_MONTHS = 24` drops thin series before they are ever fitted, so
an intermittent item is silently absent from the output rather than routed to a
model built for intermittent demand (Croston / TSB). This module names the
demand pattern explicitly, for EVERY item in active scope — fitted or not.

The "or not" is the point. An item that failed MIN_TRAIN_MONTHS is exactly the
kind of item a Croston/TSB route exists to serve, so a classification computed
only over Prophet-fitted series would be blind to its own use case.

READ-ONLY, AND NOW A PIPELINE STAGE
───────────────────────────────────
Still read-only: `classify_scope()` fits nothing, moves no config value and
changes no other frame. What HAS changed is who calls it. `main.py` now calls it
on every run and feeds the result to `model_routing.route_scope()`, whose Naive
routes `models/naive_model.py` turns into delivered forecast rows — so the
category on a Lumpy/Intermittent item is no longer only a description in a
report, it is what decides whether that item gets a trailing-average forecast
and over how many months. `analysis/demand_classification_report.py` remains the
standalone runner for reading this output on its own.

THE ONE THING THAT IS EASY TO GET WRONG
───────────────────────────────────────
A pooled successor family is FITTED AS ONE SERIES. `family_pool` sums a retired
code and its successors into a single family series, Prophet sees that, and the
result is divided back out afterwards. So the demand pattern that matters for a
pooled item is the POOLED family's pattern, not the item's own standalone
history — the successor code on its own looks intermittent almost by
construction (it started selling six months ago), while the family series it is
actually modelled through may have eight years of smooth demand behind it.
Classifying the standalone series would route a perfectly smooth family to
Croston on the strength of its newest member's launch date.

`classify_scope()` therefore classifies each item on its FIT KEY's series, taken
from the same pooled frame `scope.apply_scope()` is handed in `main()`, and
records which series was used (`family_key`, `is_pooled`) on every row. Members
of one pooled family share a family_key and therefore share identical ADI, CV^2
and category — that is not duplication, it is the classification of the one
series that exists.

DEFINITIONS
───────────
Computed on non-Amazon actuals through `config.TRAIN_END` — the same data the
Prophet fit itself sees. Classifying on data running to TEST_END would judge a
series using months no fitted model was allowed to look at.

    tenure  calendar months from the series' FIRST non-zero month to TRAIN_END,
            inclusive. The same calendar-span convention as
            `analysis.trend_audit._window_average()`: `prepare()` emits no
            zero-quantity rows, so a month with no sales is ABSENT from the
            frame, and any denominator built from row counts would score every
            series ADI = 1.0 and nothing would ever look intermittent. Starting
            at first sale rather than at TRAIN_START is the other half of it — a
            code launched in 2025 has not been failing to sell since 2018, it
            did not exist.

    ADI     tenure / number of non-zero months. 1.0 = sold every month since
            launch.

    CV^2    (stdev / mean)^2 of the monthly quantity across those non-zero
            months. POPULATION stdev (ddof=0) — see CV2_DDOF below. Undefined
            below two non-zero months and reported as None there: with a single
            observation the dispersion of demand sizes is unmeasured, not zero,
            and reporting 0.0 would classify a code that has sold exactly once
            as Smooth.

    category   the SBC quadrant from config.SB_ADI_THRESHOLD /
               config.SB_CV2_THRESHOLD, or one of the two honest non-answers:
               'Insufficient Data to Classify' below
               config.SB_MIN_MONTHS_TO_CLASSIFY non-zero months, and 'No Data'
               for a series with no sales at all through TRAIN_END.

    decline_ratio  mean monthly demand over the last TREND_WINDOW_MONTHS (24)
               calendar months divided by the mean over the 24 months before
               them, ZEROS INCLUDED — so it is measured on a zero-filled grid,
               not on the non-zero months ADI and CV^2 are computed over. Below
               1.0 the series is selling less than it was. None where there is
               no full prior window to divide by, or nothing was sold in it.

               It is not part of the SBC quadrant and moves no category. It is
               here because `preprocessing/model_routing.py` needs it: the naive
               trailing-average route it sends Lumpy/Intermittent items to picks
               a 3-month window for declining series and a 12-month one
               otherwise, and that cut is this ratio against 1.0. Computed for
               every item regardless of category, the same way ADI and CV^2 are
               — see TREND_WINDOW_MONTHS for where the windows come from.
"""
from __future__ import annotations

import logging

import pandas as pd

import config
from preprocessing import family_pool

logger = logging.getLogger(__name__)

# ── Category labels ───────────────────────────────────────────────────────────
SMOOTH = "Smooth"
INTERMITTENT = "Intermittent"
ERRATIC = "Erratic"
LUMPY = "Lumpy"
INSUFFICIENT = "Insufficient Data to Classify"
NO_DATA = "No Data"

#: Every label this module can emit, in the order reports should present them.
CATEGORIES = (SMOOTH, ERRATIC, INTERMITTENT, LUMPY, INSUFFICIENT, NO_DATA)

#: The two labels that are a refusal to classify rather than a classification.
UNCLASSIFIED = frozenset({INSUFFICIENT, NO_DATA})

# Population stdev, NOT the sample estimator. The non-zero months up to
# TRAIN_END are the complete set of demand sizes the series has actually shown,
# not a draw from a larger pool, so no (n-1) correction is called for.
#
# It also matters, slightly: the two estimators differ by a factor (n-1)/n on
# the VARIANCE — under 1.2% for the 87-96-month series that dominate this
# dataset — which is invisible except for series sitting within ~1% of a
# threshold. Exactly one does: family U35118 (current codes U35119, U35219) has
# CV^2 = 0.4947 under ddof=1 and 0.4890 under ddof=0, i.e. Erratic under one and
# Smooth under the other. ddof=0 reproduces the independently computed reference
# counts exactly (50 Smooth / 104 Erratic / 32 Lumpy / 0 Intermittent among
# fitted items); ddof=1 reports 48 / 106 / 32 / 0.
#
# Nothing about that family is settled by picking an estimator — the honest
# reading is that it sits ON the boundary. `near_threshold` flags every such
# series so a knife-edge verdict is never mistaken for a firm one.
CV2_DDOF = 0

# A classification is flagged `near_threshold` when ADI or CV^2 is within this
# fraction of its cut-off. Purely a caution flag on the output; it changes no
# category. 10% is a judgment call, sized to be comfortably wider than the ~1%
# estimator sensitivity above so it cannot miss a genuinely borderline series.
NEAR_THRESHOLD_PCT = 0.10

# Window for `decline_ratio`: the mean monthly demand of the last N calendar
# months against the N before them. Matches
# analysis.croston_experiment.TREND_WINDOW_MONTHS — this IS that comparison,
# ported from an experiment file into the routing decision it justified. Kept as
# a literal here rather than imported, because preprocessing/ must not depend on
# analysis/: experiments are disposable, routing is not.
TREND_WINDOW_MONTHS = 24

COLUMNS = ["item_code", "family_key", "is_pooled", "adi", "cv2",
           "n_nonzero_months", "category", "decline_ratio"]


def train_end_ts() -> pd.Timestamp:
    """The classification anchor: last month of the training window."""
    return pd.Timestamp(config.TRAIN_END + "-01")


def _tenure_months(first_sale: pd.Timestamp, anchor: pd.Timestamp) -> int:
    """Calendar months from `first_sale` to `anchor`, inclusive of both."""
    return ((anchor.year - first_sale.year) * 12
            + (anchor.month - first_sale.month) + 1)


def categorise(adi: float | None, cv2: float | None, n_nonzero: int) -> str:
    """
    The SBC quadrant for one series, or a refusal.

    Split out from `classify_series` so the thresholds can be re-applied to an
    already-computed adi/cv2 pair (e.g. a sensitivity sweep) without recomputing
    anything, and so the quadrant rule is readable in one place.
    """
    if n_nonzero == 0:
        return NO_DATA
    if n_nonzero < config.SB_MIN_MONTHS_TO_CLASSIFY:
        return INSUFFICIENT
    if adi is None or cv2 is None or pd.isna(adi) or pd.isna(cv2):
        return INSUFFICIENT

    high_adi = adi >= config.SB_ADI_THRESHOLD
    high_cv2 = cv2 >= config.SB_CV2_THRESHOLD
    if high_adi:
        return LUMPY if high_cv2 else INTERMITTENT
    return ERRATIC if high_cv2 else SMOOTH


def near_threshold(adi: float | None, cv2: float | None) -> bool:
    """
    Whether this series sits within NEAR_THRESHOLD_PCT of either cut-off.

    A True here means the quadrant is a coin-flip and should not be acted on
    without looking at the series — see CV2_DDOF for the case that motivated it.
    """
    for value, cut in ((adi, config.SB_ADI_THRESHOLD),
                       (cv2, config.SB_CV2_THRESHOLD)):
        if value is None or pd.isna(value):
            continue
        if abs(value - cut) <= cut * NEAR_THRESHOLD_PCT:
            return True
    return False


def _zero_filled_grid(monthly: pd.DataFrame, first_sale: pd.Timestamp,
                      anchor: pd.Timestamp) -> pd.Series:
    """
    The series as one row per calendar month from `first_sale` to `anchor`,
    months with no sales filled in as 0.0. Indexed by month.

    Same shape as `analysis.croston_experiment.monthly_grid()`, built from the
    non-zero month sums `classify_series` has already computed rather than from
    the raw rows a second time. ADI and CV^2 are defined over the non-zero months
    ALONE and never need this; `decline_ratio` is a demand RATE and does — a
    series that sold in three of the last twenty-four months has a rate of
    (total / 24), not (total / 3), and averaging over present rows only would
    report every dormant item as flat.
    """
    idx = pd.date_range(first_sale, anchor, freq="MS")
    return (monthly.set_index("ds")["y"].astype(float)
            .reindex(idx, fill_value=0.0))


def decline_ratio(grid: pd.Series, anchor: pd.Timestamp) -> float | None:
    """
    Recent demand rate over the rate before it, on a zero-filled monthly grid.

    Ported from `analysis.croston_experiment.series_facts()` — same windows,
    same zero-fill, same undefined cases — so a routing decision acts on the
    same number the experiment that justified it was run against.

    A RATIO, not a bucket: < 1.0 means declining, which is croston_experiment's
    cut exactly. That experiment's three-way split (declining / stable-or-growing
    / insufficient-history) is a REPORTING distinction belonging to it; this
    module emits only the raw ratio, and `model_routing.py` applies the same
    < 1.0 cut to it. None means there is no prior window to divide by, or the
    item sold nothing in the one it has — the two cases the experiment reports
    apart and routing treats alike.
    """
    recent_start = anchor - pd.DateOffset(months=TREND_WINDOW_MONTHS - 1)
    prior_start = anchor - pd.DateOffset(months=2 * TREND_WINDOW_MONTHS - 1)
    recent = grid[grid.index >= recent_start]
    prior = grid[(grid.index < recent_start) & (grid.index >= prior_start)]

    rate_recent = float(recent.mean()) if len(recent) else float("nan")
    rate_prior = float(prior.mean()) if len(prior) else float("nan")

    if pd.isna(rate_prior) or rate_prior == 0 or pd.isna(rate_recent):
        return None
    return rate_recent / rate_prior


def classify_series(series: pd.DataFrame, anchor: pd.Timestamp | None = None) -> dict:
    """
    ADI, CV^2 and SBC category for ONE (ds, y) series.

    `series` is a single fit key's rows — a pooled family series for a pooled
    item, the item's own series otherwise. Rows after `anchor` are ignored, so
    the caller may pass an untruncated frame.

    Returns adi/cv2 as None where they are not defined, never as 0.0.
    """
    anchor = anchor or train_end_ts()

    s = series[["ds", "y"]].copy()
    s["ds"] = pd.to_datetime(s["ds"])
    s = s[s["ds"] <= anchor]
    nz = s[s["y"] > 0]

    if nz.empty:
        return {"adi": None, "cv2": None, "n_nonzero_months": 0,
                "tenure_months": 0, "first_sale": pd.NaT, "last_sale": pd.NaT,
                "category": NO_DATA, "near_threshold": False,
                "decline_ratio": None}

    # A series can carry more than one row per month only if something upstream
    # changed grain; group defensively so ADI cannot be inflated by duplicates.
    monthly = nz.groupby("ds", as_index=False)["y"].sum()

    first, last = monthly["ds"].min(), monthly["ds"].max()
    tenure = _tenure_months(first, anchor)
    n_nz = len(monthly)
    adi = tenure / n_nz

    sizes = monthly["y"]
    if n_nz >= 2 and sizes.mean() != 0:
        cv2 = float((sizes.std(ddof=CV2_DDOF) / sizes.mean()) ** 2)
    else:
        cv2 = None

    category = categorise(adi, cv2, n_nz)

    # Computed for every series, not only the Lumpy/Intermittent ones routing
    # consults it for — the same principle ADI and CV^2 are computed under. It
    # is two means over a reindexed grid; the cost of always having it is
    # nothing next to the cost of a column that exists for some rows only.
    ratio = decline_ratio(_zero_filled_grid(monthly, first, anchor), anchor)

    return {"adi": float(adi), "cv2": cv2, "n_nonzero_months": int(n_nz),
            "tenure_months": int(tenure), "first_sale": first, "last_sale": last,
            "category": category,
            "near_threshold": (category not in UNCLASSIFIED
                               and near_threshold(adi, cv2)),
            "decline_ratio": ratio}


def active_scope_items(
    pooled_data: pd.DataFrame,
    active_products: pd.DataFrame,
    families: family_pool.SuccessorFamilies,
    eligible: frozenset[str],
    channel_mismatch_excluded: set[str] | None = None,
) -> list[str]:
    """
    Every item_code in active scope, at OUTPUT grain, before MIN_TRAIN_MONTHS.

    Mirrors `scope.apply_scope()` exactly except that `forecastable_series()` is
    NOT applied — that filter is what separates fitted from unfitted items, and
    the unfitted ones are precisely who this classification is for.

    `pooled_data` is `family_pool.pool_for_training(...).data`, so pooled
    families appear here under their family_key. `family_pool.scope_item_codes`
    expands those keys back to the real current codes the pipeline would emit
    rows for, which is the grain a routing decision has to be made at.
    """
    active = set(active_products["item_code"].astype(str).str.strip())
    df = pooled_data.copy()
    df["item_code"] = df["item_code"].astype(str).str.strip()
    df = df[df["item_code"].isin(active)]

    if channel_mismatch_excluded:
        df = df[~df["item_code"].isin(channel_mismatch_excluded)]

    items = family_pool.scope_item_codes(df, families, eligible)

    # A pooled family_key survives the active filter because pool_for_training
    # added it to the allow-list; its expanded current codes may themselves be
    # channel-mismatch excluded, and those must not reappear here.
    if channel_mismatch_excluded:
        items = [c for c in items if c not in channel_mismatch_excluded]
    return items


def classify_scope(
    pooled_data: pd.DataFrame,
    active_products: pd.DataFrame,
    families: family_pool.SuccessorFamilies,
    eligible: frozenset[str],
    channel_mismatch_excluded: set[str] | None = None,
    anchor: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """
    One row per active-scope item: item_code, family_key, is_pooled, adi, cv2,
    n_nonzero_months, category (plus tenure/first/last-sale diagnostics).

    Each item is classified on the series its FIT KEY resolves to inside
    `pooled_data` — the pooled family series for a pooled item, its own series
    otherwise. See the module docstring for why that distinction is not
    cosmetic.

    Every family series is classified once and its verdict attributed to each of
    its current codes, the same way `analysis.trend_audit` attributes one fit to
    every member: the family is what would be modelled, so reporting a separate
    per-member statistic would be describing series that are never fitted.
    """
    anchor = anchor or train_end_ts()
    items = active_scope_items(pooled_data, active_products, families, eligible,
                               channel_mismatch_excluded)

    df = pooled_data.copy()
    df["item_code"] = df["item_code"].astype(str).str.strip()
    by_key = {key: grp for key, grp in df.groupby("item_code")}

    cache: dict[str, dict] = {}
    rows: list[dict] = []
    missing: list[str] = []

    for item in items:
        key = families.family_key(item)
        is_pooled = key in eligible
        fit_key = key if is_pooled else item

        if fit_key not in by_key:
            # Only reachable if an item is on the allow-list with no rows at all
            # in the prepared frame. Recorded as No Data rather than dropped —
            # an item the pipeline can say nothing about is a finding.
            missing.append(item)
            rows.append({"item_code": item, "family_key": fit_key,
                         "is_pooled": is_pooled, "adi": None, "cv2": None,
                         "n_nonzero_months": 0, "tenure_months": 0,
                         "first_sale": pd.NaT, "last_sale": pd.NaT,
                         "category": NO_DATA, "near_threshold": False,
                         "decline_ratio": None})
            continue

        if fit_key not in cache:
            cache[fit_key] = classify_series(by_key[fit_key], anchor)
        res = cache[fit_key]

        rows.append({"item_code": item, "family_key": fit_key,
                     "is_pooled": is_pooled, **res})

    out = pd.DataFrame(rows)
    if not out.empty:
        out = out[COLUMNS + ["near_threshold", "tenure_months",
                             "first_sale", "last_sale"]]
        out = out.sort_values("item_code").reset_index(drop=True)

    if missing:
        logger.warning(
            "Demand classification: %s active-scope item(s) have no rows in the "
            "prepared frame and are reported as '%s': %s",
            f"{len(missing):,}", NO_DATA, sorted(missing)[:10],
        )
    mix = out["category"].value_counts().to_dict() if not out.empty else {}
    logger.info(
        "Demand classification (SBC, actuals <= %s, ADI >= %s / CV^2 >= %s, "
        "min %s non-zero months): %s items over %s distinct fitted series | %s",
        config.TRAIN_END, config.SB_ADI_THRESHOLD, config.SB_CV2_THRESHOLD,
        config.SB_MIN_MONTHS_TO_CLASSIFY, f"{len(out):,}", f"{len(cache):,}",
        ", ".join(f"{c} {mix.get(c, 0)}" for c in CATEGORIES),
    )
    return out
