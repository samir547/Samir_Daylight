"""One-off loader for an ad hoc Amazon review pull (not the regular SharePoint CSV feed).

Context: Varun pulled ~95 raw Amazon reviews via a scraper (Apify-shaped JSON --
fields like product_rating_object / is_amazon_vine / input.url, not the Bright Data
CSV shape DataLoader.load_amazon_product() expects). This script maps that JSON
directly onto the same review-dict schema the main pipeline uses, so it can reuse
the SAME (already-patched) ClaudeSentimentAnalyzer -- including the SENTIMENT_GUIDANCE
overall-experience weighting and star-rating context added 2026-09-11 -- and the SAME
idempotent MERGE upsert into dbo.sentiment_analysis_reviews. It does not touch the
main pipeline's checkpoint file or Reviews/amazon_product_reviews_all.csv, so it can't
be clobbered by a future --skip-sharepoint-sync-less run and can't disturb the main
pipeline's resume state.

Known gaps in this source (flagged, not silently swallowed):
  - No model_number field anywhere in the 95 raw records. product_region in
    vw_review_analysis is SKU-prefix-derived from model_number/product_sku, so
    these 8 ASINs will land in "No Region Data" until someone supplies an
    ASIN -> model_number mapping. See the printed ASIN list at the end of a run --
    once you have the mapping, a follow-up UPDATE by asin is a five-minute fix.
  - review_posted_date spans August 2025 - September 2026 (13 months), not a
    rolling 30-day window -- this pull looks like full review history per
    product, not "last 30 days". Doesn't block processing, just flagging the
    mismatch with how the source was described.
  - 18 of 95 raw records are exact re-pulls of the same review_id (same text,
    just re-scraped from two Amazon locale URLs with differently formatted
    dates) -- deduped below, keep-first, same convention as
    DataLoader.load_amazon_product()'s drop_duplicates(subset=["review_id"]).

Usage:
    python -m scripts.process_varun_amazon_batch                    # full run
    python -m scripts.process_varun_amazon_batch --dry-run           # map + DB-dedup only, no LLM/DB writes
    python -m scripts.process_varun_amazon_batch --input path.json   # override the input file
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

from daylight_sentiment.config import Config, DatabaseSettings, LLMProvider
from daylight_sentiment.infra.analyzers import ClaudeSentimentAnalyzer
from daylight_sentiment.infra.db import REVIEWS_TABLE, ReviewRepository, connect, ensure_schema
from daylight_sentiment.infra.review_sources.coercion import _clean_str, _safe_bool, _safe_int
from daylight_sentiment.infra.translation import SettingsWriter, run_translation_pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("varun_amazon_batch")

# Repo root (this file lives in scripts/), so Reviews/ resolves the same way
# it did when the script sat at the top level.
BASE = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = BASE / "Reviews" / "varun_amazon_pull_20260911.json"


def load_raw(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(
            f"Expected the JSON Varun sent at {path} -- save it there (exact filename) "
            f"and re-run, or pass --input <path> to point at wherever you saved it."
        )
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    logger.info("Loaded %d raw records from %s", len(data), path)
    return data


def map_and_dedupe(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Mirror DataLoader.load_amazon_product()'s mapping, adapted to this JSON's field names."""
    before_error = len(raw)
    raw = [r for r in raw if not r.get("error")]
    if before_error != len(raw):
        logger.warning("Skipped %d rows with a scrape error", before_error - len(raw))

    seen_ids = set()
    reviews: List[Dict[str, Any]] = []
    dropped_dupes = 0
    for row in raw:
        review_id = _clean_str(row.get("review_id"))
        if not review_id or review_id in seen_ids:
            if review_id in seen_ids:
                dropped_dupes += 1
            continue
        seen_ids.add(review_id)

        header = _clean_str(row.get("review_header"))
        body = _clean_str(row.get("review_text"))
        text = f"{header}\n\n{body}".strip() if header else body

        reviews.append({
            "review_id": review_id,
            "review_type": "product",
            "review_source": "amazon",
            "rating": _safe_int(row.get("rating")),
            "product_name": _clean_str(row.get("product_name")),
            "product_asin": _clean_str(row.get("asin")),
            # Not present in this source at all -- see module docstring.
            "model_number": "",
            "review_country": _clean_str(row.get("review_country")),
            "title": header,
            "text": text,
            "reviewer_name": _clean_str(row.get("author_name")),
            "review_date": _clean_str(row.get("review_posted_date")),
            "verified_purchase": _safe_bool(row.get("is_verified")),
        })
    if dropped_dupes:
        logger.info("Dropped %d duplicate review_id(s) within this file (kept first)", dropped_dupes)

    for r in reviews:
        r["uid"] = f"{r['review_source']}:{r['review_type']}:{r['review_id']}"
    logger.info("Mapped %d unique reviews", len(reviews))
    return reviews


def find_already_in_db(settings: DatabaseSettings, uids: List[str]) -> set:
    """Check which of these uids are already in dbo.sentiment_analysis_reviews.

    This is the explicit duplicate check on top of the MERGE upsert's own
    idempotency -- MERGE would silently update-in-place either way, but
    checking first means we don't spend a paid Claude call re-analysing a
    review that's already correctly in the database and unchanged.
    """
    if not uids:
        return set()
    conn = connect(settings)
    try:
        placeholders = ", ".join("?" for _ in uids)
        cur = conn.cursor()
        cur.execute(f"SELECT uid FROM dbo.{REVIEWS_TABLE} WHERE uid IN ({placeholders})", uids)
        existing = {row[0] for row in cur.fetchall()}
    finally:
        conn.close()
    return existing


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, default=DEFAULT_INPUT,
                    help=f"Path to Varun's JSON pull (default: {DEFAULT_INPUT})")
    p.add_argument("--dry-run", action="store_true",
                    help="Map and check DB duplicates only -- no Claude calls, no DB writes, "
                         "no translation")
    p.add_argument("--skip-translation", action="store_true",
                    help="Don't run the translation pass after loading this batch. Runs by "
                         "default -- see daylight_sentiment/infra/translation.py; it scans the WHOLE table for "
                         "review_text_en IS NULL, so it picks up this batch's non-English "
                         "rows (if any) plus anything else still untranslated, not just what "
                         "this script itself inserted.")
    args = p.parse_args()

    raw = load_raw(args.input)
    reviews = map_and_dedupe(raw)

    asins = sorted({r["product_asin"] for r in reviews if r["product_asin"]})
    logger.info("ASINs in this batch (no model_number available for any of these): %s", asins)

    dates = sorted(r["review_date"] for r in reviews if r["review_date"])
    if dates:
        logger.info("review_posted_date range in this batch: %r .. %r", dates[0], dates[-1])

    config = Config.from_env(llm_provider=LLMProvider.CLAUDE)  # match production (see pipeline log, 2026-09-04)

    conn = connect(config.db)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    all_uids = [r["uid"] for r in reviews]
    already_in_db = find_already_in_db(config.db, all_uids)
    pending = [r for r in reviews if r["uid"] not in already_in_db]

    logger.info(
        "Duplicate check: %d already in dbo.%s, %d genuinely new -> will %s",
        len(already_in_db), REVIEWS_TABLE, len(pending),
        "be analysed" if not args.dry_run else "be skipped (--dry-run)",
    )
    if already_in_db:
        logger.info("Skipping (already in DB): %s", sorted(already_in_db))

    if args.dry_run:
        logger.info("--dry-run set: stopping before any Claude calls, DB writes, or translation")
        return 0

    analyzer = ClaudeSentimentAnalyzer(config)

    if pending:
        results = []
        for i, review in enumerate(pending, start=1):
            text = review["text"][: config.max_review_chars]
            if not text.strip():
                logger.warning("Skipping %s (empty text)", review["uid"])
                continue
            try:
                analysis = analyzer.analyze_product_review(
                    text, product_name=review.get("product_name") or None, rating=review.get("rating"))
                results.append({**review, "analysis": analysis, "error": None})
                logger.info("[%d/%d] %s -> sentiment=%s confidence=%.2f",
                            i, len(pending), review["uid"],
                            analysis.get("sentiment"), analysis.get("confidence", 0.0))
            except Exception as exc:  # noqa: BLE001
                logger.error("[%d/%d] %s failed: %s", i, len(pending), review["uid"], exc)
                results.append({**review, "analysis": None, "error": str(exc)})

        analysed = [r for r in results if r.get("analysis") is not None]
        failed = [r for r in results if r.get("error")]
        n = ReviewRepository(config.db).upsert(results)
        logger.info("Upserted %d review row(s) into dbo.%s (analysed=%d, failed=%d)",
                    n, REVIEWS_TABLE, len(analysed), len(failed))
        if failed:
            logger.warning("Failed uids (not written -- will not overwrite any existing good data "
                            "for these, safe to re-run): %s", [r["uid"] for r in failed])
    else:
        logger.info("Nothing new to process -- every review in this batch is already in the database")

    if not args.skip_translation:
        # Runs over the whole table (review_text_en IS NULL), not just this batch -- see
        # daylight_sentiment/infra/translation.py. Reuses the same Claude analyzer already built above, so no
        # second API client gets constructed.
        logger.info("Running translation pass (fr/de/it/es/nl/sv/pl/cs -> English)...")
        translation_stats = run_translation_pass(
            analyzer, SettingsWriter(config.db), max_chars=config.max_review_chars)
        logger.info("Translation pass: %d matched, %d translated, %d failed",
                    translation_stats["matched"], translation_stats["translated"],
                    translation_stats["failed"])
    else:
        logger.info("--skip-translation set; not translating")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
