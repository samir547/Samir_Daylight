"""
Successor-family pooling for discontinued / replaced product codes.

NAMING — not the same thing as preprocessing/family.py
──────────────────────────────────────────────────────
`family.py` handles **Product Family**, a reporting tag read from the master
product table ("Slimline", "Wafer 1", …) and joined onto output for grouping.
This module handles a **successor family**: the set of item_codes that describe
one continuous stream of demand across a product changeover — a retired code
plus the successor code(s) that replaced it. The two concepts are unrelated;
everything here is prefixed/worded as *successor family* or *family_key* to
keep them apart.

THE PROBLEM
───────────
Products get discontinued and replaced, sometimes one-for-one (a rename) and
sometimes one-to-many (a plain colourway splitting into three finishes). The
successor codes start their sales history at the changeover, so they carry only
a handful of months. `config.MIN_TRAIN_MONTHS = 24` then drops them from
`scope.forecastable_series()` and they get no forecast at all — even though the
underlying demand has years of history under the retired code.

THE DETOUR
──────────
Pooling is a self-contained detour around the per-item Prophet fit. It is
invisible to every other part of the pipeline:

    item-level prepared data
        → pooled to family_key × month   (this module, pre-fit)
        → run_forecasts() sees family_key in the item_code column
        → split back to item_code        (this module, post-fit)
    item-level test_df / forecast_df / metrics_df

`config.GROUP_COLS` stays `["item_code"]` throughout: the pooled frame simply
carries the family_key *in* the `item_code` column for the duration of the fit.
By the time `family.enrich()` or `benchmark.build_comparison()` see anything,
the frames are back at plain item_code grain.

THE MAP IS NOT A FOREST
───────────────────────
`dbo.product_successor_map` is a many-to-many relation, not a tree: 13 successor
codes are each reachable from two different retired codes (e.g. E35119 succeeds
both E35117 and E35118, which were two old colourways collapsing into the same
two new ones). Resolving `family_key` by "the old code it maps from" is
therefore ambiguous, and pooling per old code would (a) count a shared
successor's history into two separate pooled series and (b) emit two forecast
rows for the same item_code, breaking the item_code grain contract downstream.

Families are therefore resolved as **connected components** of the old↔new
bipartite graph. For the common one-old-code case a component is exactly the
old code plus its successors, so this is a strict generalisation. The
`family_key` is the lexicographically smallest retired code in the component
(deterministic, and still an `old_item_code` as the spec describes).

SCOPING ORDER
─────────────
`scope.filter_active_products()` matches against `active_products.csv`, which
only ever lists *current* codes — retired codes were dropped from it long ago
(here: 4 of 71 retired codes are on the list, vs 78 of 78 successor codes).
So family eligibility must be decided on the family's **current** codes, then
the family_key added to the allow-list. Evaluating eligibility on the
family_key itself would silently drop every pooled family.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

import pandas as pd

import config
from evaluation.metrics import calc_metrics

logger = logging.getLogger(__name__)


# ── Tunables ──────────────────────────────────────────────────────────────────

# Review statuses whose pairings are NOT pooled. 'Old code still selling' means
# the changeover has not actually happened yet, so there is no post-changeover
# period from which to observe a split mix. Those codes stay singletons.
POOLING_EXCLUDED_STATUSES = frozenset({"Old code still selling"})

# TODO(successor-data-quality): remove this exclusion once review_id 51 is
# confirmed by whoever entered it.
# Review row old_item_code='D35040' (region UK, range_family 'Wafer 1') carries
# correction_raw = 'D35151, E35121' — an EU-prefixed successor inside a
# UK-region correction. Every other multi-code correction in the table stays
# within one region's prefix (E35040 → 'E35151, E35121', U35040 → 'U35151,
# U35121'), so this is very likely a transcription slip for 'D35121'. It is
# NOT silently corrected here, and NOT silently pooled as a cross-region
# mapping: the whole family is excluded from pooling and logged, leaving
# D35040 / D35151 / E35121 to behave exactly as they do today.
EXCLUDED_OLD_ITEM_CODES = frozenset({"D35040"})

# Trailing window (months of item-level actuals) used to observe each successor
# code's share of family demand.
TRAILING_WINDOW_MONTHS = 6

# Minimum post-changeover months of actual sales required *per successor code*
# before a computed mix is trusted. Below this the family splits equally and is
# flagged with split_method='equal_fallback'.
MIN_RATIO_MONTHS = 3

# split_method values written to the output frames.
SPLIT_NA = "na"                     # singleton, or a one-for-one rename
SPLIT_OBSERVED = "observed_mix"     # ratios from trailing item-level actuals
SPLIT_EQUAL = "equal_fallback"      # too little post-changeover history

# Which of the two ratios (see compute_split_ratios) a frame was split with.
BASIS_TEST = "test"                 # test_ratio — for anything scored on the test window
BASIS_FORWARD = "forward"           # forward_ratio — for the genuine forward forecast

TRACE_COLS = ["family_key", "split_method", "split_ratio", "ratio_basis"]


# ── Phase 2: resolve family keys ──────────────────────────────────────────────

@dataclass(frozen=True)
class SuccessorFamilies:
    """Resolved successor-family structure. Purely derived from the map table."""

    #: every item_code in a pooled family → its family_key
    family_key_by_item: Mapping[str, str]
    #: family_key → every member code (retired + successors), sorted
    members_by_family: Mapping[str, tuple[str, ...]]
    #: family_key → the family's *current* (successor) codes, sorted
    current_codes_by_family: Mapping[str, tuple[str, ...]]
    #: family_key → earliest estimated changeover date (NaT if unknown)
    changeover_by_family: Mapping[str, pd.Timestamp]

    def family_key(self, item_code: str) -> str:
        """family_key for `item_code`; the code itself if it is a singleton."""
        code = str(item_code).strip()
        return self.family_key_by_item.get(code, code)

    @property
    def family_keys(self) -> frozenset[str]:
        return frozenset(self.members_by_family)


def _normalise_map(successor_map: pd.DataFrame) -> pd.DataFrame:
    """Trim, drop unusable rows, and apply the pooling exclusions (logged)."""
    df = successor_map.copy()
    for col in ("old_item_code", "new_item_code"):
        df[col] = df[col].astype(str).str.strip()
    df["status"] = df.get("status", pd.Series(dtype=str)).astype(str).str.strip()
    df["estimated_changeover"] = pd.to_datetime(
        df.get("estimated_changeover"), errors="coerce"
    )

    blank = df["old_item_code"].isin(["", "nan", "None"]) | df["new_item_code"].isin(["", "nan", "None"])
    if blank.any():
        logger.warning("Successor map: dropping %s rows with a blank code", f"{blank.sum():,}")
    df = df[~blank]

    # Self-pairings would collapse a family onto itself; none exist today.
    df = df[df["old_item_code"] != df["new_item_code"]]

    still_selling = df["status"].isin(POOLING_EXCLUDED_STATUSES)
    if still_selling.any():
        logger.info(
            "Successor pooling: excluding %s pairing(s) with status in %s "
            "(changeover has not happened yet) — %s stay singletons",
            f"{still_selling.sum():,}", sorted(POOLING_EXCLUDED_STATUSES),
            sorted(df.loc[still_selling, "old_item_code"].unique().tolist()),
        )
    df = df[~still_selling]

    excluded = df["old_item_code"].isin(EXCLUDED_OLD_ITEM_CODES)
    if excluded.any():
        for old_code, grp in df[excluded].groupby("old_item_code"):
            logger.warning(
                "Successor pooling: EXCLUDING family %s (successors %s) — unconfirmed "
                "data-quality issue: the review correction mixes an EU-prefixed code "
                "into a UK-region row and has not been confirmed. %s and its "
                "successors are left as singletons. See EXCLUDED_OLD_ITEM_CODES.",
                old_code, sorted(grp["new_item_code"].unique().tolist()), old_code,
            )
    df = df[~excluded]

    return df


def _connected_components(edges: Sequence[tuple[str, str]]) -> dict[str, set[str]]:
    """Union-find over the old↔new bipartite graph. Returns root → members."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]      # path compression
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in edges:
        union(a, b)

    comps: dict[str, set[str]] = {}
    for node in parent:
        comps.setdefault(find(node), set()).add(node)
    return comps


def build_successor_families(successor_map: pd.DataFrame) -> SuccessorFamilies:
    """
    Resolve successor families from the raw map table.

    Pure function — depends only on `successor_map`, touches no other pipeline
    state, and is independently testable.
    """
    if successor_map is None or successor_map.empty:
        logger.warning("Successor map is empty — no families will be pooled.")
        return SuccessorFamilies({}, {}, {}, {})

    df = _normalise_map(successor_map)
    if df.empty:
        logger.warning("Successor map has no poolable pairings after exclusions.")
        return SuccessorFamilies({}, {}, {}, {})

    olds = set(df["old_item_code"])
    comps = _connected_components(list(zip(df["old_item_code"], df["new_item_code"])))

    family_key_by_item: dict[str, str] = {}
    members_by_family: dict[str, tuple[str, ...]] = {}
    current_by_family: dict[str, tuple[str, ...]] = {}
    changeover_by_family: dict[str, pd.Timestamp] = {}

    for members in comps.values():
        component_olds = sorted(members & olds)
        # A successor that is itself retired elsewhere would land in the same
        # component; "current" therefore means "in this component and not a
        # retired code of it". (No such chains exist in the data today.)
        current = tuple(sorted(members - set(component_olds)))
        if not component_olds or not current:
            continue

        key = component_olds[0]
        members_by_family[key] = tuple(sorted(members))
        current_by_family[key] = current
        for code in members:
            family_key_by_item[code] = key

        dates = df.loc[df["old_item_code"].isin(component_olds), "estimated_changeover"].dropna()
        changeover_by_family[key] = dates.min() if len(dates) else pd.NaT

    n_multi_old = sum(
        1 for k, m in members_by_family.items()
        if len(set(m) & olds) > 1
    )
    n_split = sum(1 for c in current_by_family.values() if len(c) > 1)
    logger.info(
        "Successor families: %s resolved | %s split families (>1 current code) | "
        "%s merged components (>1 retired code) | %s item codes mapped",
        f"{len(members_by_family):,}", f"{n_split:,}", f"{n_multi_old:,}",
        f"{len(family_key_by_item):,}",
    )
    return SuccessorFamilies(
        family_key_by_item=family_key_by_item,
        members_by_family=members_by_family,
        current_codes_by_family=current_by_family,
        changeover_by_family=changeover_by_family,
    )


def resolve_family_keys(
    successor_map: pd.DataFrame, item_codes: Iterable[str]
) -> dict[str, str]:
    """
    item_code → family_key for every code in `item_codes` (Phase 2).

    Codes not in a poolable family map to themselves (singletons). Pure
    function; safe to call in isolation for testing.
    """
    families = build_successor_families(successor_map)
    return {
        code: families.family_key(code)
        for code in sorted({str(c).strip() for c in item_codes if str(c).strip()})
    }


# ── Phase 3: pool for training ────────────────────────────────────────────────

@dataclass(frozen=True)
class PooledTrainingSet:
    """What `pool_for_training` hands to `scope.apply_scope`."""

    #: prepared frame with pooled families relabelled to their family_key
    data: pd.DataFrame
    #: active-product allow-list, extended with the eligible family_keys
    active_products: pd.DataFrame
    #: BDM sheet, extended with a rating row per eligible family_key (--pilot)
    bdm_forecasts: pd.DataFrame
    #: family_keys that were actually pooled
    eligible: frozenset[str]


def _eligible_families(
    families: SuccessorFamilies,
    active_products: pd.DataFrame,
    present_codes: set[str],
) -> frozenset[str]:
    """
    A family is eligible if ANY of its **current** codes is on the active-product
    allow-list, and at least one member code has data in this run.

    Evaluated on current codes, never on the family_key — see module docstring.
    """
    active = set(active_products["item_code"].astype(str).str.strip())
    eligible = {
        key for key, current in families.current_codes_by_family.items()
        if (set(current) & active) and (set(families.members_by_family[key]) & present_codes)
    }
    dropped = set(families.current_codes_by_family) - eligible
    logger.info(
        "Successor pooling eligibility: %s/%s families active (checked against the "
        "family's CURRENT codes, not the retired family_key)%s",
        f"{len(eligible):,}", f"{len(families.current_codes_by_family):,}",
        f" | {len(dropped):,} inactive/absent" if dropped else "",
    )
    return frozenset(eligible)


def pool_for_training(
    prepared: pd.DataFrame,
    families: SuccessorFamilies,
    active_products: pd.DataFrame,
    bdm_df: pd.DataFrame,
) -> PooledTrainingSet:
    """
    Replace each eligible family's member series with one pooled family series.

    `prepared` is the item-level output of `prepare.prepare()` (item_code, ds, y).
    Member rows are summed to `family_key × ds` and carried in the `item_code`
    column so `run_forecasts()` (which only needs (ds, y) per group) needs no
    change. Non-member rows pass through untouched.

    The allow-list and BDM sheet are extended with the family_keys so that
    `scope.filter_active_products()` / `scope.filter_pilot()` keep working
    unmodified — the extension is the mechanism that carries the family's
    *current*-code eligibility over to its retired family_key.
    """
    if prepared.empty or not families.members_by_family:
        return PooledTrainingSet(prepared, active_products, bdm_df, frozenset())

    df = prepared.copy()
    df["item_code"] = df["item_code"].astype(str).str.strip()

    eligible = _eligible_families(families, active_products, set(df["item_code"]))
    if not eligible:
        return PooledTrainingSet(df, active_products, bdm_df, frozenset())

    member_to_key = {
        code: key
        for key in eligible
        for code in families.members_by_family[key]
    }

    is_member = df["item_code"].isin(member_to_key)
    passthrough = df[~is_member]
    pooled = df[is_member].copy()
    pooled["item_code"] = pooled["item_code"].map(member_to_key)
    pooled = (
        pooled.groupby(config.GROUP_COLS + ["ds"], as_index=False)["y"]
        .sum()
    )

    out = pd.concat([passthrough, pooled], ignore_index=True).sort_values(
        config.GROUP_COLS + ["ds"]
    ).reset_index(drop=True)

    # Extend the allow-list with the family_keys (see docstring).
    active_aug = pd.concat(
        [active_products, pd.DataFrame({"item_code": sorted(eligible)})],
        ignore_index=True,
    ).drop_duplicates(subset=["item_code"], keep="first")

    # Extend the BDM sheet with one synthetic rating row per family_key so that
    # --pilot selects a pooled family whenever any of its current codes is
    # A-rated. Only `item_code` and `rating` are read by scope.filter_pilot();
    # this copy never reaches benchmark.build_comparison().
    bdm_aug = bdm_df
    if "rating" in bdm_df.columns:
        rating_by_code = (
            bdm_df.assign(
                item_code=bdm_df["item_code"].astype(str).str.strip(),
                rating=bdm_df["rating"].astype(str).str.strip(),
            )
            .dropna(subset=["rating"])
            .groupby("item_code")["rating"]
            .agg(lambda s: sorted(set(s))[0])
        )
        synth = []
        for key in sorted(eligible):
            ratings = [
                rating_by_code[c] for c in families.current_codes_by_family[key]
                if c in rating_by_code.index
            ]
            if ratings:
                # Best (alphabetically first) rating among the current codes.
                synth.append({"item_code": key, "rating": min(ratings)})
        if synth:
            bdm_aug = pd.concat([bdm_df, pd.DataFrame(synth)], ignore_index=True)

    n_members = sum(len(families.members_by_family[k]) for k in eligible)
    logger.info(
        "Successor pooling: %s member series → %s pooled family series "
        "(%s rows → %s rows); %s singleton series untouched",
        f"{n_members:,}", f"{len(eligible):,}",
        f"{is_member.sum():,}", f"{len(pooled):,}",
        f"{passthrough['item_code'].nunique():,}",
    )
    return PooledTrainingSet(out, active_aug, bdm_aug, eligible)


# ── Phase 4: the known-changeover segment ─────────────────────────────────────

def known_changeover_keys(
    families: SuccessorFamilies,
    eligible: frozenset[str],
) -> frozenset[str]:
    """
    The fit keys whose demand history spans a CONFIRMED product changeover.

    This is the segmentation variable behind `config.CHANGEPOINT_PRIOR_KNOWN_
    CHANGEOVER`. Membership requires BOTH conditions, exactly as
    `analysis.changepoint_experiment.partition()` defined them when the split was
    validated — this function is that logic moved into the pipeline, not a
    re-derivation of it:

      * the series is fitted as a pooled successor family (`key in eligible`),
        so the key really is a family and not a plain item code; AND
      * that family carries a non-null `estimated_changeover`, sourced from
        `dbo.product_successor_review` via `SuccessorFamilies.changeover_by_family`.

    A family that was pooled but whose review rows never got a date is NOT in the
    segment: there is no confirmed changeover to justify loosening its trend, so
    it keeps the standard prior. That is the same rule that produced the 19/22
    win rate the segment value was chosen on.

    Returns FIT KEYS, which is the grain `run_forecasts()` actually sees —
    pooled families ride through the fit with their family_key in the
    `item_code` column, so a fit key is a family_key for pooled families and a
    plain item_code for singletons. Singletons are never in the segment.
    """
    return frozenset(
        key for key in eligible
        if pd.notna(families.changeover_by_family.get(key, pd.NaT))
    )


def in_known_changeover_segment(
    item_code: str,
    families: SuccessorFamilies,
    eligible: frozenset[str],
) -> bool:
    """
    Whether one item_code's fitted series belongs to the known-changeover segment.

    Convenience wrapper over `known_changeover_keys` for callers holding an
    item_code rather than a fit key — every member of a pooled family shares the
    family's verdict, because the family is what gets fitted.
    """
    return families.family_key(item_code) in known_changeover_keys(families, eligible)


def scope_item_codes(
    scoped: pd.DataFrame,
    families: SuccessorFamilies,
    eligible: frozenset[str],
) -> list[str]:
    """
    Expand a pooled scope frame's keys back to real item_codes.

    Used for the unmatched-Product-Family log, which must report the actual
    item codes in scope rather than the retired family_keys standing in for them.
    """
    codes: set[str] = set()
    for key in scoped["item_code"].astype(str).str.strip().unique():
        if key in eligible:
            codes.update(families.current_codes_by_family[key])
        else:
            codes.add(key)
    return sorted(codes)


# ── Phase 5: split the family forecast back to item codes ─────────────────────

def _post_changeover(
    actuals: pd.DataFrame, changeover: pd.Timestamp
) -> pd.DataFrame:
    """Rows at or after the changeover; falls back to first month with sales."""
    if pd.notna(changeover):
        return actuals[actuals["ds"] >= changeover]
    sold = actuals[actuals["y"] > 0]
    if sold.empty:
        return actuals
    return actuals[actuals["ds"] >= sold["ds"].min()]


def _ratios_as_of(
    families: SuccessorFamilies,
    eligible: frozenset[str],
    act: pd.DataFrame,
    cutoff: pd.Timestamp | None,
    label: str,
) -> pd.DataFrame:
    """
    Split ratios computed from actuals no later than `cutoff` (None = all data).

    Shares come from each code's **own item-level actuals** over a trailing
    window — deliberately not from the pooled family series, which is the
    smoothed total and carries no per-SKU information. A family whose codes do
    not all have at least MIN_RATIO_MONTHS of post-changeover sales within the
    cutoff is split equally and flagged, rather than trusting a mix inferred
    from one or two months.

    Returns: family_key, item_code, ratio, method.
    """
    if cutoff is not None:
        act = act[act["ds"] <= cutoff]

    rows: list[dict] = []
    n_equal = 0
    for key in sorted(eligible):
        current = families.current_codes_by_family[key]

        # One-for-one rename: the family's demand belongs entirely to one code.
        if len(current) == 1:
            rows.append({"family_key": key, "item_code": current[0],
                         "ratio": 1.0, "method": SPLIT_NA})
            continue

        fam_act = act[act["item_code"].isin(current)]
        post = _post_changeover(fam_act, families.changeover_by_family.get(key, pd.NaT))

        months_sold = (
            post[post["y"] > 0].groupby("item_code")["ds"].nunique()
            .reindex(list(current)).fillna(0).astype(int)
        )
        thin = months_sold[months_sold < MIN_RATIO_MONTHS]

        window_total = pd.Series(dtype=float)
        if thin.empty and not post.empty:
            window = sorted(post["ds"].unique())[-TRAILING_WINDOW_MONTHS:]
            window_total = (
                post[post["ds"].isin(window)].groupby("item_code")["y"].sum()
                .reindex(list(current)).fillna(0.0)
            )

        if thin.empty and window_total.sum() > 0:
            ratios = window_total / window_total.sum()
            method = SPLIT_OBSERVED
        else:
            reason = (
                f"codes below {MIN_RATIO_MONTHS} post-changeover months: "
                f"{thin.to_dict()}" if not thin.empty
                else "no post-changeover sales in the trailing window"
            )
            logger.warning(
                "Successor family %s (%s): equal split [%s ratio] — %s",
                key, ", ".join(current), label, reason,
            )
            ratios = pd.Series(1.0 / len(current), index=list(current))
            method = SPLIT_EQUAL
            n_equal += 1

        for code in current:
            rows.append({"family_key": key, "item_code": code,
                         "ratio": float(ratios[code]), "method": method})

    out = pd.DataFrame(rows, columns=["family_key", "item_code", "ratio", "method"])
    n_split = sum(1 for k in eligible if len(families.current_codes_by_family[k]) > 1)
    logger.info(
        "Split ratios [%s, actuals ≤ %s]: %s families (%s split / %s rename) → %s "
        "item codes | %s split families used observed mix, %s fell back to equal",
        label, cutoff.date() if cutoff is not None else "no cutoff",
        f"{len(eligible):,}", f"{n_split:,}", f"{len(eligible) - n_split:,}",
        f"{len(out):,}", f"{n_split - n_equal:,}", f"{n_equal:,}",
    )
    return out


def compute_split_ratios(
    families: SuccessorFamilies,
    eligible: frozenset[str],
    item_actuals: pd.DataFrame,
) -> pd.DataFrame:
    """
    Each family's current codes and their share of family demand — TWICE.

    WHY TWO RATIOS
    ──────────────
    A pooled family is fitted as one series and its forecast is divided back
    out by an observed mix ratio. Which actuals that mix may be observed from
    depends entirely on what the resulting number is used for:

    `test_ratio`   — observed from actuals up to `config.TRAIN_END` only, the
        same data the Prophet fit itself saw. Used for every frame that is
        scored on the held-out test window (test_validation.csv,
        model_metrics.csv). A single ratio computed from data running to
        `TEST_END` would be derived from inside the very window being scored:
        the split would already "know" how the family's demand actually divided
        during the test months, and the resulting test-window accuracy would be
        optimistic in a way no honest backtest can be.

        The tell was visible in the old `model_metrics.csv`: 6 of the 14 split
        families reported an IDENTICAL `bias_pct` for every one of their codes.
        That can only happen when each code's realised share of test-window
        demand exactly equals the ratio it was split by — which is not a
        coincidence, it is the trailing window and the scored window being the
        same months. Under `test_ratio` that drops to 0 of 14.

    `forward_ratio` — observed from the freshest actuals available (whatever
        `prepare()` truncates at; no cutoff is imposed here). Used for the
        genuine forward forecast. There is no scoring-integrity problem here:
        the forward months are not in the data at all, so using every observed
        month up to the boundary is simply the best available estimate of the
        current mix, which is exactly what the business wants applied to a
        forecast of the future.

    Note the boundary precisely: `test_ratio` includes `TRAIN_END` itself. That
    month is inside the training window — the model is fitted on it — so
    observing the mix from it is not look-ahead. The line that must not be
    crossed is `TEST_START`.

    Returns one row per (family_key, item_code) with columns:
        family_key, item_code,
        test_ratio, test_split_method,
        forward_ratio, forward_split_method,
        ratio_delta      — forward_ratio - test_ratio, i.e. how much the
                           observed mix moved across the test window; a large
                           value is the size of the advantage the old
                           single-ratio code was handing itself,
        split_ratio      — RETAINED, and equal to `forward_ratio`. The old
                           single column was computed with no cutoff, so this
                           is its original meaning unchanged for the forward
                           forecast (the only place it was ever legitimate).
                           Prefer the explicit columns in new code.
    """
    cols = ["family_key", "item_code", "test_ratio", "test_split_method",
            "forward_ratio", "forward_split_method", "ratio_delta", "split_ratio"]
    if not eligible:
        return pd.DataFrame(columns=cols)

    act = item_actuals[["item_code", "ds", "y"]].copy()
    act["item_code"] = act["item_code"].astype(str).str.strip()
    act["ds"] = pd.to_datetime(act["ds"])

    train_end = pd.Timestamp(config.TRAIN_END + "-01")
    test = _ratios_as_of(families, eligible, act, train_end, BASIS_TEST)
    forward = _ratios_as_of(families, eligible, act, None, BASIS_FORWARD)

    out = (
        test.rename(columns={"ratio": "test_ratio", "method": "test_split_method"})
        .merge(
            forward.rename(columns={"ratio": "forward_ratio",
                                    "method": "forward_split_method"}),
            on=["family_key", "item_code"], how="outer",
        )
    )
    out["ratio_delta"] = (out["forward_ratio"] - out["test_ratio"]).round(6)
    out["split_ratio"] = out["forward_ratio"]

    moved = out[out["ratio_delta"].abs() > 0.05]
    if not moved.empty:
        logger.info(
            "Split ratios: %s of %s codes moved >5pp between the test-anchored and "
            "forward ratios (max %.1fpp, family %s) — that gap is the look-ahead the "
            "old single ratio was silently taking credit for.",
            f"{len(moved):,}", f"{len(out):,}",
            out["ratio_delta"].abs().max() * 100,
            out.loc[out["ratio_delta"].abs().idxmax(), "family_key"],
        )
    n_disagree = (out["test_split_method"] != out["forward_split_method"]).sum()
    if n_disagree:
        logger.info(
            "Split ratios: %s code(s) fall back to an equal split on the test "
            "anchor but not on the forward anchor (or vice versa) — the extra "
            "post-changeover months are only knowable after TRAIN_END.",
            f"{n_disagree:,}",
        )
    return out[cols]


def _basis_ratios(ratios: pd.DataFrame, basis: str) -> pd.DataFrame:
    """
    Narrow the two-ratio frame to the one ratio `basis` calls for.

    Returns family_key, item_code, split_ratio, split_method, ratio_basis — so
    every consumer sees a single unambiguous `split_ratio`, and `ratio_basis`
    records on the row itself which of the two it is. That column is what makes
    "which ratio was used where" answerable from the output CSVs alone rather
    than by reading this module.
    """
    if basis not in (BASIS_TEST, BASIS_FORWARD):
        raise ValueError(f"basis must be {BASIS_TEST!r} or {BASIS_FORWARD!r}, got {basis!r}")
    if ratios.empty:
        return pd.DataFrame(columns=["family_key", "item_code", "split_ratio",
                                     "split_method", "ratio_basis"])

    out = ratios[["family_key", "item_code", f"{basis}_ratio", f"{basis}_split_method"]].copy()
    out = out.rename(columns={f"{basis}_ratio": "split_ratio",
                              f"{basis}_split_method": "split_method"})
    out["ratio_basis"] = basis
    return out


def split_forecast(
    df: pd.DataFrame,
    families: SuccessorFamilies,
    ratios: pd.DataFrame,
    item_actuals: pd.DataFrame | None = None,
    *,
    basis: str = BASIS_FORWARD,
) -> pd.DataFrame:
    """
    Turn a family-keyed prediction frame back into a plain item_code frame.

    * singleton families  — passthrough; the code is already correct
    * one-for-one renames — relabelled, ratio 1.0, no ratio maths
    * split families      — one row per current code per month, with
                            yhat/yhat_lower/yhat_upper scaled by the code's share

    `basis` picks which ratio from `compute_split_ratios` to divide by, and MUST
    match what the frame is for: BASIS_TEST for the held-out test frame (which
    gets scored, so its ratio may not have seen the test window) and
    BASIS_FORWARD for the forward forecast. See compute_split_ratios.

    Adds `family_key`, `split_method`, `split_ratio` and `ratio_basis` for
    traceability.

    When `item_actuals` is supplied and the frame carries an `actual` column
    (i.e. test_df), each emitted row's `actual` is replaced with that item
    code's OWN observed actual rather than a ratio-scaled slice of the family
    total — the actual is a measured fact per SKU, so splitting it would be
    inventing data and would make the benchmark MAPE meaningless.
    """
    if df.empty:
        return df

    ratios = _basis_ratios(ratios, basis)

    out = df.copy()
    out["item_code"] = out["item_code"].astype(str).str.strip()

    keyed = frozenset(ratios["family_key"]) if not ratios.empty else frozenset()
    is_pooled = out["item_code"].isin(keyed)

    singles = out[~is_pooled].copy()
    singles["family_key"] = singles["item_code"]
    singles["split_method"] = SPLIT_NA
    singles["split_ratio"] = 1.0
    singles["ratio_basis"] = SPLIT_NA

    pooled = out[is_pooled].copy()
    if pooled.empty:
        return singles.reset_index(drop=True)

    pooled = pooled.rename(columns={"item_code": "family_key"}).merge(
        ratios, on="family_key", how="inner"
    )
    for col in ("yhat", "yhat_lower", "yhat_upper"):
        if col in pooled.columns:
            pooled[col] = (pooled[col] * pooled["split_ratio"]).round(2)

    if "actual" in pooled.columns:
        if item_actuals is not None and not item_actuals.empty:
            act = item_actuals[["item_code", "ds", "y"]].copy()
            act["item_code"] = act["item_code"].astype(str).str.strip()
            act["ds"] = pd.to_datetime(act["ds"])
            act = act.rename(columns={"y": "_own_actual"})
            pooled["ds"] = pd.to_datetime(pooled["ds"])
            pooled = pooled.merge(act, on=["item_code", "ds"], how="left")
            # Fall back to the ratio-scaled family actual only where the code has
            # no row at all for that month (i.e. genuinely no data).
            pooled["actual"] = pooled["_own_actual"].fillna(
                (pooled["actual"] * pooled["split_ratio"]).round(2)
            )
            pooled = pooled.drop(columns=["_own_actual"])
        else:
            pooled["actual"] = (pooled["actual"] * pooled["split_ratio"]).round(2)

    result = pd.concat([singles, pooled[singles.columns]], ignore_index=True)
    logger.info(
        "Split %s family-keyed rows → %s item-level rows (%s passthrough) [%s ratio]",
        f"{is_pooled.sum():,}", f"{len(pooled):,}", f"{len(singles):,}", basis,
    )
    return result


def split_metrics(
    metrics_df: pd.DataFrame,
    split_test_df: pd.DataFrame,
    ratios: pd.DataFrame,
    *,
    basis: str = BASIS_TEST,
) -> pd.DataFrame:
    """
    Relabel per-family metrics to per-item metrics.

    Singleton rows pass through byte-for-byte (only the three traceability
    columns are added), so a run with pooling enabled is identical to one
    without for every unaffected item.

    For pooled families the accuracy metrics are RECOMPUTED from the split test
    rows — each code's split forecast against that code's own actual — rather
    than scaled from the family-level numbers. Scaling would report a MAPE that
    was never measured against the SKU it is attributed to. `n_train_months`
    is inherited from the pooled family series (that is what was fitted) and
    `family_key` records where it came from.

    `basis` defaults to BASIS_TEST because these metrics ARE the test-window
    score — the whole point of the two-ratio split. `split_test_df` must have
    been produced by `split_forecast(..., basis=BASIS_TEST)` for the recomputed
    numbers to be consistent with the ratio recorded alongside them.
    """
    if metrics_df.empty:
        return metrics_df

    ratios = _basis_ratios(ratios, basis)

    out = metrics_df.copy()
    out["item_code"] = out["item_code"].astype(str).str.strip()

    keyed = frozenset(ratios["family_key"]) if not ratios.empty else frozenset()
    is_pooled = out["item_code"].isin(keyed)

    singles = out[~is_pooled].copy()
    singles["family_key"] = singles["item_code"]
    singles["split_method"] = SPLIT_NA
    singles["split_ratio"] = 1.0
    singles["ratio_basis"] = SPLIT_NA

    pooled = out[is_pooled].copy()
    if pooled.empty:
        return singles.reset_index(drop=True)

    pooled = pooled.rename(columns={"item_code": "family_key"}).merge(
        ratios, on="family_key", how="inner"
    )

    metric_cols = ["mae", "rmse", "mape_pct", "wape_pct", "bias_pct"]
    if not split_test_df.empty:
        by_item = {
            code: grp.dropna(subset=["actual", "yhat"])
            for code, grp in split_test_df.groupby("item_code")
        }
        recomputed, n_test = [], []
        for code in pooled["item_code"]:
            grp = by_item.get(code)
            if grp is None or grp.empty:
                recomputed.append({c: float("nan") for c in metric_cols})
                n_test.append(0)
            else:
                recomputed.append(calc_metrics(grp["actual"], grp["yhat"]))
                n_test.append(len(grp))
        rec = pd.DataFrame(recomputed, index=pooled.index)
        for col in metric_cols:
            if col in pooled.columns:
                pooled[col] = rec[col]
        if "n_test_months" in pooled.columns:
            pooled["n_test_months"] = n_test

    result = pd.concat([singles, pooled[singles.columns]], ignore_index=True)
    logger.info(
        "Metrics: %s family rows → %s item rows (%s passthrough)",
        f"{is_pooled.sum():,}", f"{len(pooled):,}", f"{len(singles):,}",
    )
    return result
