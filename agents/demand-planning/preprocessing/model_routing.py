"""
Model routing: which forecaster each active-scope item should go to, and why.

WHAT THIS REPLACES
──────────────────
This decision already exists — as Sheet 9 of the reporting workbook, assembled
by hand from several separate diagnostics. Hand-assembly is fine once; it is not
fine as the thing a hybrid pipeline is steered by, because nothing records which
version of which diagnostic each verdict was read from, and the whole sheet has
to be rebuilt from scratch every time any input moves. This module is that sheet
as a function: same decision, same inputs, derived from the pipeline's own
`scope` / `family_pool` logic rather than re-typed alongside it.

WHAT IT DOES *NOT* DECIDE
─────────────────────────
Nothing here fits anything. `Naive-3mo` and `Naive-12mo` are routing labels, not
a claim that the averaging is implemented — it is not, exactly as `Croston-TSB`
never was before them. A row routed there is a statement about which model the
demand pattern calls for and, for these two, over how many trailing months.

The two naive routes replaced a single `Croston-TSB` label because that label
was never backed by an implementation, and `analysis/croston_experiment.py`
found no Croston-family variant worth building one for: on this population a
plain trailing average beat every Croston/TSB variant, and rerun inside trend
subgroups (that file's Part A') the winning WINDOW differed — short on the
declining items, long on the stable, growing and no-prior-window ones. That
split is the whole content of the choice between the two labels here.

Trend-disconnect status (`analysis/trend_audit.py`) is deliberately absent. It
separates "Prophet is fine" from "Prophet is fitted but worth a business
review", and both of those route to Prophet — so it is a reporting overlay on
this output, not an input to it.

THE GATES, IN PRIORITY ORDER
────────────────────────────
Top to bottom, first match wins, mirroring the order `scope.apply_scope()`
already applies its own filters in. The ordering carries meaning: a retired code
is not reported as "insufficient history", because the reason it gets no
forecast is that it is retired, and no amount of history would change that.

  1. retired code            → Blocked  'retired, no forecast needed'
  2. not on active_products  → Blocked  'not currently active'
  3. Amazon channel mismatch → Blocked  'needs Amazon sell-out data'
  4. category 'No Data'      → Blocked  'no sales history'
  5. category 'Insufficient' → Blocked  'insufficient history to classify yet'
  6. Smooth / Erratic        → Prophet if the FIT KEY clears MIN_TRAIN_MONTHS,
                               else Blocked '…insufficient calendar history'
  7. Lumpy / Intermittent    → a naive trailing average if the fit key clears
                               CROSTON_MIN_OCCURRENCES, else Blocked. Which
                               window is `decline_ratio` (from
                               `classify_scope()`) against 1.0: below it the
                               series is selling less than it was and gets
                               Naive-3mo, at-or-above it — and where the ratio
                               is undefined for want of a prior window — it gets
                               Naive-12mo. Both the cut and the two windows come
                               from `analysis/croston_experiment.py`; nothing is
                               re-derived here.

Gates 6 and 7 block for opposite reasons and neither block is permanent: 6 is
the "once mature" case (the pattern is right, the calendar is short) and 7 is
the "not enough occurrences yet" case. Both are re-evaluated on every run, so an
item leaves the blocked set by itself as data arrives.

TWO THRESHOLDS, TWO DIFFERENT COUNTS
────────────────────────────────────
`MIN_TRAIN_MONTHS` counts CALENDAR months of training data; a Prophet fit needs
two full yearly cycles to have a seasonality to find. `CROSTON_MIN_OCCURRENCES`
counts NON-ZERO months; Croston does not care how long the calendar is, it cares
how many demand events it has to estimate a size and an interval from. Those are
different numbers for the same series and are not interchangeable — hence
`n_qualifying_months` on every row, alongside `qualifying_basis` naming which of
the two it is. A bare count with no basis is ambiguous between the two gates.

NEAR-THRESHOLD IS CARRIED, NEVER APPLIED
────────────────────────────────────────
`near_threshold` (from `demand_classification`) rides through on every row
regardless of which gate fired, and changes no route. A Smooth item sitting 1%
from the Erratic cut-off still routes to Prophet — but it routes there visibly
flagged as a close call, rather than being reported with a confidence the
underlying statistic does not support.

GATES 1-3 AND WHERE THE INPUT COMES FROM
────────────────────────────────────────
`classify_scope()`'s output has already had gates 1-3 applied upstream: it is
built at current-code grain from the active allow-list minus channel-mismatch
exclusions. Fed that frame, gates 1-3 will match nothing, and that is the
correct result rather than a sign they are dead code — they are evaluated here
so the routing verdict is complete on its own terms and stays correct if this is
ever handed a wider item universe. `route_scope()` logs how many rows each of
those three gates actually caught, so "zero" is an observation in the run log
rather than an assumption.

READ-ONLY, AND NOW A PIPELINE STAGE
───────────────────────────────────
Still read-only and still fits nothing — but `main.py` now calls `route_scope()`
on every run, and `models/naive_model.py` acts on the result: a row routed to
`Naive-3mo` or `Naive-12mo` becomes real forecast rows in
`forecast_<window>.csv`, carrying the route label in a `model` column. So the
opening claim above is now literally true in both directions: this module still
decides only WHICH model and over what window, and something else does the
arithmetic — but that something else exists, and the verdict here is delivered
rather than reported.

Two things this does NOT make load-bearing. `Prophet` rows change nothing: the
Prophet fit is still chosen by `scope.apply_scope()` upstream, not by this
ladder, so a Prophet verdict here remains a description of what the pipeline
does elsewhere. And `Blocked` rows still produce nothing anywhere — they are the
absence of a forecast, which is what they always described.

`analysis/model_routing_report.py` remains the standalone runner for inspecting
this output and its diagnostics on their own.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

import pandas as pd

import config
from preprocessing import demand_classification as dc
from preprocessing import family_pool, scope

logger = logging.getLogger(__name__)

# ── Route labels ──────────────────────────────────────────────────────────────
PROPHET = "Prophet"
# Two labels, one route: a naive trailing average over the last N months. The
# window is the only thing that differs, and `decline_ratio` picks it — see
# gate 7 in the module docstring, and analysis/croston_experiment.py Part A' for
# the comparison that chose the two numbers.
NAIVE_SHORT = "Naive-3mo"
NAIVE_LONG = "Naive-12mo"
BLOCKED = "Blocked"

ROUTES = (PROPHET, NAIVE_SHORT, NAIVE_LONG, BLOCKED)

# ── Block reasons ─────────────────────────────────────────────────────────────
# One string per gate. Kept as constants because these are read by eye in a
# reporting workbook and drift between runs would look like a change in the data.
REASON_RETIRED = "retired, no forecast needed"
REASON_INACTIVE = "not currently active"
REASON_AMAZON = "needs Amazon sell-out data"
REASON_NO_HISTORY = "no sales history"
REASON_UNCLASSIFIED = "insufficient history to classify yet"
REASON_SHORT_CALENDAR = (
    f"Prophet-suitable pattern but insufficient calendar history "
    f"(<{config.MIN_TRAIN_MONTHS}mo)"
)
# The model-family claim is gone from this string — the route is no longer
# Croston/TSB — but the occurrence fact it reports is unchanged, and so is the
# threshold behind it. Whether a naive average should be gated on an occurrence
# count AT ALL is an open question; see config.CROSTON_MIN_OCCURRENCES.
REASON_FEW_OCCURRENCES = (
    "Lumpy/Intermittent pattern but insufficient sale occurrences yet"
)

#: Blocks that data alone will clear, given time. Distinguished from the
#: structural blocks (retired / inactive / Amazon) because the two want opposite
#: follow-ups: one is a waiting list, the other is a decision someone has made.
TEMPORARY_REASONS = frozenset({REASON_SHORT_CALENDAR, REASON_FEW_OCCURRENCES})

# ── What `n_qualifying_months` is counting on a given row ──────────────────────
BASIS_TRAIN_MONTHS = "calendar_train_months"   # vs config.MIN_TRAIN_MONTHS
BASIS_NONZERO_MONTHS = "nonzero_months"        # vs CROSTON_MIN_OCCURRENCES etc.
BASIS_NONE = "n/a"                             # gate fired before any count

COLUMNS = ["item_code", "family_key", "is_pooled", "category", "near_threshold",
           "currently_fitted", "route", "block_reason", "n_qualifying_months",
           "qualifying_basis", "threshold_applied"]


# ── Inputs derived from the pipeline's own functions ──────────────────────────

def retired_codes(families: family_pool.SuccessorFamilies) -> frozenset[str]:
    """
    Every code that `family_pool` resolved as a RETIRED member of a family.

    Retired means "in a resolved family and not one of that family's current
    codes" — the same member/current split `SuccessorFamilies` already carries,
    so this cannot disagree with what the pooling actually did.

    Note what this inherits: `_normalise_map()` drops pairings whose status is in
    `POOLING_EXCLUDED_STATUSES` ('Old code still selling' — the changeover has
    not happened) and the families in `EXCLUDED_OLD_ITEM_CODES` (an unconfirmed
    data-quality issue). Codes dropped there are not in any resolved family and
    so are not retired as far as this gate is concerned. That is the intended
    reading: 'retired, no forecast needed' should follow the same resolution the
    pipeline pools on, not a second, looser definition of retirement maintained
    here. `route_scope()` logs any such code that shows up in the routed set.
    """
    out: set[str] = set()
    for key, members in families.members_by_family.items():
        current = set(families.current_codes_by_family.get(key, ()))
        out.update(set(members) - current)
    return frozenset(out)


def unresolved_retired_codes(
    successor_map: pd.DataFrame, families: family_pool.SuccessorFamilies
) -> frozenset[str]:
    """
    Old codes present in the raw successor map but NOT in any resolved family.

    Purely a diagnostic — nothing routes on it. These are the codes the pooling
    exclusions above deliberately left as singletons; surfacing them keeps the
    gap between "retired in the source table" and "retired per family_pool"
    visible instead of buried in this module's choice of definition.

    It matters for reading the OTHER end of such a family too. When a family is
    left unpooled, its successor codes are routed on their own short histories,
    so they land in the 'insufficient calendar history' block for a reason that
    is really the pooling exclusion, not the product. `route_scope()` logs both
    ends when it is given the map.
    """
    if successor_map is None or successor_map.empty:
        return frozenset()
    olds = set(successor_map["old_item_code"].astype(str).str.strip())
    olds.discard("")
    return frozenset(olds - set(families.family_key_by_item))


def train_month_counts(pooled_data: pd.DataFrame) -> pd.Series:
    """
    Distinct training months per FIT KEY, and the set that clears the gate.

    `scope.forecastable_series()` applies exactly this rule but returns the
    filtered frame, not the counts, and this module needs the counts themselves
    for `n_qualifying_months`. So the counts are computed here and the resulting
    pass-set is CHECKED against what `forecastable_series()` actually keeps — if
    that function's rule ever changes, the mismatch is logged rather than
    silently reproducing a stale copy of it here.
    """
    train_end = pd.Timestamp(config.TRAIN_END + "-01")
    df = pooled_data.copy()
    df["item_code"] = df["item_code"].astype(str).str.strip()
    df["ds"] = pd.to_datetime(df["ds"])
    counts = (
        df[df["ds"] <= train_end]
        .groupby(config.GROUP_COLS)["ds"].nunique()
    )
    counts.index = counts.index.get_level_values(0) if counts.index.nlevels > 1 else counts.index

    derived = set(counts[counts >= config.MIN_TRAIN_MONTHS].index)
    actual = set(
        scope.forecastable_series(df)["item_code"].astype(str).str.strip().unique()
    )
    if derived != actual:
        logger.warning(
            "model_routing: the MIN_TRAIN_MONTHS pass-set derived here disagrees "
            "with scope.forecastable_series() — %s only here, %s only there. "
            "scope.forecastable_series() is authoritative; this module's count "
            "logic needs re-syncing.",
            sorted(derived - actual)[:10], sorted(actual - derived)[:10],
        )
    return counts


def fitted_items_from_metrics(out_dir: Path | str) -> frozenset[str]:
    """
    Item codes the last real pipeline run actually fitted, from model_metrics.csv.

    This is an observation of a PAST run, not a recomputation of eligibility —
    which is exactly what makes it useful as a check: an item that the routing
    logic blocks but that the last run fitted means something moved between the
    two, and that is worth knowing about.
    """
    path = Path(out_dir) / "model_metrics.csv"
    if not path.exists():
        logger.warning(
            "model_routing: %s not found — `currently_fitted` will be False on "
            "every row and the fitted-vs-route cross-check is not meaningful.",
            path,
        )
        return frozenset()
    m = pd.read_csv(path, usecols=["item_code"])
    return frozenset(m["item_code"].astype(str).str.strip())


# ── The decision ──────────────────────────────────────────────────────────────

def route_one(
    row: pd.Series,
    *,
    is_retired: bool,
    is_active: bool,
    is_channel_mismatch: bool,
    n_train_months: int,
    decline_ratio: float | None,
) -> tuple[str, str, float | None, str, int | None]:
    """
    The gate ladder for ONE item. Returns
    (route, block_reason, n_qualifying_months, qualifying_basis, threshold).

    Split out from `route_scope` so the priority order is readable top-to-bottom
    in one place, and so a single item's verdict can be re-derived in isolation
    when someone disputes it.
    """
    category = row["category"]
    n_nonzero = int(row.get("n_nonzero_months") or 0)

    # 1-3: structural gates. Nothing about the demand pattern can overturn these,
    # so they are asked first and no count is attached — the item is not being
    # measured against a threshold, it is out of scope.
    if is_retired:
        return BLOCKED, REASON_RETIRED, None, BASIS_NONE, None
    if not is_active:
        return BLOCKED, REASON_INACTIVE, None, BASIS_NONE, None
    if is_channel_mismatch:
        return BLOCKED, REASON_AMAZON, None, BASIS_NONE, None

    # 4-5: the classifier's two refusals. Both are occurrence-count facts, so
    # the count that produced them travels with the row.
    if category == dc.NO_DATA:
        return (BLOCKED, REASON_NO_HISTORY, n_nonzero, BASIS_NONZERO_MONTHS,
                config.SB_MIN_MONTHS_TO_CLASSIFY)
    if category == dc.INSUFFICIENT:
        return (BLOCKED, REASON_UNCLASSIFIED, n_nonzero, BASIS_NONZERO_MONTHS,
                config.SB_MIN_MONTHS_TO_CLASSIFY)

    # 6: Prophet-suitable patterns, gated on CALENDAR history.
    if category in (dc.SMOOTH, dc.ERRATIC):
        if n_train_months >= config.MIN_TRAIN_MONTHS:
            return (PROPHET, "", n_train_months, BASIS_TRAIN_MONTHS,
                    config.MIN_TRAIN_MONTHS)
        return (BLOCKED, REASON_SHORT_CALENDAR, n_train_months,
                BASIS_TRAIN_MONTHS, config.MIN_TRAIN_MONTHS)

    # 7: intermittent-demand patterns, gated on OCCURRENCES. The gate decides
    # WHETHER; `decline_ratio` decides only WHICH WINDOW, and cannot block
    # anything on its own — an item with no ratio is still routed.
    if category in (dc.LUMPY, dc.INTERMITTENT):
        if n_nonzero >= config.CROSTON_MIN_OCCURRENCES:
            # None = no full prior window to divide by. It falls through to the
            # long window deliberately: croston_experiment's insufficient_history
            # bucket picked the 12-month mean clearly, the same as the stable and
            # growing items did. Defaulting an unknown trend to "declining" would
            # be the one reading the experiment does not support.
            route = (NAIVE_SHORT if decline_ratio is not None
                                    and decline_ratio < 1.0
                     else NAIVE_LONG)
            return (route, "", n_nonzero, BASIS_NONZERO_MONTHS,
                    config.CROSTON_MIN_OCCURRENCES)
        return (BLOCKED, REASON_FEW_OCCURRENCES, n_nonzero,
                BASIS_NONZERO_MONTHS, config.CROSTON_MIN_OCCURRENCES)

    # Unreachable while `category` comes from dc.CATEGORIES. A new label added
    # there must be routed deliberately, not defaulted into a model.
    raise ValueError(
        f"model_routing: no gate handles category {category!r} for item "
        f"{row['item_code']!r}. Add it to the ladder in route_one()."
    )


def route_scope(
    classes: pd.DataFrame,
    pooled_data: pd.DataFrame,
    active_products: pd.DataFrame,
    families: family_pool.SuccessorFamilies,
    channel_mismatch_excluded: set[str] | None = None,
    fitted_items: Iterable[str] | None = None,
    successor_map: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    One routing row per item in `classes`.

    Args:
        classes     — `demand_classification.classify_scope()`'s output.
        pooled_data — the same `family_pool.pool_for_training(...).data` that
                      produced `classes`, so MIN_TRAIN_MONTHS is measured on the
                      FIT KEY's series (the pooled family series for a pooled
                      item) rather than on the member code's own short history.
                      This is the whole reason pooling exists; measuring it
                      per-code would block every successor it was built to save.
        active_products — the allow-list. Either the raw frame or
                      `pool_for_training`'s augmented copy works: the augmented
                      copy adds family_keys, which are retired codes, and gate 1
                      catches those before gate 2 is asked.
        families    — resolved `SuccessorFamilies` (gate 1, and is_pooled).
        channel_mismatch_excluded — `scope.detect_channel_mismatch()`'s set.
        fitted_items — item codes from the last run's model_metrics.csv. Labels
                      rows only; never filters and never affects a route.
        successor_map — the raw map, for the unresolved-retired diagnostic only.
                      Optional; nothing routes on it. See
                      `unresolved_retired_codes()`.

    Returns a frame with `COLUMNS`, sorted by item_code.
    """
    if classes.empty:
        return pd.DataFrame(columns=COLUMNS)

    retired = retired_codes(families)
    active = set(active_products["item_code"].astype(str).str.strip())
    mismatch = set(channel_mismatch_excluded or ())
    fitted = frozenset(str(c).strip() for c in (fitted_items or ()))
    counts = train_month_counts(pooled_data)

    rows: list[dict] = []
    gate_hits = {REASON_RETIRED: 0, REASON_INACTIVE: 0, REASON_AMAZON: 0}

    for _, row in classes.iterrows():
        item = str(row["item_code"]).strip()
        fit_key = str(row["family_key"]).strip()

        route, reason, n_qual, basis, threshold = route_one(
            row,
            is_retired=item in retired,
            is_active=item in active,
            is_channel_mismatch=item in mismatch,
            n_train_months=int(counts.get(fit_key, 0)),
            # Carried on `classes` by demand_classification.COLUMNS, so there is
            # no second input to keep in step with the classification it came
            # from. `.get` because an older cached classification frame will not
            # have the column — that routes everything to the long window, which
            # is visible in the mix rather than silently wrong per item.
            decline_ratio=row.get("decline_ratio"),
        )
        if reason in gate_hits:
            gate_hits[reason] += 1

        rows.append({
            "item_code": item,
            "family_key": fit_key,
            "is_pooled": bool(row["is_pooled"]),
            "category": row["category"],
            "near_threshold": bool(row["near_threshold"]),
            "currently_fitted": item in fitted,
            "route": route,
            "block_reason": reason,
            "n_qualifying_months": n_qual,
            "qualifying_basis": basis,
            "threshold_applied": threshold,
        })

    out = pd.DataFrame(rows)[COLUMNS].sort_values("item_code").reset_index(drop=True)

    mix = out["route"].value_counts().to_dict()
    logger.info(
        "Model routing: %s items | %s | thresholds: MIN_TRAIN_MONTHS=%s (calendar "
        "months), CROSTON_MIN_OCCURRENCES=%s (non-zero months, PROVISIONAL)",
        f"{len(out):,}",
        ", ".join(f"{r} {mix.get(r, 0)}" for r in ROUTES),
        config.MIN_TRAIN_MONTHS, config.CROSTON_MIN_OCCURRENCES,
    )
    logger.info(
        "Model routing: structural gates 1-3 caught %s retired / %s inactive / "
        "%s channel-mismatch. Zero is expected when the input is "
        "classify_scope()'s output, which has all three applied upstream.",
        gate_hits[REASON_RETIRED], gate_hits[REASON_INACTIVE],
        gate_hits[REASON_AMAZON],
    )

    if successor_map is not None:
        unresolved = unresolved_retired_codes(successor_map, families)
        seen = unresolved & set(out["item_code"])
        if seen:
            logger.info(
                "Model routing: %s routed item(s) are old_item_codes in the raw "
                "successor map that family_pool left unresolved (pooling status "
                "exclusion or EXCLUDED_OLD_ITEM_CODES), so gate 1 does not treat "
                "them as retired: %s",
                len(seen), sorted(seen),
            )
        orphaned = {
            code for key in unresolved
            for code in successor_map.loc[
                successor_map["old_item_code"].astype(str).str.strip() == key,
                "new_item_code"].astype(str).str.strip()
        } & set(out.loc[out["block_reason"] == REASON_SHORT_CALENDAR, "item_code"])
        if orphaned:
            logger.info(
                "Model routing: %s item(s) blocked for short calendar history are "
                "successors of an UNPOOLED family — they are routed on their own "
                "launch-date history because family_pool declined to pool them, "
                "not because the underlying demand is new: %s",
                len(orphaned), sorted(orphaned),
            )

    flagged = out[out["near_threshold"]]
    if not flagged.empty:
        logger.info(
            "Model routing: %s routed item(s) are within %.0f%% of an SBC "
            "cut-off and carry near_threshold=True — their route is a close "
            "call, not a verdict.",
            f"{len(flagged):,}", dc.NEAR_THRESHOLD_PCT * 100,
        )
    return out


def fitted_but_blocked(routing: pd.DataFrame) -> pd.DataFrame:
    """
    Rows the last run FITTED but that this logic blocks — should be empty.

    Not a failure mode of the routing itself: every gate here is evaluated
    against current data, while `currently_fitted` is a fact about a previous
    run. A non-empty result means something moved between the two (an item left
    the active list, tipped over the channel-mismatch thresholds, or had its
    history change) and is a finding to chase, not a number to accept.
    """
    return routing[routing["currently_fitted"] & (routing["route"] == BLOCKED)]


def once_mature(routing: pd.DataFrame) -> pd.DataFrame:
    """
    Smooth/Erratic items blocked only by short calendar history.

    The waiting list: the demand pattern is already the one Prophet handles, and
    the single thing standing between these items and a forecast is months on
    the clock. Worth reading by name — each row is a product someone is
    currently getting no forecast for and will, unprompted, start getting one.
    """
    return routing[routing["block_reason"] == REASON_SHORT_CALENDAR]
