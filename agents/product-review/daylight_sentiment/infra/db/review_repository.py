"""Review-row upserts (ISSUE-05: one repository, one responsibility).

review_id alone is NOT a safe dedup key -- it's only unique per source, not
across sources (Trustpilot and Amazon can both hand out the same id).
uid ("{source}:{type}:{review_id}") is the pipeline's own guaranteed-unique
key, so that's the PRIMARY KEY / MERGE key here: an id that was already
inserted is updated in place, never duplicated, no matter how many times a
run or a --load-db-only reload happens.

Rows with no successful analysis this run (analysis is None -- an LLM call
that errored, hit a billing/quota wall, etc.) are never sent to the
database at all -- see upsert(). Without that guard, a --rerun-* pass that
fails outright (e.g. the Anthropic account running out of credits mid-run)
would upsert NULL sentiment/sector over already-good existing rows,
destroying real data for a reason that has nothing to do with that data's
quality. The checkpoint file has the same property for the same reason.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List

from daylight_sentiment.config import DatabaseSettings
from daylight_sentiment.infra.db.connection import REVIEWS_TABLE, connect

logger = logging.getLogger("multi_source_sentiment")


class ReviewRepository:
    """Idempotent MERGE upserts into dbo.sentiment_analysis_reviews."""

    COLUMNS = ("uid", "review_id", "review_type", "review_source", "rating", "product_name",
               "product_sku", "product_asin", "model_number", "review_country",
               "title", "review_text", "reviewer_name",
               "review_date", "verified_purchase", "sentiment", "confidence",
               "primary_sector", "primary_aspect", "key_themes", "analysis_error")

    def __init__(self, settings: DatabaseSettings):
        self.settings = settings

    @staticmethod
    def row(r: Dict[str, Any]) -> tuple:
        """One parameter tuple per review, in COLUMNS order."""
        analysis = r.get("analysis") or {}
        themes = analysis.get("key_themes")
        return (
            r.get("uid"), r.get("review_id"), r.get("review_type"), r.get("review_source"),
            r.get("rating"), r.get("product_name"), r.get("product_sku"), r.get("product_asin"),
            r.get("model_number"), r.get("review_country"),
            r.get("title"), r.get("text"), r.get("reviewer_name"), r.get("review_date"),
            r.get("verified_purchase"), analysis.get("sentiment"), analysis.get("confidence"),
            analysis.get("primary_sector"), analysis.get("primary_aspect"),
            json.dumps(themes, ensure_ascii=False) if themes else None, r.get("error"),
        )

    @classmethod
    def merge_sql(cls, n_rows: int) -> str:
        """MERGE statement upserting n_rows at once. Reads and writes ONLY
        COLUMNS (+ server-side loaded_at) -- the structural property that
        prevents the write-back NULLing bug caught on 2026-09-12."""
        cols = cls.COLUMNS
        row_placeholder = "(" + ", ".join(["?"] * len(cols)) + ")"
        values_sql = ", ".join([row_placeholder] * n_rows)
        set_clause = ", ".join(f"target.{c} = src.{c}" for c in cols if c != "uid")
        insert_cols = ", ".join(cols)
        insert_vals = ", ".join(f"src.{c}" for c in cols)
        return f"""
MERGE dbo.{REVIEWS_TABLE} AS target
USING (VALUES {values_sql}) AS src ({insert_cols})
ON target.uid = src.uid
WHEN MATCHED THEN UPDATE SET {set_clause}, target.loaded_at = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals});
"""

    def upsert(self, reviews: List[Dict[str, Any]], batch_size: int = 50,
               max_batch_retries: int = 6) -> int:
        """MERGEs rows in small batches, each its own round-trip and commit.

        The network path to this server drops sustained connections under
        load ('Communication link failure' after a few minutes of transfer),
        so each batch is committed independently -- a drop only costs the
        in-flight batch, not everything already loaded -- and a failed batch
        reconnects and retries with backoff rather than aborting the run.
        batch_size=50 keeps params (21 cols x 50 = 1050) well under SQL
        Server's 2100-per-statement limit and keeps each batch's transfer
        small, which so far has stayed reliable where bigger ones haven't.
        """
        # Only rows with a real, successful analysis get written. A review
        # that failed this run (LLM error, billing/quota wall, etc.) is
        # simply skipped -- never upserted -- so it can't blank out
        # already-good sentiment/sector data sitting in the table for that
        # uid from a previous successful run. It stays exactly as it was;
        # the failed review will just get retried on the next run, same as
        # the checkpoint already treats it.
        skip_uids = [r.get("uid") for r in reviews if r.get("uid") and r.get("analysis") is None]
        if skip_uids:
            logger.warning(
                "Not writing %d review(s) with no successful analysis this run "
                "(e.g. %s) -- skipped so a failed reprocess can't overwrite "
                "already-good existing data for those uids.",
                len(skip_uids), skip_uids[0])
        rows = [self.row(r) for r in reviews
                if r.get("uid") and r.get("analysis") is not None]
        total = len(rows)

        conn = connect(self.settings)
        try:
            for i in range(0, total, batch_size):
                chunk = rows[i:i + batch_size]
                flat_params = [v for row in chunk for v in row]
                merge_sql = self.merge_sql(len(chunk))
                done = min(i + batch_size, total)

                for attempt in range(1, max_batch_retries + 1):
                    t0 = time.time()
                    try:
                        conn.cursor().execute(merge_sql, flat_params)
                        conn.commit()
                        logger.info("Upserted %d/%d review rows (%.1fs)",
                                    done, total, time.time() - t0)
                        break
                    except Exception as exc:  # noqa: BLE001 - pyodbc raises many shapes
                        logger.warning("Batch %d-%d failed (attempt %d/%d, %.1fs): %s",
                                       i, done, attempt, max_batch_retries, time.time() - t0, exc)
                        if attempt == max_batch_retries:
                            raise
                        try:
                            conn.close()
                        except Exception:  # noqa: BLE001
                            pass
                        time.sleep(min(5 * attempt, 30))
                        conn = connect(self.settings)
        finally:
            conn.close()
        return total
