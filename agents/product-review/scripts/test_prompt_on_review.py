"""Compare old vs. new sentiment for specific review(s), without touching the checkpoint
or main pipeline state -- for validating a prompt change (e.g. SENTIMENT_GUIDANCE, 2026-09-11)
against known rows before committing to a full --rerun-product-reviews pass.

Pulls the row(s) straight from dbo.sentiment_analysis_reviews (source of truth already
has review_text/rating/product_name -- no need to re-load or re-sync the source CSVs/xlsx),
re-analyses with the CURRENT (patched) ClaudeSentimentAnalyzer, and prints old vs new
side by side. Does NOT write to the database unless --write is passed.

Usage:
    python -m scripts.test_prompt_on_review --sku DN1560
    python -m scripts.test_prompt_on_review --sku DN1560 --write        # update DB if you like the result
    python -m scripts.test_prompt_on_review --review-id 68fa8013c87bf700b0d4dffb
    python -m scripts.test_prompt_on_review --uid "trustpilot:product:68fa8013c87bf700b0d4dffb"
"""
from __future__ import annotations

import argparse
import logging
from typing import Any, Dict, List, Optional

from daylight_sentiment.config import Config, DatabaseSettings, LLMProvider
from daylight_sentiment.infra.analyzers import ClaudeSentimentAnalyzer
from daylight_sentiment.infra.db import REVIEWS_TABLE, ReviewRepository, connect

# ISSUE-12: shared logging setup -- same format as the pipeline, and this
# script's output now reaches multi_source_sentiment.log too.
from daylight_sentiment.logging_setup import configure_logging

configure_logging(level=logging.WARNING)
logger = logging.getLogger("test_prompt_on_review")


def fetch_rows(settings: DatabaseSettings, sku: Optional[str], review_id: Optional[str],
                uid: Optional[str]) -> List[Dict[str, Any]]:
    # Fetches every column the MERGE upsert will write back (see ReviewRepository.COLUMNS)
    # -- the MERGE overwrites ALL non-uid columns unconditionally, so
    # any column not selected here and later passed to --write would come back as
    # None and silently NULL out real existing data (reviewer_name, review_date,
    # verified_purchase, review_country) for that row.
    select_cols = ("uid", "review_id", "review_type", "review_source", "rating",
                   "product_name", "product_sku", "product_asin", "model_number",
                   "review_country", "title", "review_text", "reviewer_name",
                   "review_date", "verified_purchase", "sentiment", "confidence",
                   "primary_sector", "primary_aspect")
    select_clause = ", ".join(select_cols)
    conn = connect(settings)
    try:
        cur = conn.cursor()
        if uid:
            cur.execute(f"SELECT {select_clause} FROM dbo.{REVIEWS_TABLE} WHERE uid = ?", uid)
        elif review_id:
            cur.execute(f"SELECT {select_clause} FROM dbo.{REVIEWS_TABLE} WHERE review_id = ?", review_id)
        else:
            cur.execute(
                f"SELECT {select_clause} FROM dbo.{REVIEWS_TABLE} "
                "WHERE product_sku = ? OR model_number = ? OR product_asin = ?", sku, sku, sku)
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()
    return rows


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--sku", help="Matches product_sku, model_number, or product_asin (any review type)")
    g.add_argument("--review-id", help="Exact review_id (source-assigned, not unique across sources)")
    g.add_argument("--uid", help="Exact uid, e.g. 'trustpilot:product:<review_id>'")
    p.add_argument("--write", action="store_true",
                    help="Upsert the new result into the database. Without this flag, nothing is written.")
    args = p.parse_args()

    config = Config.from_env(llm_provider=LLMProvider.CLAUDE)
    rows = fetch_rows(config.db, args.sku, args.review_id, args.uid)

    if not rows:
        print("No matching rows found.")
        return 0

    print(f"Found {len(rows)} matching row(s).\n")
    analyzer = ClaudeSentimentAnalyzer(config)
    to_write: List[Dict[str, Any]] = []

    for row in rows:
        text = (row.get("review_text") or "")[: config.max_review_chars]
        print("=" * 78)
        print(f"uid:          {row['uid']}")
        print(f"product:      {row.get('product_name')}")
        print(f"sku/asin:     sku={row.get('product_sku')} asin={row.get('product_asin')} model={row.get('model_number')}")
        print(f"rating:       {row.get('rating')}/5")
        print(f"text:         {text[:200]}{'...' if len(text) > 200 else ''}")
        print(f"OLD sentiment: {row.get('sentiment')} (confidence={row.get('confidence')})")

        if not text.strip():
            print("  (empty review_text -- skipping re-analysis)")
            continue

        if row["review_type"] == "product":
            analysis = analyzer.analyze_product_review(
                text, product_name=row.get("product_name") or None, rating=row.get("rating"))
        else:
            analysis = analyzer.analyze_service_review(text, rating=row.get("rating"))

        changed = analysis.get("sentiment") != row.get("sentiment")
        marker = "  <-- CHANGED" if changed else ""
        print(f"NEW sentiment: {analysis.get('sentiment')} (confidence={analysis.get('confidence')}){marker}")
        print(f"key_themes:    {analysis.get('key_themes')}")

        to_write.append({**row, "text": text, "analysis": analysis, "error": None})

    print("=" * 78)

    if args.write:
        n = ReviewRepository(config.db).upsert(to_write)
        print(f"\n--write set: upserted {n} row(s) with the new sentiment.")
    else:
        print("\n(Dry run -- nothing written. Pass --write to save the new result.)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
