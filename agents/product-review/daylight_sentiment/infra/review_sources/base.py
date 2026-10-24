"""ReviewSource protocol + shared source plumbing (ISSUE-01/02)."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Protocol, Sequence, runtime_checkable

import pandas as pd

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
    already worked via select_items_by_patterns()). An .xlsx and a .csv
    match compete on equal terms: the newest one wins, whatever its format."""
    candidates = [p for p in (directory.iterdir() if directory.is_dir() else [])
                  if p.is_file() and pattern.matches(p.name)]
    if candidates:
        return max(candidates, key=lambda p: p.stat().st_mtime)
    raise FileNotFoundError(
        f"No file in {directory} matches {pattern.describe()}")


class MissingColumnsError(ValueError):
    """A review export lacks columns its loader reads. Raised instead of
    loading the rows with those fields silently empty."""


def read_table(path: Path, required: Sequence[str] = ()) -> pd.DataFrame:
    """Read a review export by its real extension: .xlsx through read_excel,
    .csv through read_csv. Every loader goes through here, so a source can be
    supplied in either format and the column handling after it is shared.

    ``required`` lists the columns the loader reads; if any is absent the
    whole file is rejected with MissingColumnsError naming the file and the
    missing columns."""
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        df = pd.read_excel(path)
    elif suffix == ".csv":
        df = pd.read_csv(path)
    else:
        raise ValueError(f"{path}: unsupported review file type {path.suffix!r} "
                         f"(expected .xlsx or .csv)")
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise MissingColumnsError(
            f"{path} is missing required column(s): {', '.join(missing)}")
    return df


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
