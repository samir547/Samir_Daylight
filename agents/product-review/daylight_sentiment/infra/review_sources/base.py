"""ReviewSource protocol + shared source plumbing (ISSUE-01/02)."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Protocol, runtime_checkable

from daylight_sentiment.config import FilePattern

logger = logging.getLogger("multi_source_sentiment")


@runtime_checkable
class ReviewSource(Protocol):
    """A source of reviews. fetch() returns dicts in the pipeline's
    normalised shape (review_id/review_type/review_source/rating/text/...)."""
    name: str

    def fetch(self) -> List[Dict[str, Any]]: ...


def resolve_source_file(pattern: FilePattern, directory: Path) -> Path:
    """Find a source file: the newest file in the directory matching the
    pattern. There is no preferred/expected filename to check first -- a
    rename, "(2)" suffix, fresh row count, or date stamp in the name never
    matters, since nothing is ever compared against a specific name (that
    used to be true only once an expected exact filename went missing;
    now it's true unconditionally, matching how the SharePoint side
    already worked via select_items_by_patterns())."""
    candidates = [p for p in directory.glob(f"*{pattern.extension}")
                  if pattern.matches(p.name)]
    if candidates:
        return max(candidates, key=lambda p: p.stat().st_mtime)
    raise FileNotFoundError(
        f"No file in {directory} matches {pattern.describe()}")


def load_reviews(sources: List[ReviewSource]) -> List[Dict[str, Any]]:
    """Fetch every source and assign the pipeline's unique uid per review.

    review_id is unique per source but not guaranteed across sources, so the
    uid is "{review_source}:{review_type}:{review_id}".
    """
    all_reviews: List[Dict[str, Any]] = []
    for source in sources:
        all_reviews.extend(source.fetch())

    for idx, r in enumerate(all_reviews):
        rid = r["review_id"] or f"row{idx}"
        r["uid"] = f"{r['review_source']}:{r['review_type']}:{rid}"

    logger.info("Total reviews loaded: %d", len(all_reviews))
    return all_reviews
