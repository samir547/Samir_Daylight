"""
Daylight Prophet Forecast — monthly quantity at Item grain.
─────────────────────────────────────────────────────────────────
Usage:
    python main.py                 # load from DB, train, save outputs
    python main.py --cached        # reuse cached CSVs in output/ (skip DB)
    python main.py --pilot         # restrict to A-rated items (fast validation)
    python main.py --plots         # also save Prophet component plots
    python main.py --write-db      # also append results to Azure SQL
    python main.py --auto-window --write-db
                                    # roll TRAIN/TEST/FORECAST window forward from
                                    # the latest complete month before running —
                                    # the flag a scheduled/unattended run should use
    python main.py --auto-window --require-roll --write-db
                                    # same, but exit 3 (nothing forecast, nothing
                                    # written) if there is no newer complete month
                                    # than config.TEST_END to roll onto. What
                                    # run_forecast_cycle.sh calls.
    python main.py --cached --item A25090 --plots
                                    # fit + plot ONLY this item (fast single-item
                                    # debugging) — repeatable: --item A --item B

Notes:
    Items whose real demand has migrated almost entirely into Amazon (see
    config.CHANNEL_MISMATCH_* and preprocessing/scope.py) get no forecast at
    all — their non-Amazon training signal is too thin to be meaningful, even
    though the item's total demand is real. Excluded items are written to
    output/excluded_channel_mismatch.csv, checked BEFORE the Amazon filter
    below removes the rows needed to detect this.

    Amazon channel rows (channel = 'Amazon' in forecast_training_data) are
    filtered from raw before prepare(). Excluded rows are written to
    output/excluded_amazon_rows.csv for inspection.

    Retired product codes and their successors are pooled into one training
    series (preprocessing/family_pool.py) so a successor with less than
    MIN_TRAIN_MONTHS of its own history still gets a forecast. The pooling is a
    detour around the fit only — everything from step 5b onwards sees plain
    item_code grain. Ratios used to split each family forecast back out are
    written to output/successor_split_ratios.csv — TWO of them: a test-anchored
    ratio for the scored test frames and a fresh-data ratio for the forward
    forecast. Every output row records which one it was split by, in
    `ratio_basis`.
"""
from __future__ import annotations

import argparse
import logging
import sys

import pandas as pd

import config
from data import loader
from preprocessing import (
    prepare, scope, family, family_pool, demand_classification, model_routing
)
from analysis.month_completeness import latest_complete_month
from analysis.window_proposal import propose as propose_window, verify_offsets
from models.prophet_model import run_forecasts, refresh_window_constants
from models.naive_model import run_naive_forecasts
from evaluation import benchmark
from evaluation.plots import save_component_plots

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("prophet_forecast")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Daylight Prophet monthly-qty forecast")
    ap.add_argument("--cached", action="store_true",
                    help="Load from cached CSVs in output/ instead of the database")
    ap.add_argument("--pilot", action="store_true",
                    help="Restrict scope to A-rated items only")
    ap.add_argument("--plots", action="store_true",
                    help="Save Prophet component plots to output/plots/")
    ap.add_argument("--write-db", action="store_true",
                    help="Append results to dbo.forecast_output / dbo.forecast_accuracy")
    ap.add_argument("--auto-window", action="store_true",
                    help="Recompute TRAIN_END/TEST_START/TEST_END/FORECAST_START/"
                         "FORECAST_END from the latest complete month in the pulled "
                         "training data, overriding config.py's static values for "
                         "THIS RUN ONLY (config.py on disk is never edited). Also "
                         "rolls BENCHMARK_START/BENCHMARK_END to the overlap between "
                         "the new TEST window and whatever months the pulled BDM "
                         "sheet actually covers — BDM_FORECAST_YEAR is gone, so "
                         "there is no longer a hand-maintained year to fall behind. "
                         "Lets an unattended scheduler roll every window constant "
                         "forward every run with no human retyping constants first. "
                         "benchmark.check_bdm_coverage() still runs and now checks "
                         "real BDM coverage against the rolled window rather than a "
                         "static year.")
    ap.add_argument("--require-roll", action="store_true",
                    help="Only with --auto-window. If the newest complete month in the "
                         "pulled data is not after config.TEST_END (so the window did "
                         "not roll forward, or the completeness check could not pick a "
                         "month), stop with exit code 3 BEFORE fitting or writing "
                         "anything, instead of re-forecasting the old static window "
                         "and exiting 0. Meant for scheduled runs, where a silent "
                         "no-op is worse than a loud failure.")
    ap.add_argument("--item", action="append", metavar="ITEM_CODE",
                    help="Restrict fitting/forecasting to this item_code. "
                         "Repeatable (--item A --item B) for more than one. "
                         "Resolved through the successor-family map first, so "
                         "passing a retired or successor code correctly pulls in "
                         "the whole pooled family it fits under — see "
                         "preprocessing/family_pool.py's SuccessorFamilies.family_key(). "
                         "Applied AFTER scope + naive-route exclusion, so it only "
                         "reduces how many series get fitted this run, never which "
                         "ones are eligible. Typically combined with --cached --plots "
                         "for fast single-item debugging.")
    return ap.parse_args()


# ── Data loading (with CSV cache) ─────────────────────────────────────────────

def _bdm_cache_is_stale(df: pd.DataFrame) -> bool:
    """True if a cached BDM CSV predates a schema/correctness fix.

    Two generations of bug live behind this one check:
      - No `forecast_year` column at all — predates cross-year detection
        existing in the pulled data in the first place.
      - `forecast_year` present but only ONE distinct year in it — written by
        the old single-year-filtered query (queries.BDM_FORECASTS used to bind
        :bdm_year = config.BDM_FORECAST_YEAR before that filter was removed).
        The sheet has carried 2+ planning years since 2027 planning started, so
        a genuinely fresh pull never looks like this — a lone year means the
        cache predates the fix that dropped the year filter, not that the sheet
        ran out of years. Reusing it would silently defeat the whole multi-year
        join fix even though the code now handles multi-year data correctly.
    """
    if "forecast_year" not in df.columns:
        return True
    years = pd.to_numeric(df["forecast_year"], errors="coerce").dropna().unique()
    return len(years) <= 1


def _load_one(path, load_fn, cached: bool, label: str,
              is_stale=None) -> pd.DataFrame:
    """Read one input from its CSV cache, else pull it and write the cache.

    The cache gate is PER FILE, deliberately. It used to be all-or-nothing: a
    single missing CSV re-pulled all five inputs, so invalidating one cache
    silently turned every --cached run into a full refresh. Each input is
    independent, so each decides for itself.
    """
    if cached and path.exists():
        df = pd.read_csv(path, dtype=str)
        if is_stale is not None and is_stale(df):
            logger.warning(
                "%s cache at %s is stale (pre-dates a schema/correctness fix) — "
                "re-pulling from the database; other caches are unaffected.",
                label, path,
            )
        else:
            logger.info("%s: reused cache %s", label, path.name)
            return df

    df = load_fn()
    df.to_csv(path, index=False)
    logger.info("%s: pulled from database, cached to %s", label, path.name)
    return df


def load_all(cached: bool):
    out = config.OUTPUT_DIR
    raw = _load_one(out / "raw_data.csv", loader.load_training_data,
                    cached, "training data")
    bdm_df = _load_one(out / "bdm_forecasts.csv", loader.load_bdm_forecasts,
                       cached, "BDM forecasts", is_stale=_bdm_cache_is_stale)
    active = _load_one(out / "active_products.csv", loader.load_active_products,
                       cached, "active products")
    master = _load_one(out / "master_product.csv", loader.load_master_product,
                       cached, "master product")
    successor = _load_one(out / "successor_map.csv", loader.load_successor_map,
                          cached, "successor map")

    bdm_df["bdm_forecast_qty"] = pd.to_numeric(bdm_df["bdm_forecast_qty"], errors="coerce")
    return raw, bdm_df, active, master, successor


def apply_auto_window(raw: pd.DataFrame, bdm_df: pd.DataFrame) -> None:
    """Recompute the five rolling window constants and override config.py's
    values for this run only (the file on disk is never touched). Also rolls
    BENCHMARK_START/BENCHMARK_END — see the second half of this function.

    Reuses analysis.window_proposal's own offset table and
    analysis.month_completeness's own anchor-detection — this does not
    re-decide anything or duplicate that logic, it just removes the manual
    "read proposed_window_report.md, retype five constants" step so a
    scheduler can call it unattended.

    Refuses to move the window BACKWARD or sideways: if the newest complete
    month in `raw` is not strictly after the current config.TEST_END, this
    logs a warning and leaves config.py's static values in place rather than
    silently forecasting off a no-op or a bad pull. A month failing the
    completeness check entirely (raises inside latest_complete_month) is
    handled the same way — keep the static window, warn loudly, let the run
    proceed rather than fail the whole scheduled job outright. That trade-off
    (deliver a run on the stale-but-known window, over delivering nothing) is
    a judgement call — worth revisiting if a genuinely bad pull ever produces
    a forecast nobody wanted rolled at all.
    """
    drift = verify_offsets()
    if drift:
        logger.warning(
            "--auto-window: config.py's window constants do not already follow "
            "the standard offsets — %s. Proceeding anyway: the computed override "
            "below replaces all five constants wholesale, so this drift is "
            "corrected as a side effect rather than compounded.",
            "; ".join(drift),
        )

    try:
        anchor, _evidence = latest_complete_month(raw)
    except ValueError as err:
        logger.warning(
            "--auto-window: could not determine a complete month (%s) — keeping "
            "config.py's static window for this run.", err,
        )
        return

    current_test_end = config.TEST_END
    if anchor <= current_test_end:
        logger.warning(
            "--auto-window: latest complete month (%s) is not after the current "
            "TEST_END (%s) — no new data to roll onto. Keeping config.py's static "
            "window for this run.", anchor, current_test_end,
        )
        return

    proposed = propose_window(anchor)
    shift_months = (pd.Timestamp(anchor + "-01").to_period("M")
                    - pd.Timestamp(current_test_end + "-01").to_period("M")).n
    logger.info(
        "--auto-window: rolling TEST_END %s -> %s (+%d month(s)). "
        "TRAIN_END=%s TEST_START=%s TEST_END=%s FORECAST_START=%s FORECAST_END=%s",
        current_test_end, anchor, shift_months,
        proposed["TRAIN_END"], proposed["TEST_START"], proposed["TEST_END"],
        proposed["FORECAST_START"], proposed["FORECAST_END"],
    )
    for name, value in proposed.items():
        setattr(config, name, value)

    # models/prophet_model.py and models/naive_model.py cache TRAIN_END_TS etc.
    # as module-level Timestamps computed once at import time — the setattr
    # calls above do not, by themselves, change those. Both functions also
    # self-refresh on entry, so this call is defensive rather than load-bearing,
    # but it means anything logged between here and run_forecasts() (there is
    # none today, but might be added later) sees correct values too.
    refresh_window_constants()

    # ── Roll BENCHMARK_START/END too, from the BDM sheet's OWN coverage ──────
    # This is what piece 4 of the BDM/benchmark fix is: BENCHMARK_START/END used
    # to be left untouched here because they depended on a hand-maintained
    # BDM_FORECAST_YEAR this pipeline did not own. That constant is gone —
    # queries.BDM_FORECASTS / loader.load_bdm_forecasts() now pull every
    # planning year present, and evaluation.benchmark._bdm_month_col() keys off
    # each row's own forecast_year — so the benchmark window can now be
    # computed directly from what the pulled BDM data actually covers, with no
    # human step in between.
    bdm_months = benchmark.bdm_coverage_months(bdm_df)
    if not bdm_months:
        logger.warning(
            "--auto-window: bdm_df has no usable (forecast_year, month_name) "
            "rows — leaving BENCHMARK_START/END at config.py's static values."
        )
        return

    new_start = max(config.TEST_START, min(bdm_months))
    new_end = min(config.TEST_END, max(bdm_months))
    if new_start > new_end:
        logger.warning(
            "--auto-window: rolled TEST window (%s..%s) does not overlap the "
            "BDM sheet's coverage (%s..%s) at all — leaving BENCHMARK_START/END "
            "at config.py's static values rather than writing an empty range.",
            config.TEST_START, config.TEST_END, min(bdm_months), max(bdm_months),
        )
        return

    if (new_start, new_end) != (config.BENCHMARK_START, config.BENCHMARK_END):
        logger.info(
            "--auto-window: rolling BENCHMARK_START/END %s..%s -> %s..%s "
            "(overlap of the rolled TEST window and the BDM sheet's own "
            "coverage, %s..%s).",
            config.BENCHMARK_START, config.BENCHMARK_END, new_start, new_end,
            min(bdm_months), max(bdm_months),
        )
    config.BENCHMARK_START = new_start
    config.BENCHMARK_END = new_end


def forecast_filename() -> str:
    """The forward-forecast CSV's name for the CURRENT window.

    Shared with analysis/oos_scoring.py so the reader and the writer cannot
    disagree about which file the forward forecast lives in.
    """
    return f"forecast_{config.FORECAST_START}_{config.FORECAST_END}.csv"


def _fmt_ds(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    if "ds" in frame.columns:
        frame["ds"] = pd.to_datetime(frame["ds"]).dt.strftime("%Y-%m")
    return frame


def main() -> int:
    args = parse_args()

    # --write-db appends to dbo.forecast_output / forecast_accuracy, and
    # dbo.vw_forecast_output_latest serves whichever batch has the newest
    # run_date. A run that is partial (--item, --pilot) or built from stale CSV
    # caches (--cached) must therefore never be written: it would silently
    # become "the current forecast" in Power BI.
    if args.write_db:
        blocked = [flag for flag, on in (
            ("--item", bool(args.item)),
            ("--cached", args.cached),
            ("--pilot", args.pilot),
        ) if on]
        if blocked:
            logger.error(
                "--write-db cannot be combined with %s: those runs are partial or "
                "cache-based and would overwrite the 'latest' forecast in the "
                "database. Drop --write-db for debug runs, or drop %s for a real "
                "cycle.", ", ".join(blocked), ", ".join(blocked),
            )
            return 2
    if args.require_roll and not args.auto_window:
        logger.error("--require-roll only makes sense together with --auto-window.")
        return 2

    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── 1–2. Load ─────────────────────────────────────────────────────────────
    raw, bdm_df, active, master, successor = load_all(args.cached)

    # Every config.TRAIN_END / TEST_START / TEST_END / FORECAST_START /
    # FORECAST_END read in THIS file happens live, at the point of use, so
    # overriding them here is enough for everything below. The two model
    # modules cache their own Timestamp copies at import time and cannot be
    # fixed by an override this late; they self-refresh on entry instead —
    # see apply_auto_window() and prophet_model.refresh_window_constants().
    if args.auto_window:
        test_end_before = config.TEST_END
        apply_auto_window(raw, bdm_df)
        if args.require_roll and config.TEST_END == test_end_before:
            logger.error(
                "--require-roll: the window did not roll (TEST_END is still %s). "
                "Either the newest complete month in the pulled data is not after "
                "it, or no month passed the completeness check — see the "
                "--auto-window warning above. Stopping with exit 3: nothing was "
                "forecast or written.", test_end_before,
            )
            return 3

    family_map = family.build_family_map(master)

    # item_code → region lookup for benchmark reporting, captured from raw
    # before prepare() aggregates region away.
    series_meta = prepare.series_metadata(raw)

    # ── 2a. Detect channel-mismatch items ───────────────────────────────
    # Must run on raw BEFORE the Amazon filter below — needs both channels to
    # compute each item's Amazon share. See config.py and scope.py for the
    # full rationale (discovered from U35108, 2026-08).
    channel_mismatch_excluded, channel_mismatch_diag = scope.detect_channel_mismatch(
        raw, config.TEST_END
    )
    if not channel_mismatch_diag.empty:
        cm_path = config.OUTPUT_DIR / "excluded_channel_mismatch.csv"
        channel_mismatch_diag.to_csv(cm_path, index=False)
        logger.info(
            "Channel-mismatch exclusions → %s  (%s item(s))",
            cm_path, f"{len(channel_mismatch_diag):,}",
        )

    # ── 2b. Exclude Amazon channel rows ───────────────────────────────────────
    # Must run before prepare() — channel is dropped by the groupby aggregation.
    # series_meta is intentionally built from unfiltered raw: Amazon rows in
    # series_meta are dead rows that join to nothing in benchmark.
    raw, excluded_amazon = scope.filter_amazon_channel(raw)
    if not excluded_amazon.empty:
        excl_path = config.OUTPUT_DIR / "excluded_amazon_rows.csv"
        excluded_amazon.to_csv(excl_path, index=False)
        logger.info("Excluded Amazon rows → %s  (%s rows)", excl_path, f"{len(excluded_amazon):,}")

    # ── 3. Prepare ────────────────────────────────────────────────────────────
    # `prepared` stays at item_code grain for the whole run: it is the prior-year
    # baseline's source in benchmark.build_comparison(), the per-code actuals
    # both test frames are backfilled from, and the source of the per-SKU split
    # ratios below. Pooling is applied to a copy of it, not to it.
    prepared = prepare.prepare(raw)

    # ── 3b. Successor-family pooling (detour around the per-item fit) ─────────
    # Retired codes and their successors are summed into one family series so a
    # successor with <MIN_TRAIN_MONTHS of its own history still gets a forecast.
    # The family_key rides in the item_code column through the fit and is split
    # back out at step 5b, before anything downstream sees it. Scope inputs are
    # extended (not rewritten) so scope.py needs no change — eligibility is
    # decided on each family's CURRENT codes, which are what active_products
    # actually lists.
    families = family_pool.build_successor_families(successor)
    pooled = family_pool.pool_for_training(prepared, families, active, bdm_df)

    # Demand classification + model routing, computed HERE — before scope — because
    # step 4 below now uses the routing to keep Prophet away from the Naive-routed
    # keys. Both were report-only until the naive path landed; they read the pooled
    # frame, because a pooled family's category (and hence its route, and hence its
    # average) is a fact about the COMBINED series, not about any one member code.
    classes = demand_classification.classify_scope(
        pooled.data, pooled.active_products, families, pooled.eligible,
        channel_mismatch_excluded,
    )
    routing = model_routing.route_scope(
        classes, pooled.data, pooled.active_products, families,
        channel_mismatch_excluded,
        # Last run's model_metrics.csv, read off disk. Diagnostic ONLY — it
        # feeds `currently_fitted` and the fitted_but_blocked cross-check, and
        # no route depends on it, so a briefly stale file is a slightly
        # less useful diagnostic and never a wrong forecast.
        fitted_items=model_routing.fitted_items_from_metrics(config.OUTPUT_DIR),
        successor_map=successor,
    )

    # ── 4. Scope ──────────────────────────────────────────────────────────────
    # The MIN_TRAIN_MONTHS check inside apply_scope now runs against the pooled
    # family series, which is the point of the exercise.
    scoped = scope.apply_scope(
        pooled.data, pooled.active_products, pooled.bdm_forecasts, pilot=args.pilot,
        channel_mismatch_excluded=channel_mismatch_excluded,
    )

    if scoped.empty:
        logger.error("No series remain after scope filtering — check inputs / flags.")
        return 1

    # Naive-routed keys leave Prophet's scope entirely. model_routing.py has
    # already decided these belong to the naive-average path; letting Prophet
    # also fit them would emit two conflicting forecast rows per item. The
    # route is the decision, independent of whether Prophet CAN fit the series.
    naive_fit_keys = frozenset(
        routing.loc[routing["route"].isin(config.NAIVE_WINDOW_MONTHS),
                    "family_key"].astype(str).str.strip()
    )
    before = scoped["item_code"].nunique()
    scoped = scoped[~scoped["item_code"].astype(str).str.strip()
                    .isin(naive_fit_keys)].copy()
    logger.info(
        "Excluded %s Naive-routed fit key(s) from Prophet's scope (%s -> %s "
        "series) — model_routing.py already decided these belong to the "
        "naive-average path, not Prophet.",
        len(naive_fit_keys), before, scoped["item_code"].nunique(),
    )

    # ── 4b. Optional --item filter (debugging aid, typically with --cached --plots) ──
    # Applied AFTER scope + naive exclusion, so `scoped` here is exactly the set
    # of fit keys that would otherwise reach Prophet — this only cuts down HOW
    # MANY get fitted this run, never which ones were eligible in the first place.
    if args.item:
        requested = [str(i).strip() for i in args.item if str(i).strip()]
        # A requested code may be a retired code, a successor code, or a plain
        # singleton item_code. families.family_key() resolves all three to the
        # actual FIT KEY scoped/run_forecasts() operate on (itself, for a
        # singleton) — matching on item_code directly would silently miss any
        # code that belongs to a pooled successor family.
        resolved = {code: families.family_key(code) for code in requested}
        target_keys = frozenset(resolved.values())

        before_items = scoped["item_code"].nunique()
        scoped = scoped[scoped["item_code"].astype(str).str.strip()
                        .isin(target_keys)].copy()

        # Also narrow the naive-route path to the same target set. Without this,
        # run_naive_forecasts() at step 5c still computes and forecasts EVERY
        # naive-routed item regardless of --item — harmless for --plots (a
        # naive item never gets a Prophet model to plot), but a real
        # correctness gap for --write-db: forecast_df would carry all 40+
        # naive-routed items' forecasts into dbo.forecast_output alongside the
        # one item actually requested. Filtering here keeps --item meaning
        # what it says on both paths, not just Prophet's.
        before_naive = routing["family_key"].nunique()
        routing = routing[routing["family_key"].astype(str).str.strip()
                          .isin(target_keys)].copy()

        logger.info(
            "--item filter: %s requested code(s) -> %s fit key(s) | Prophet "
            "scope %s -> %s series | naive routing %s -> %s fit key(s)",
            len(requested), len(target_keys), before_items,
            scoped["item_code"].nunique(), before_naive,
            routing["family_key"].nunique(),
        )

        remaining_keys = frozenset(scoped["item_code"].astype(str).str.strip())
        raw_codes = frozenset(raw["item_code"].astype(str).str.strip())
        for code, key in resolved.items():
            if key in remaining_keys:
                note = f"in scope as fit key {key!r}" if key != code else "in scope"
            elif key in naive_fit_keys:
                note = (
                    f"routed to a Naive trailing average (fit key {key!r}) — "
                    "--plots produces nothing for it, since no Prophet model is "
                    "ever fitted for a Naive-routed item, but its forecast will "
                    "still be produced and (if --write-db) written normally"
                )
            elif code not in raw_codes and key not in raw_codes:
                note = "not found anywhere in the pulled training data — check for a typo"
            else:
                note = (
                    f"resolved to fit key {key!r} but excluded from scope before "
                    "fitting (inactive, below MIN_TRAIN_MONTHS, channel-mismatch, "
                    "or blocked) — see scope.apply_scope's logging above, and "
                    "output/excluded_channel_mismatch.csv / "
                    "output/excluded_amazon_rows.csv"
                )
            logger.info("  --item %s: %s", code, note)

        if scoped.empty and routing.empty:
            logger.error(
                "--item filter left nothing in scope for EITHER Prophet or the "
                "naive route — see notes above."
            )
            return 1

    # ── 5. Train + forecast ───────────────────────────────────────────────────
    # Series whose family carries a CONFIRMED changeover date are fitted with a
    # looser changepoint prior — a real level shift the business already knows
    # about, which the default 0.05 regularises away as noise. Segment rule, not
    # per-SKU tuning: see config.CHANGEPOINT_PRIOR_KNOWN_CHANGEOVER.
    known_changeover = family_pool.known_changeover_keys(families, pooled.eligible)
    test_df, forecast_df, metrics_df, models = run_forecasts(scoped, known_changeover)
    # No "nothing fitted" check here: an empty `scoped` (e.g. an --item request
    # that resolved entirely to a Naive-routed item) legitimately produces empty
    # test_df/forecast_df/metrics_df/models from THIS call alone, and that is
    # not a failure — step 5c's naive path still has a real result to
    # contribute. The combined check sits after that merge, below.

    # ── 5a. Split family forecasts back to item_code grain ────────────────────
    # After this block every frame is plain item_code grain again, so
    # family.enrich(), benchmark.build_comparison() and the CSV writers below
    # need no knowledge of any of this.
    # Two ratios, deliberately: the test frames are SCORED, so they may only be
    # split by a mix observed up to TRAIN_END (`test_ratio`) — splitting them by
    # a ratio computed from data running through the test window would let the
    # split peek at the answer and quietly flatter the test-window accuracy. The
    # forward forecast has no such constraint and uses the freshest mix
    # (`forward_ratio`). See family_pool.compute_split_ratios.
    split_ratios = family_pool.compute_split_ratios(families, pooled.eligible, prepared)
    test_df     = family_pool.split_forecast(test_df, families, split_ratios, prepared,
                                             basis=family_pool.BASIS_TEST)
    forecast_df = family_pool.split_forecast(forecast_df, families, split_ratios,
                                             basis=family_pool.BASIS_FORWARD)
    metrics_df  = family_pool.split_metrics(metrics_df, test_df, split_ratios,
                                            basis=family_pool.BASIS_TEST)

    ratio_path = config.OUTPUT_DIR / "successor_split_ratios.csv"
    split_ratios.to_csv(ratio_path, index=False)
    logger.info("Successor split ratios → %s  (%s rows)", ratio_path, f"{len(split_ratios):,}")

    # ── 5b. Family enrichment (reporting tag) ─────────────────────────────────
    # Join Product Family onto every item-level output artifact. Unmatched
    # item_codes are tagged 'Unknown', not dropped or guessed.
    test_df     = family.enrich(test_df, family_map)
    forecast_df = family.enrich(forecast_df, family_map)
    metrics_df  = family.enrich(metrics_df, family_map)

    # Report real item codes, not the retired family_keys standing in for them.
    scope_items = family_pool.scope_item_codes(scoped, families, pooled.eligible)
    unmatched = family.unmatched_in_scope(scope_items, family_map)
    unmatched_path = config.OUTPUT_DIR / "unmatched_family_log.csv"
    unmatched.to_csv(unmatched_path, index=False)
    logger.info(
        "Family coverage: %s/%s in-scope items tagged, %s unmatched → %s",
        f"{len(scope_items) - len(unmatched):,}", f"{len(scope_items):,}",
        f"{len(unmatched):,}", unmatched_path,
    )

    # ── 5c. Naive-route forecasts (Lumpy / Intermittent items) ────────────────
    # The other half of the hybrid. Prophet fits Smooth/Erratic series; the
    # Lumpy/Intermittent ones it was never suited to have had a ROUTE since
    # model_routing.py landed and no forecast to go with it. This block turns
    # that route into delivered rows: a trailing average over 3 or 12 months,
    # tagged in a `model` column so Power BI can tell the two apart.
    #
    # TWO FRAMES: the same trailing level over the test window and over the
    # forward window. The test frame is what puts these items into section 6's
    # benchmark alongside Prophet's — the rolling-origin CV in
    # analysis/croston_experiment.py is still the evidence that chose the
    # method and the windows; this is the in-pipeline comparison against BDM.
    # No metrics_df analogue comes back: model_metrics.csv stays Prophet-only,
    # because there is no fit here to describe. See models/naive_model.py's
    # docstring for that boundary in full.
    #
    # `classes` / `routing` come from step 3b — the same objects step 4 used to
    # keep these keys out of Prophet's scope, so the two paths cannot disagree
    # about which items are naive. Deliberately not recomputed here.
    #
    # Source frame is pooled.data, NOT the now-filtered `scoped`: the naive
    # average needs the full pooled history, and scope's Prophet-only filters
    # (MIN_TRAIN_MONTHS especially) are exactly the ones these items fail.
    naive_test_df, naive_df = run_naive_forecasts(pooled.data, routing)
    # Same ratios, same basis as the Prophet forward split two blocks up — a
    # family's forward mix is a property of the family, not of which model
    # produced the number being split. Recomputing them here would risk two
    # different splits of the same family in one output file.
    naive_df = family_pool.split_forecast(naive_df, families, split_ratios,
                                          basis=family_pool.BASIS_FORWARD)
    naive_df = family.enrich(naive_df, family_map)

    # Same call, the OTHER basis — BASIS_TEST, because this frame gets scored
    # and its ratio may not have seen the test window. `prepared` goes in as
    # item_actuals for exactly the reason Prophet's test_df passes it: a pooled
    # family's `actual` here is the family total, and each current code must be
    # scored against its OWN measured quantity, not a ratio-scaled slice of the
    # family's.
    naive_test_df = family_pool.split_forecast(naive_test_df, families,
                                               split_ratios, prepared,
                                               basis=family_pool.BASIS_TEST)
    naive_test_df = family.enrich(naive_test_df, family_map)

    # Merged only now, with BOTH frames split by the same ratio and enriched the
    # same way, and before step 6 — so the benchmark sees one test frame that
    # already covers every forecast item, Prophet-routed and naive-routed alike,
    # each row naming its own model. metrics_df is deliberately NOT touched: it
    # stays Prophet-only, since a trailing mean has no fit to report.
    if not naive_df.empty:
        forecast_df = pd.concat([forecast_df, naive_df], ignore_index=True)
    if not naive_test_df.empty:
        test_df = pd.concat([test_df, naive_test_df], ignore_index=True)

    if test_df.empty and forecast_df.empty:
        logger.error(
            "No models were fitted and no naive-route forecast was produced — "
            "check data, MIN_TRAIN_MONTHS, and (if --item was used) the notes "
            "logged above for why the requested item(s) resolved to nothing."
        )
        return 1

    # ── 6. Benchmark ──────────────────────────────────────────────────────────
    bench_df = benchmark.build_comparison(test_df, bdm_df, prepared, series_meta)

    # ── 7. Save outputs ───────────────────────────────────────────────────────
    if not test_df.empty:
        path = config.OUTPUT_DIR / "test_validation.csv"
        _fmt_ds(test_df).to_csv(path, index=False)
        logger.info("Test validation  → %s", path)

    if not forecast_df.empty:
        # Name derived from the window, not hard-coded: the constants roll
        # forward (analysis/window_proposal.py) and a file called
        # forecast_may_oct_2026.csv holding some other six months is worse than
        # no name at all. See forecast_filename().
        path = config.OUTPUT_DIR / forecast_filename()
        _fmt_ds(forecast_df).to_csv(path, index=False)
        logger.info("Forecast results → %s", path)

    if not metrics_df.empty:
        path = config.OUTPUT_DIR / "model_metrics.csv"
        metrics_df.to_csv(path, index=False)
        logger.info("Model metrics    → %s", path)

    if not bench_df.empty:
        path = config.OUTPUT_DIR / "benchmark_comparison.csv"
        bench_df.to_csv(path, index=False)
        logger.info("Benchmark        → %s", path)

    if args.plots:
        save_component_plots(models, config.OUTPUT_DIR / "plots")

    if args.write_db:
        fc = forecast_df.copy()
        fc["run_kind"] = "forecast"
        loader.write_outputs(fc, metrics_df)

    # ── 8. Console summary ────────────────────────────────────────────────────
    print_summary(metrics_df, forecast_df, bench_df)
    return 0


# ── Console summary ───────────────────────────────────────────────────────────

def print_summary(metrics_df, forecast_df, bench_df) -> None:
    sep = "-" * 72

    if not metrics_df.empty:
        print(f"\n{sep}\nTest-set performance  ({config.TEST_START} – {config.TEST_END})\n{sep}")
        cols = [c for c in config.GROUP_COLS +
                ["n_train_months", "n_test_months", "mae", "rmse",
                 "mape_pct", "wape_pct", "bias_pct"]
                if c in metrics_df.columns]
        view = metrics_df[cols].sort_values("mape_pct")
        print(view.head(40).to_string(index=False))
        print(f"\nFitted series: {len(metrics_df):,} | "
              f"median MAPE: {metrics_df['mape_pct'].median():.1f}%")

    if not bench_df.empty:
        print(f"\n{sep}\nBenchmark vs BDM / prior year  "
              f"({config.BENCHMARK_START} – {config.BENCHMARK_END})\n{sep}")
        benchmark.summarize(bench_df)

    if not forecast_df.empty:
        print(f"\n{sep}\nForward forecast  ({config.FORECAST_START} – "
              f"{config.FORECAST_END})  — total yhat units\n{sep}")
        pivot = (
            _fmt_ds(forecast_df)
            .pivot_table(index="ds", values="yhat", aggfunc="sum")
            .round(0)
        )
        print(pivot.to_string())

    print(f"\n{sep}\nDone.")


if __name__ == "__main__":
    sys.exit(main())
