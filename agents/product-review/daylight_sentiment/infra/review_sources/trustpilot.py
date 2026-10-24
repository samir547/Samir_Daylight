"""Trustpilot review sources (product + service exports, .xlsx or .csv)."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from daylight_sentiment.config import SourceFiles
from daylight_sentiment.infra.review_sources.base import read_table, resolve_source_file
from daylight_sentiment.infra.review_sources.coercion import _clean_date, _clean_str, _safe_int

logger = logging.getLogger("multi_source_sentiment")

# Every column each loader reads; a file missing any of them is rejected.
PRODUCT_COLUMNS = ("review_id", "stars", "product_name", "product_sku", "content",
                   "consumer_name", "created_at")
SERVICE_COLUMNS = ("Review Id", "Review Stars", "Review Title", "Review Content",
                   "Review Username", "Review Created (UTC)")
# Read as text in both formats (ids and SKUs must not become numbers).
PRODUCT_TEXT_COLUMNS = ("review_id", "product_name", "product_sku", "content",
                        "consumer_name")
SERVICE_TEXT_COLUMNS = ("Review Id", "Review Title", "Review Content", "Review Username")


def load_trustpilot_product(path: Path) -> List[Dict[str, Any]]:
    df = read_table(path, PRODUCT_COLUMNS, PRODUCT_TEXT_COLUMNS)
    reviews = []
    for _, row in df.iterrows():
        reviews.append({
            "review_id": _clean_str(row.get("review_id")),
            "review_type": "product",
            "review_source": "trustpilot",
            "rating": _safe_int(row.get("stars")),
            "product_name": _clean_str(row.get("product_name")),
            "product_sku": _clean_str(row.get("product_sku")),
            "text": _clean_str(row.get("content")),
            "reviewer_name": _clean_str(row.get("consumer_name")),
            "review_date": _clean_date(row.get("created_at")),
        })
    logger.info("Loaded %d Trustpilot product reviews", len(reviews))
    return reviews


def load_trustpilot_service(path: Path) -> List[Dict[str, Any]]:
    df = read_table(path, SERVICE_COLUMNS, SERVICE_TEXT_COLUMNS)
    reviews = []
    for _, row in df.iterrows():
        title = _clean_str(row.get("Review Title"))
        body = _clean_str(row.get("Review Content"))
        # The title carries real signal on service reviews ("Great After-Sales
        # Service!"), so feed both to the model as one block.
        text = f"{title}\n\n{body}".strip() if title else body
        reviews.append({
            "review_id": _clean_str(row.get("Review Id")),
            "review_type": "service",
            "review_source": "trustpilot",
            "rating": _safe_int(row.get("Review Stars")),
            "title": title,
            "text": text,
            "reviewer_name": _clean_str(row.get("Review Username")),
            "review_date": _clean_date(row.get("Review Created (UTC)")),
        })
    logger.info("Loaded %d Trustpilot service reviews", len(reviews))
    return reviews


class TrustpilotProductSource:
    name = "trustpilot_product"

    def __init__(self, sources: SourceFiles):
        self.sources = sources

    def fetch(self) -> List[Dict[str, Any]]:
        path = resolve_source_file(self.sources.trustpilot_product_pattern,
                                   self.sources.reviews_dir)
        return load_trustpilot_product(path)


class TrustpilotServiceSource:
    name = "trustpilot_service"

    def __init__(self, sources: SourceFiles):
        self.sources = sources

    def fetch(self) -> List[Dict[str, Any]]:
        path = resolve_source_file(self.sources.trustpilot_service_pattern,
                                   self.sources.reviews_dir)
        return load_trustpilot_service(path)
