"""CLI entry point (ISSUE-04): argument parsing + a main() that only wires
phases together. Each phase is its own function, independently callable and
testable -- the old main() did all of this inline across 141 lines.

Run as:
    python -m daylight_sentiment.cli ...
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from daylight_sentiment.application.action_items import generate_action_items
from daylight_sentiment.application.aggregator import AnalysisAggregator
from daylight_sentiment.application.checkpoint import load_checkpoint
from daylight_sentiment.application.output_generator import OutputGenerator
from daylight_sentiment.application.pipeline import MultiSourcePipeline
from daylight_sentiment.config import (
    Config,
    LLMProvider,
    parse_min_reviews,
    parse_window_months,
)
from daylight_sentiment.domain.action_items import format_action_items
from daylight_sentiment.logging_setup import configure_logging as _configure_logging
from daylight_sentiment.infra.analyzers import (
    FatalProviderError,
    SentimentAnalyzer,
    build_analyzer,
)
from daylight_sentiment.infra.db import (
    REVIEWS_TABLE,
    RUNS_TABLE,
    ActionItemRepository,
    ExistingReviewsReadError,
    ReviewRepository,
    RunRepository,
    connect,
    ensure_schema,
)
from daylight_sentiment.infra.review_sources import (
    AmazonBrightDataSource,
    TrustpilotProductSource,
    TrustpilotServiceSource,
    build_default_sources,
    load_reviews,
)
from daylight_sentiment.infra.sharepoint_sync import (
    SharePointSignInRequiredError,
    SharePointSync,
    SourceSpec,
    SourceSyncResult,
)

logger = logging.getLogger("multi_source_sentiment")


def _window_months(value: str) -> tuple:
    try:
        return parse_window_months(value, name="--action-items-months")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _min_reviews(value: str) -> int:
    try:
        return parse_min_reviews(value, name="--action-items-min-reviews")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Multi-source sentiment pipeline")
    p.add_argument("--provider", choices=[e.value for e in LLMProvider],
                   default=LLMProvider.CLAUDE.value,
                   help="LLM provider (default: claude)")
    p.add_argument("--model", help="Override the provider's default model")
    p.add_argument("--limit", type=int, help="Only process the first N reviews (smoke test)")
    p.add_argument("--workers", type=int, default=None,
                   help="Concurrent worker threads (default 8)")
    p.add_argument("--batch-size", type=int, default=None,
                   help="Reviews per unit of work handed to a worker thread (default 10)")
    p.add_argument("--fresh-start", action="store_true",
                   help="Ignore the checkpoint and reprocess every review from scratch, "
                        "including reviews that already have a stored analysis in the "
                        "database. By default the pipeline resumes from the checkpoint and "
                        "skips reviews the database already holds a sentiment for.")
    p.add_argument("--rerun-product-reviews", action="store_true",
                   help="Reopen only product reviews (Trustpilot + Amazon) for reprocessing, "
                        "leaving service reviews untouched in the checkpoint. Use this after "
                        "changing the sector taxonomy (SECTORS), or after adding a new field "
                        "the loaders capture for product reviews (e.g. model_number) that "
                        "existing checkpoint entries predate. Product reviews already in the "
                        "database are re-analysed too. Ignored if --fresh-start is also "
                        "passed, since that already reprocesses everything.")
    p.add_argument("--rerun-service-reviews", action="store_true",
                   help="Reopen only service reviews (Trustpilot) for reprocessing, leaving "
                        "product reviews untouched in the checkpoint. Mirrors "
                        "--rerun-product-reviews for the service side -- use after a prompt "
                        "change (e.g. to SENTIMENT_GUIDANCE) that should apply to service "
                        "reviews too. Service reviews already in the database are re-analysed "
                        "too. Combine both flags to reopen everything without a full "
                        "--fresh-start. Ignored if --fresh-start is also passed.")
    p.add_argument("--reanalyse-existing", action="store_true",
                   help="Analyse every loaded review, from every source, even if the "
                        "database already holds a sentiment for it (its stored result is "
                        "overwritten on load). Without it, reviews already analysed in the "
                        "database are skipped. Unlike --fresh-start the checkpoint is still "
                        "resumed from.")
    p.add_argument("--skip-sharepoint-sync", action="store_true",
                   help="Don't pull fresh files from SharePoint even if SP_* env vars are set; "
                        "use whatever's already in Reviews/")
    p.add_argument("--skip-db-load", action="store_true",
                   help="Don't load results into the database even if DB_* env vars are set")
    p.add_argument("--skip-translation", action="store_true",
                   help="Don't run the translation pass (fr/de/it/es/nl/sv/pl/cs -> English) "
                        "even if DB_* env vars are set. Runs by default on every invocation "
                        "that can reach the database, translating whatever's newly untranslated "
                        "(review_text_en IS NULL) -- independent of --fresh-start/--rerun-*, "
                        "since translation state lives in its own columns, not the sentiment "
                        "checkpoint. See daylight_sentiment/infra/translation.py.")
    p.add_argument("--load-db-only", action="store_true",
                   help="Skip SharePoint sync, loading, and analysis entirely -- just load the "
                        "existing Trustpilot_Result/*.json files into the database and exit")
    p.add_argument("--sync-sharepoint-only", action="store_true",
                   help="Only sync review files from SharePoint into Reviews/, then exit -- "
                        "skip loading, analysis, and the database entirely. Always syncs "
                        "when passed, regardless of --skip-sharepoint-sync.")
    p.add_argument("--keep-output-files", action="store_true",
                   help="Don't delete the JSON output files after a successful database load")
    p.add_argument("--skip-action-items", action="store_true",
                   help="Don't generate the top 3 business to-do items after the database "
                        "load. They are otherwise generated whenever this run analysed and "
                        "loaded new reviews.")
    p.add_argument("--action-items-months", type=_window_months, metavar="N[,N...]",
                   help="The review windows to generate to-do items for, in months, "
                        "comma-separated: one set of items for each 'last N months', "
                        "counted back from the newest review date in the database "
                        "(default 3,6,12, or ACTION_ITEMS_WINDOWS_MONTHS)")
    p.add_argument("--action-items-min-reviews", type=_min_reviews, metavar="N",
                   help="Skip a review window with fewer than N analysed reviews; "
                        "at least 10 (default 50, or ACTION_ITEMS_MIN_REVIEWS)")
    p.add_argument("--action-items-only", action="store_true",
                   help="Skip SharePoint sync, loading and analysis entirely -- just generate "
                        "the to-do items for every review window from the reviews already in "
                        "the database, then exit (1 if any window failed)")
    p.add_argument("--action-items-dry-run", action="store_true",
                   help="Generate and print every window's to-do items but don't save them "
                        "to the database. Combine with --action-items-only to preview them.")
    return p.parse_args(argv)


def build_config(args: argparse.Namespace) -> Config:
    """Config from environment + CLI overrides. Overrides apply only when the
    flag was actually passed, so tuning defaults live in one place (Config)."""
    config = Config.from_env(llm_provider=LLMProvider(args.provider))
    if args.workers is not None:
        config.max_workers = args.workers
    if args.batch_size is not None:
        config.batch_size = args.batch_size
    if args.model:
        getattr(config, config.llm_provider.value).model = args.model
    if args.action_items_months is not None:
        config.action_items.windows_months = args.action_items_months
    if args.action_items_min_reviews is not None:
        config.action_items.min_reviews = args.action_items_min_reviews
    config.output_dir.mkdir(parents=True, exist_ok=True)
    return config


def configure_logging() -> None:
    _configure_logging()


def _build_syncer(config: Config) -> SharePointSync:
    return SharePointSync(config.sp_share_url, config.sp_username, config.sp_password,
                          config.sp_client_id, config.sp_tenant)


def sharepoint_source_specs(config: Config) -> List[SourceSpec]:
    """The three review sources' "In Progress" staging folders, keyed by the
    ReviewSource.name each one feeds -- so a source whose folder is empty can
    be dropped from build_default_sources() by name (see active_sources)."""
    src = config.sources
    return [
        SourceSpec(TrustpilotProductSource.name, src.sp_trustpilot_product_dir,
                   src.trustpilot_product_pattern),
        SourceSpec(TrustpilotServiceSource.name, src.sp_trustpilot_service_dir,
                   src.trustpilot_service_pattern),
        SourceSpec(AmazonBrightDataSource.name, src.sp_amazon_product_dir,
                   src.amazon_product_pattern),
    ]


def sharepoint_processed_dirs(config: Config) -> Dict[str, str]:
    """Source key -> "Processed" folder a synced file moves to after a
    confirmed database load. Parallel to sharepoint_source_specs()."""
    src = config.sources
    return {
        TrustpilotProductSource.name: src.sp_trustpilot_product_processed_dir,
        TrustpilotServiceSource.name: src.sp_trustpilot_service_processed_dir,
        AmazonBrightDataSource.name: src.sp_amazon_product_processed_dir,
    }


def sync_sharepoint(config: Config, skip: bool) -> Optional[Dict[str, SourceSyncResult]]:
    """Pull fresh review exports from each source's SharePoint "In Progress"
    folder: the newest file matching each source's pattern is always used,
    whatever it's named; an empty folder is a skipped source.

    Returns the per-source sync results, or None when no sync happened
    (skipped / not configured) -- in which case every source runs off
    whatever is already in Reviews/, exactly as before.
    """
    if skip:
        logger.info("--skip-sharepoint-sync set; using local Reviews/ files as-is")
        return None
    if not config.sharepoint.configured:
        logger.info("SP_SHARE_URL/SP_USERNAME/SP_PASSWORD not set; using local Reviews/ files as-is")
        return None
    logger.info("Syncing review files from SharePoint (%s)", config.sp_username)
    results = _build_syncer(config).sync_from_subfolders(
        config.sources.reviews_dir, sharepoint_source_specs(config))
    synced = sorted(k for k, r in results.items() if r.synced)
    skipped = sorted(k for k, r in results.items() if not r.synced)
    logger.info("SharePoint sync: %d source(s) with a new file %s; %d skipped %s",
                len(synced), synced, len(skipped), skipped)
    return results


def active_sources(config: Config, sync_results: Optional[Dict[str, SourceSyncResult]]):
    """The ReviewSource loaders to run this pass. Without a sync (None) that's
    all of them; with one, only the sources that actually received a new file
    -- a skipped source contributes nothing to this run, not even its stale
    local Reviews/ copy."""
    sources = build_default_sources(config.sources)
    if sync_results is None:
        return sources
    return [s for s in sources if s.name in sync_results and sync_results[s.name].synced]


def move_synced_files_to_processed(config: Config,
                                   sync_results: Dict[str, SourceSyncResult]) -> None:
    """Move every synced source's file from "In Progress" to "Processed".

    Only ever called after load_database() has returned without raising --
    the same gate delete_analysis_outputs() sits behind -- so a file leaves
    "In Progress" only once its reviews are confirmed durable in the
    database. A failed load leaves everything where it was for the next run.
    """
    to_move = [r for r in sync_results.values() if r.synced]
    if not to_move:
        logger.info("No SharePoint files to move to Processed (nothing was synced this run)")
        return
    syncer = _build_syncer(config)
    processed_dirs = sharepoint_processed_dirs(config)
    for result in to_move:
        dest_subpath = processed_dirs[result.key]
        dest_folder = syncer._resolve_subfolder(dest_subpath)
        syncer.move_item(result.item, dest_folder)
        logger.info("%s: moved %s from %s to %s (now durable in the database)",
                    result.key, result.item["name"], result.subpath, dest_subpath)


def apply_rerun_flags(done: Dict[str, Dict[str, Any]], rerun_product: bool,
                      rerun_service: bool) -> Dict[str, Dict[str, Any]]:
    """Reopen review types for reprocessing by removing them from `done` --
    process_all_reviews() then treats them as pending, exactly as if they'd
    never been analysed. The other type stays in `done` untouched.

    New results get appended to the same checkpoint file (see CheckpointWriter's
    resume mode); load_checkpoint() replays the file top-to-bottom and keeps
    the *last* record per uid, so the fresh entry correctly wins on any future
    run. The file just carries superseded lines for these uids from here on --
    harmless at this scale, not worth the complexity of rewriting it clean.
    """
    if rerun_product and done:
        before = len(done)
        done = {uid: rec for uid, rec in done.items() if rec.get("review_type") != "product"}
        logger.info("--rerun-product-reviews: reopened %d product review(s) for "
                    "reprocessing; %d review(s) remain marked done",
                    before - len(done), len(done))
    if rerun_service and done:
        before = len(done)
        done = {uid: rec for uid, rec in done.items() if rec.get("review_type") != "service"}
        logger.info("--rerun-service-reviews: reopened %d service review(s) for "
                    "reprocessing; %d review(s) remain marked done",
                    before - len(done), len(done))
    return done


def resume_state(config: Config, args: argparse.Namespace,
                 all_reviews: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Checkpoint replay, restricted to the reviews actually loaded this run,
    with the rerun flags applied."""
    ckpt_path = config.output_dir / config.checkpoint_file
    done = {} if args.fresh_start else load_checkpoint(ckpt_path)
    if done:
        wanted = {r["uid"] for r in all_reviews}
        done = {k: v for k, v in done.items() if k in wanted}
    return apply_rerun_flags(done, args.rerun_product_reviews, args.rerun_service_reviews)


def drop_already_analysed(config: Config, args: argparse.Namespace,
                          reviews: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """Leave out the reviews the database already holds a sentiment for:
    history is frozen once scored, so they are neither re-analysed (paid LLM
    calls) nor re-loaded over their stored result. Returns (the reviews to
    analyse, how many were left out).

    The check is off with --fresh-start / --reanalyse-existing, and when no
    database is configured; --rerun-product-reviews / --rerun-service-reviews
    turn it off for that review type only. A stored row whose analysis
    failed has no sentiment and is analysed again. A review with a blank
    review_id is never matched: its uid ends in "row<N>", its position in
    this run's combined load, which says nothing about what's stored.
    Raises ExistingReviewsReadError if the database can't be read -- never
    treats everything as new."""
    total = len(reviews)
    if args.fresh_start or args.reanalyse_existing:
        flag = "--fresh-start" if args.fresh_start else "--reanalyse-existing"
        logger.info("%s: not skipping reviews already analysed in the database; all %d "
                    "loaded review(s) go to analysis", flag, total)
        return reviews, 0
    if not config.db.configured:
        logger.info("DB_SERVER/DB_USER/DB_PASSWORD not set; can't skip reviews already "
                    "analysed in the database, so all %d loaded review(s) go to analysis",
                    total)
        return reviews, 0

    reopened = set()
    if args.rerun_product_reviews:
        reopened.add("product")
    if args.rerun_service_reviews:
        reopened.add("service")
    if reopened:
        logger.info("Re-analysing %s reviews even if they're already in the database",
                    " and ".join(sorted(reopened)))

    blank_ids = sum(1 for r in reviews if not r.get("review_id"))
    if blank_ids:
        logger.warning("%d loaded review(s) have no review_id, so their uid is only their "
                       "row position in this run and can't be matched against the "
                       "database: they are analysed (and loaded) again on every run",
                       blank_ids)

    checkable = [r for r in reviews
                 if r.get("review_id") and r.get("review_type") not in reopened]
    existing = ReviewRepository(config.db).fetch_analysed_uids(r["uid"] for r in checkable)
    skip = {id(r) for r in checkable if r["uid"] in existing}
    pending = [r for r in reviews if id(r) not in skip]
    logger.info("Reviews loaded: %d; already analysed in the database (skipped): %d; "
                "to analyse: %d", total, len(skip), len(pending))
    return pending, len(skip)


def report_existing_reviews_unreadable(exc: ExistingReviewsReadError) -> int:
    logger.error("Stopping before any analysis: %s", exc)
    logger.error("Without that list every review would look new and be re-analysed, "
                 "overwriting stored results. Fix the database connection and rerun, or "
                 "pass --reanalyse-existing to analyse every loaded review regardless.")
    return 1


def finish_without_new_reviews(config: Config, args: argparse.Namespace,
                               sync_results: Optional[Dict[str, SourceSyncResult]]) -> int:
    """Every loaded review is already analysed in the database: nothing to
    analyse, write, load, translate or generate action items for. Synced
    files still move to Processed -- the lookup that emptied the list is the
    confirmation that their reviews are in the database -- unless
    --skip-db-load says this run must not commit anything."""
    logger.info("Every loaded review is already analysed in the database; nothing new to "
                "analyse, load, translate or generate action items for")
    if sync_results is None:
        return 0
    if args.skip_db_load:
        logger.info("--skip-db-load set; not moving any SharePoint files to Processed. They "
                    "stay in In Progress for the next run.")
        return 0
    try:
        move_synced_files_to_processed(config, sync_results)
    except SharePointSignInRequiredError as exc:
        return report_sign_in_required(exc)
    return 0


def write_outputs(config: Config, results: List[Dict[str, Any]],
                  elapsed: float) -> tuple:
    """Aggregate + write the two JSON outputs; returns (output, summary_path,
    detail_path)."""
    by_source = AnalysisAggregator.aggregate_by_source(results)
    comparison = AnalysisAggregator.cross_source_comparison(by_source)
    output = OutputGenerator.generate(results, by_source, comparison, config)
    output["metadata"]["elapsed_seconds"] = round(elapsed, 1)

    out_path = config.output_dir / config.output_file
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")

    detail_path = config.output_dir / config.detail_file
    detail_path.write_text(
        json.dumps(sorted(results, key=lambda r: r.get("uid", "")), indent=2,
                   ensure_ascii=False, default=str),
        encoding="utf-8")
    return output, out_path, detail_path


def load_database(config: Config, summary_path: Path, detail_path: Path) -> None:
    """Load the two JSON outputs into SQL Server via the repositories."""
    reviews = json.loads(detail_path.read_text(encoding="utf-8"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    conn = connect(config.db)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    n = ReviewRepository(config.db).upsert(reviews)
    logger.info("Upserted %d reviews into dbo.%s (deduped on uid)", n, REVIEWS_TABLE)

    RunRepository(config.db).upsert(summary)
    logger.info("Upserted run summary (timestamp=%s) into dbo.%s",
                summary.get("metadata", {}).get("timestamp"), RUNS_TABLE)


def delete_analysis_outputs(config: Config) -> None:
    """Remove the two JSON output files once they're durable in the database.

    Deliberately leaves the checkpoint (.jsonl) alone -- that's resume state,
    not an analysis output. Deleting it would force the next run to
    reprocess every review through the paid LLM API from scratch.
    """
    for name in (config.output_file, config.detail_file):
        path = config.output_dir / name
        if path.exists():
            path.unlink()
            logger.info("Deleted %s (now durable in the database)", path)


def run_translation(config: Config, analyzer: SentimentAnalyzer) -> None:
    # Independent of the sentiment checkpoint in either direction: its own
    # columns (review_text_en, detected_language), its own "already done"
    # check (IS NULL). See daylight_sentiment/infra/translation.py.
    from daylight_sentiment.infra.translation import SettingsWriter, run_translation_pass
    logger.info("Running translation pass (fr/de/it/es/nl/sv/pl/cs -> English)...")
    stats = run_translation_pass(
        analyzer, SettingsWriter(config.db), max_chars=config.max_review_chars)
    logger.info("Translation pass: %d matched, %d translated, %d failed",
                stats["matched"], stats["translated"], stats["failed"])


def count_newly_analysed(results: List[Dict[str, Any]],
                         done: Dict[str, Dict[str, Any]]) -> int:
    """Reviews analysed successfully by THIS run, i.e. not replayed from the
    checkpoint (process_all_reviews() returns both)."""
    return sum(1 for r in results
               if r.get("analysis") is not None and r.get("uid") not in done)


def run_action_items(config: Config, analyzer: SentimentAnalyzer,
                     run_timestamp: Optional[str] = None, dry_run: bool = False) -> int:
    """Generate the top 3 business to-do items for each review window from
    the reviews in the database and save them (see
    application/action_items.py). Logs one ERROR line per window that
    failed and returns how many failed; a skipped window (too few reviews)
    is not a failure. Raises if the reviews can't be read at all, and on a
    FatalProviderError."""
    logger.info("Generating the top 3 action items for the review windows %s from the "
                "stored review results...",
                ", ".join(str(m) for m in config.action_items.windows_months))
    outcome = generate_action_items(
        analyzer, ActionItemRepository(config.db), config.action_items,
        config.llm_provider.value, config.model_name,
        run_timestamp=run_timestamp, dry_run=dry_run)
    for result in outcome.windows:
        if result.failed:
            report_action_items_failure(result.error, result.window.label)
        elif result.items and dry_run:
            print("\n" + format_action_items(result.items, result.window))
        elif result.items:
            logger.info("Action items saved for %s: %s", result.window.label,
                        "; ".join(f"{i['rank']}. {i['title']}" for i in result.items))
    return len(outcome.failed)


def report_action_items_failure(exc: Exception, window_label: Optional[str] = None) -> None:
    message = str(exc)
    which = f" for {window_label}" if window_label else ""
    logger.error("Action items were not generated%s: %s%s", which, type(exc).__name__,
                 f": {message}" if message else "")


def action_items_only(config: Config, dry_run: bool) -> int:
    """--action-items-only: generate every review window from what's already
    in the database. Exit 1 if any window failed (a skipped window is not a
    failure)."""
    if not config.db.configured:
        logger.error("--action-items-only needs the database: set DB_SERVER/DB_NAME/"
                     "DB_USER/DB_PASSWORD in .env")
        return 1
    try:
        failed = run_action_items(config, build_analyzer(config), dry_run=dry_run)
    except FatalProviderError as exc:
        return report_fatal_provider_error(config, exc)
    except Exception as exc:  # noqa: BLE001
        report_action_items_failure(exc)
        return 1
    return 1 if failed else 0


def report_fatal_provider_error(config: Config, exc: FatalProviderError) -> int:
    logger.error("Stopping: the %s API rejected the request in a way every other call "
                 "would repeat (invalid API key, no credit, missing permission or unknown "
                 "model %r): %s", config.llm_provider.value, config.model_name, exc)
    logger.error("Fix the key, billing or model setting and rerun; reviews that were not "
                 "analysed stay pending and the run resumes from the checkpoint.")
    return 1


def report_sign_in_required(exc: SharePointSignInRequiredError) -> int:
    logger.error("%s", exc)
    return 2


def main(argv: Optional[List[str]] = None) -> int:
    # .env is loaded here -- explicitly, at the entry point -- never at import
    # time (ISSUE-11).
    from dotenv import load_dotenv
    load_dotenv()

    args = parse_args(argv)
    config = build_config(args)
    configure_logging()
    logger.info("Provider=%s model=%s workers=%d",
                config.llm_provider.value, config.model_name, config.max_workers)

    if args.action_items_only:
        logger.info("--action-items-only: generating action items from the reviews "
                    "already in the database")
        return action_items_only(config, dry_run=args.action_items_dry_run)

    if args.load_db_only:
        out_path = config.output_dir / config.output_file
        detail_path = config.output_dir / config.detail_file
        logger.info("--load-db-only: loading existing %s / %s into the database",
                    out_path.name, detail_path.name)
        load_database(config, out_path, detail_path)
        if not args.keep_output_files:
            delete_analysis_outputs(config)
        return 0

    if args.sync_sharepoint_only:
        try:
            sync_sharepoint(config, skip=False)
        except SharePointSignInRequiredError as exc:
            return report_sign_in_required(exc)
        return 0

    try:
        sync_results = sync_sharepoint(config, skip=args.skip_sharepoint_sync)
    except SharePointSignInRequiredError as exc:
        return report_sign_in_required(exc)

    # A sync that ran and found nothing new means there's nothing to do: no
    # analysis, no empty outputs, no zero-review run row, no translation pass.
    if sync_results is not None and not any(r.synced for r in sync_results.values()):
        logger.info("No new review files waiting in SharePoint; nothing to do")
        return 0

    sources = active_sources(config, sync_results)
    if sync_results is not None:
        logger.info("Active sources this run: %s", [s.name for s in sources])
    all_reviews = load_reviews(sources)
    if args.limit:
        all_reviews = all_reviews[: args.limit]
        logger.info("Limited to first %d reviews", len(all_reviews))

    try:
        all_reviews, already_analysed = drop_already_analysed(config, args, all_reviews)
    except ExistingReviewsReadError as exc:
        return report_existing_reviews_unreadable(exc)
    if already_analysed and not all_reviews:
        return finish_without_new_reviews(config, args, sync_results)

    analyzer = build_analyzer(config)
    done = resume_state(config, args, all_reviews)

    started = time.time()
    pipeline = MultiSourcePipeline(config, analyzer)
    # resume is explicit: a run that loads only some sources (or none) must
    # append to the checkpoint, never truncate the other sources' entries.
    try:
        results = pipeline.process_all_reviews(
            all_reviews, config.output_dir / config.checkpoint_file, done,
            resume=not args.fresh_start)
    except FatalProviderError as exc:
        return report_fatal_provider_error(config, exc)
    elapsed = time.time() - started

    output, out_path, detail_path = write_outputs(config, results, elapsed)

    s = output["summary"]
    logger.info("Analysed %d, failed %d, in %.1fs",
                s["total_reviews_analyzed"], s["failed_reviews"], elapsed)
    print(f"\nSummary -> {out_path}")
    print(f"Per-review -> {detail_path}")
    print(json.dumps(s["overall_sentiment"], indent=2))

    newly_analysed = count_newly_analysed(results, done)
    loaded = config.db.configured and not args.skip_db_load
    if loaded:
        logger.info("Loading results into database %s/%s", config.db_server, config.db_name)
        load_database(config, out_path, detail_path)
        # Only delete / move once load_database() has returned without raising
        # -- i.e. the data is confirmed durable in the database first.
        if not args.keep_output_files:
            delete_analysis_outputs(config)
        if sync_results is not None:
            try:
                move_synced_files_to_processed(config, sync_results)
            except SharePointSignInRequiredError as exc:
                return report_sign_in_required(exc)
    else:
        if args.skip_db_load:
            logger.info("--skip-db-load set; not loading into the database")
        else:
            logger.info("DB_SERVER/DB_USER/DB_PASSWORD not set; not loading into the database")
        if sync_results is not None and any(r.synced for r in sync_results.values()):
            logger.info("Not moving any SharePoint files to Processed: the database load "
                        "didn't run, so their reviews aren't confirmed durable yet. They "
                        "stay in In Progress for the next run that can load them.")

    if config.db.configured and not args.skip_translation:
        try:
            run_translation(config, analyzer)
        except FatalProviderError as exc:
            return report_fatal_provider_error(config, exc)
    elif args.skip_translation:
        logger.info("--skip-translation set; not translating")
    else:
        logger.info("DB_SERVER/DB_USER/DB_PASSWORD not set; not translating")

    # After the load, the move and translation, so the items see this run's
    # reviews with their English text. A failure here (of any window, or of
    # all of them) doesn't fail the run: the reviews are already saved.
    if args.skip_action_items:
        logger.info("--skip-action-items set; not generating action items")
    elif not loaded:
        logger.info("Not generating action items: the database load didn't run")
    elif newly_analysed == 0:
        logger.info("Not generating action items: no new reviews were analysed this run")
    else:
        try:
            run_action_items(config, analyzer,
                             run_timestamp=output.get("metadata", {}).get("timestamp"),
                             dry_run=args.action_items_dry_run)
        except FatalProviderError as exc:
            return report_fatal_provider_error(config, exc)
        except Exception as exc:  # noqa: BLE001
            report_action_items_failure(exc)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
