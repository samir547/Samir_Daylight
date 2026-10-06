"""Run-summary upserts (ISSUE-05: the second, unrelated repository that used
to share DatabaseWriter with review rows)."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

from daylight_sentiment.config import DatabaseSettings
from daylight_sentiment.infra.db.connection import RUNS_TABLE, connect

logger = logging.getLogger("multi_source_sentiment")


class RunRepository:
    """Delete-then-insert upserts into dbo.sentiment_analysis_runs, keyed on
    run_timestamp (unique constraint)."""

    def __init__(self, settings: DatabaseSettings):
        self.settings = settings

    @staticmethod
    def upsert_on(conn, summary: Dict[str, Any]) -> None:
        """Upsert using an existing connection (caller manages its lifetime)."""
        meta = summary.get("metadata", {})
        s = summary.get("summary", {})
        overall = s.get("overall_sentiment", {})
        cur = conn.cursor()
        cur.execute(f"DELETE FROM dbo.{RUNS_TABLE} WHERE run_timestamp = ?", meta.get("timestamp"))
        cur.execute(f"""
INSERT INTO dbo.{RUNS_TABLE}
    (run_timestamp, llm_provider, llm_model, total_reviews, total_reviews_analyzed,
     failed_reviews, positive_pct, negative_pct, neutral_pct, elapsed_seconds, full_summary_json)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", meta.get("timestamp"), meta.get("llm_provider"), meta.get("llm_model"),
            meta.get("total_reviews"), s.get("total_reviews_analyzed"), s.get("failed_reviews"),
            overall.get("positive_pct"), overall.get("negative_pct"), overall.get("neutral_pct"),
            meta.get("elapsed_seconds"), json.dumps(summary, ensure_ascii=False))
        conn.commit()

    def upsert(self, summary: Dict[str, Any]) -> None:
        """Upsert on a fresh, self-managed connection."""
        conn = connect(self.settings)
        try:
            self.upsert_on(conn, summary)
        finally:
            conn.close()
