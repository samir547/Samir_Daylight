"""Business to-do items: reads the analysed reviews they are based on from
dbo.vw_review_analysis (which already parses the raw review dates), and saves
each generation's items to dbo.sentiment_action_items.

Read-only on the review side, insert-only on the items side: older
generations are kept as history and dbo.vw_latest_action_items shows the
newest one.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional, Sequence

from daylight_sentiment.config import DatabaseSettings
from daylight_sentiment.infra.db.connection import ACTION_ITEMS_TABLE, connect, ensure_schema

logger = logging.getLogger("multi_source_sentiment")

REVIEW_VIEW = "vw_review_analysis"


class ActionItemRepository:
    # Columns read from the view. reviewer_name is deliberately absent; the
    # review text columns are read for negative reviews only (the excerpts).
    FETCH_COLUMNS = ("uid", "review_type", "review_source", "sentiment", "confidence",
                     "rating", "sector_group", "primary_aspect", "product_region",
                     "resolved_sku", "product_name", "key_themes", "review_date_parsed")
    NEGATIVE_TEXT_COLUMNS = ("review_text_en", "review_text")

    INSERT_COLUMNS = ("generation_id", "run_timestamp", "[rank]", "title", "description",
                      "rationale", "related_area", "evidence", "window_start", "window_end",
                      "reviews_analysed", "llm_provider", "llm_model")

    def __init__(self, settings: DatabaseSettings):
        self.settings = settings

    @classmethod
    def fetch_sql(cls) -> str:
        text_cols = [f"CASE WHEN sentiment = 'negative' THEN {c} END AS {c}"
                     for c in cls.NEGATIVE_TEXT_COLUMNS]
        cols = ",\n       ".join(list(cls.FETCH_COLUMNS) + text_cols)
        return f"""
SELECT {cols}
FROM dbo.{REVIEW_VIEW}
WHERE sentiment IS NOT NULL
"""

    @classmethod
    def insert_sql(cls) -> str:
        cols = ", ".join(cls.INSERT_COLUMNS)
        marks = ", ".join(["?"] * len(cls.INSERT_COLUMNS))
        return f"INSERT INTO dbo.{ACTION_ITEMS_TABLE} ({cols}) VALUES ({marks})"

    @staticmethod
    def fetch_on(conn) -> List[Dict[str, Any]]:
        cur = conn.cursor()
        cur.execute(ActionItemRepository.fetch_sql())
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def fetch_analysed_rows(self) -> List[Dict[str, Any]]:
        conn = connect(self.settings)
        try:
            return self.fetch_on(conn)
        finally:
            conn.close()

    @classmethod
    def rows(cls, items: Sequence[Dict[str, Any]], generation_id: str, window_start,
             window_end, reviews_analysed: int, llm_provider: Optional[str],
             llm_model: Optional[str], run_timestamp: Optional[str]) -> List[tuple]:
        """One parameter tuple per item, in INSERT_COLUMNS order."""
        return [(generation_id, run_timestamp, item["rank"], item["title"],
                 item["description"], item["rationale"], item["related_area"],
                 item["evidence"], window_start, window_end, reviews_analysed,
                 llm_provider, llm_model)
                for item in items]

    @classmethod
    def insert_on(cls, conn, rows: Sequence[tuple]) -> None:
        """Insert every row in ONE transaction: either the whole generation
        is saved or none of it is."""
        sql = cls.insert_sql()
        cur = conn.cursor()
        try:
            for row in rows:
                cur.execute(sql, row)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def save(self, items: Sequence[Dict[str, Any]], window_start, window_end,
             reviews_analysed: int, llm_provider: Optional[str], llm_model: Optional[str],
             run_timestamp: Optional[str] = None) -> str:
        """Save one generation and return its generation_id. Applies the
        schema migrations first, so the table exists on a fresh database."""
        generation_id = str(uuid.uuid4())
        rows = self.rows(items, generation_id, window_start, window_end, reviews_analysed,
                         llm_provider, llm_model, run_timestamp)
        conn = connect(self.settings)
        try:
            ensure_schema(conn)
            self.insert_on(conn, rows)
        finally:
            conn.close()
        logger.info("Saved %d action items (generation %s) into dbo.%s",
                    len(rows), generation_id, ACTION_ITEMS_TABLE)
        return generation_id
