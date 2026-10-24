"""Amazon product reviews from the Bright Data export shape (.xlsx or .csv).

Bright Data is no longer the extraction vendor (ISSUE-03); this loader
stays for the existing export on disk until a replacement source exists --
at which point the replacement becomes another ReviewSource here, not a
parallel script.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from daylight_sentiment.config import SourceFiles
from daylight_sentiment.infra.review_sources.base import read_table, resolve_source_file
from daylight_sentiment.infra.review_sources.coercion import (
    _clean_date,
    _clean_str,
    _safe_bool,
    _safe_int,
)

logger = logging.getLogger("multi_source_sentiment")

# Every column the loader reads; a file missing any of them is rejected.
# "error" is optional: only scrapes that hit failures carry it.
COLUMNS = ("review_id", "rating", "product_name", "asin", "model_number",
           "review_country", "review_header", "review_text", "author_name",
           "review_posted_date", "is_verified")
# Read as text in both formats (ids and codes must not become numbers).
TEXT_COLUMNS = ("review_id", "product_name", "asin", "model_number", "review_country",
                "review_header", "review_text", "author_name")


def load_amazon_product(path: Path) -> List[Dict[str, Any]]:
    df = read_table(path, COLUMNS, TEXT_COLUMNS)

    # Scrape-failure rows carry an error code and no review payload.
    if "error" in df.columns:
        errored = int(df["error"].notna().sum())
        if errored:
            df = df[df["error"].isna()]
            logger.warning("Skipped %d Amazon rows with scrape errors", errored)

    before = len(df)
    df = df.drop_duplicates(subset=["review_id"], keep="first")
    if before != len(df):
        logger.warning("Dropped %d duplicate Amazon review_ids", before - len(df))

    reviews = []
    for _, row in df.iterrows():
        header = _clean_str(row.get("review_header"))
        body = _clean_str(row.get("review_text"))
        text = f"{header}\n\n{body}".strip() if header else body
        reviews.append({
            "review_id": _clean_str(row.get("review_id")),
            "review_type": "product",
            "review_source": "amazon",
            "rating": _safe_int(row.get("rating")),
            "product_name": _clean_str(row.get("product_name")),
            "product_asin": _clean_str(row.get("asin")),
            # Daylight's own item code -- already present in the scrape but
            # previously dropped. Uppercased so it's directly comparable to
            # master_product_table NAME / the future ASIN->SKU bridge table
            # without every downstream consumer having to remember to.
            "model_number": _clean_str(row.get("model_number")).upper(),
            # Reviewer-stated country -- not the marketplace queried (Amazon
            # surfaces cross-border reviews), kept as raw signal for later.
            "review_country": _clean_str(row.get("review_country")),
            "title": header,
            "text": text,
            "reviewer_name": _clean_str(row.get("author_name")),
            # Day only: the view parses Amazon dates with TRY_PARSE, where a
            # trailing Z could shift the day by the server's time zone.
            "review_date": _clean_date(row.get("review_posted_date"), with_time=False),
            "verified_purchase": _safe_bool(row.get("is_verified")),
        })
    logger.info("Loaded %d Amazon product reviews", len(reviews))
    return reviews


class AmazonBrightDataSource:
    name = "amazon_brightdata"

    def __init__(self, sources: SourceFiles):
        self.sources = sources

    def fetch(self) -> List[Dict[str, Any]]:
        path = resolve_source_file(self.sources.amazon_product_pattern,
                                   self.sources.reviews_dir)
        return load_amazon_product(path)
