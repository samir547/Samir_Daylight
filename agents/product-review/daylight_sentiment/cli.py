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
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from daylight_sentiment.application.aggregator import AnalysisAggregator
from daylight_sentiment.application.checkpoint import load_checkpoint
from daylight_sentiment.application.output_generator import OutputGenerator
from daylight_sentiment.application.pipeline import MultiSourcePipeline
from daylight_sentiment.config import Config, LLMProvider
from daylight_sentiment.logging_setup import configure_logging as _configure_logging
from daylight_sentiment.infra.analyzers import SentimentAnalyzer, build_analyzer
from daylight_sentiment.infra.db import (
    REVIEWS_TABLE,
    RUNS_TABLE,
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
    SharePointSync,
    SourceSpec,
    SourceSyncResult,
)

logger = logging.getLogger("multi_source_sentiment")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Multi-source sentiment pipeline")
    p.add_argument("--provider", choices=[e.value for e in LLMProvider],
                   default=os.getenv("LLM_PROVIDER", "groq"))
    p.add_argument("--model", help="Override the provider's default model")
    p.add_argument("--limit", type=int, help="Only process the first N reviews (smoke test)")
    p.add_argument("--workers", type=int, default=None,
                   help="Concurrent worker threads (default 8)")
    p.add_argument("--batch-size", type=int, default=None,
                   help="Reviews per LLM call; 1 disables batching (default 10)")
    p.add_argument("--fresh-start", action="store_true",
                   help="Ignore the checkpoint and reprocess every review from scratch. "
                        "By default the pipeline always resumes from the checkpoint -- "
                        "this is the only way to force a full reprocess.")
    p.add_argument("--rerun-product-reviews", action="store_true",
                   help="Reopen only product reviews (Trustpilot + Amazon) for reprocessing, "
                        "leaving service reviews untouched in the checkpoint. Use this after "
                        "changing the sector taxonomy (SECTORS), or after adding a new field "
                        "the loaders capture for product reviews (e.g. model_number) that "
                        "existing checkpoint entries predate. Ignored if --fresh-start is also "
                        "passed, since that already reprocesses everything.")
    p.add_argument("--rerun-service-reviews", action="store_true",
                   help="Reopen only service reviews (Trustpilot) for reprocessing, leaving "
                        "product reviews untouched in the checkpoint. Mirrors "
                        "--rerun-product-reviews for the service side -- use after a prompt "
                        "change (e.g. to SENTIMENT_GUIDANCE) that should apply to service "
                        "reviews too. Combine both flags to reopen everything without a full "
                        "--fresh-start. Ignored if --fresh-start is also passed.")
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
        if config.llm_provider is LLMProvider.GROQ:
            config.groq_model = args.model
        else:
            config.claude_model = args.model
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


def main(argv: Optional[List[str]] = None) -> int:
    # .env is loaded here -- explicitly, at the entry point -- never at import
    # time (ISSUE-11). It must precede parse_args(), whose --provider default
    # reads LLM_PROVIDER from the environment.
    from dotenv import load_dotenv
    load_dotenv()

    args = parse_args(argv)
    config = build_config(args)
    configure_logging()
    logger.info("Provider=%s model=%s workers=%d",
                config.llm_provider.value, config.model_name, config.max_workers)

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
        sync_sharepoint(config, skip=False)
        return 0

    sync_results = sync_sharepoint(config, skip=args.skip_sharepoint_sync)

    sources = active_sources(config, sync_results)
    if sync_results is not None:
        logger.info("Active sources this run: %s", [s.name for s in sources])
    all_reviews = load_reviews(sources)
    if args.limit:
        all_reviews = all_reviews[: args.limit]
        logger.info("Limited to first %d reviews", len(all_reviews))

    analyzer = build_analyzer(config)
    done = resume_state(config, args, all_reviews)

    started = time.time()
    pipeline = MultiSourcePipeline(config, analyzer)
    # resume is explicit: a run that loads only some sources (or none) must
    # append to the checkpoint, never truncate the other sources' entries.
    results = pipeline.process_all_reviews(
        all_reviews, config.output_dir / config.checkpoint_file, done,
        resume=not args.fresh_start)
    elapsed = time.time() - started

    output, out_path, detail_path = write_outputs(config, results, elapsed)

    s = output["summary"]
    logger.info("Analysed %d, failed %d, in %.1fs",
                s["total_reviews_analyzed"], s["failed_reviews"], elapsed)
    print(f"\nSummary -> {out_path}")
    print(f"Per-review -> {detail_path}")
    print(json.dumps(s["overall_sentiment"], indent=2))

    if config.db.configured and not args.skip_db_load:
        logger.info("Loading results into database %s/%s", config.db_server, config.db_name)
        load_database(config, out_path, detail_path)
        # Only delete / move once load_database() has returned without raising
        # -- i.e. the data is confirmed durable in the database first.
        if not args.keep_output_files:
            delete_analysis_outputs(config)
        if sync_results is not None:
            move_synced_files_to_processed(config, sync_results)
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
        run_translation(config, analyzer)
    elif args.skip_translation:
        logger.info("--skip-translation set; not translating")
    else:
        logger.info("DB_SERVER/DB_USER/DB_PASSWORD not set; not translating")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
