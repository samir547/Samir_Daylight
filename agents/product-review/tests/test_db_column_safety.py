"""Characterization: ReviewRepository's MERGE column-safety property.

This pins the exact class of bug caught in test_prompt_on_review.py on
2026-09-12: an upsert whose UPDATE clause writes columns the caller didn't
fetch silently NULLs real data. The structural guarantee tested here is that
the generated MERGE reads and writes ONLY the columns in ReviewRepository.COLUMNS
(plus loaded_at, which is server-generated), and that ReviewRepository.row()
produces exactly one value per column, in order.

No database connection is made -- only the SQL text is inspected.
"""
import re

from daylight_sentiment.infra.db import REVIEWS_TABLE, ReviewRepository

COLS = ReviewRepository.COLUMNS


def test_review_cols_shape():
    assert len(COLS) == 21
    assert COLS[0] == "uid"
    assert len(set(COLS)) == len(COLS)


def test_review_row_matches_cols_in_length():
    row = ReviewRepository.row({
        "uid": "amazon:product:r1", "review_id": "r1", "review_type": "product",
        "review_source": "amazon", "rating": 5, "text": "great",
        "analysis": {"sentiment": "positive", "confidence": 0.9,
                     "primary_sector": "beauty", "key_themes": ["light"]},
    })
    assert len(row) == len(COLS)
    assert row[0] == "amazon:product:r1"
    # key_themes serialised as JSON text, error slot None
    assert row[COLS.index("key_themes")] == '["light"]'
    assert row[COLS.index("analysis_error")] is None


def test_review_row_no_analysis_gives_null_analysis_fields():
    row = ReviewRepository.row({"uid": "x", "error": "boom"})
    assert row[COLS.index("sentiment")] is None
    assert row[COLS.index("key_themes")] is None
    assert row[COLS.index("analysis_error")] == "boom"


def test_merge_placeholder_count_scales_with_rows():
    for n in (1, 2, 50):
        sql = ReviewRepository.merge_sql(n)
        assert sql.count("?") == len(COLS) * n


def test_merge_targets_correct_table_and_key():
    sql = ReviewRepository.merge_sql(1)
    assert f"MERGE dbo.{REVIEWS_TABLE} AS target" in sql
    assert "ON target.uid = src.uid" in sql


def test_update_writes_every_col_except_uid_and_nothing_else():
    sql = ReviewRepository.merge_sql(1)
    set_clause = sql.split("WHEN MATCHED THEN UPDATE SET", 1)[1] \
                    .split("WHEN NOT MATCHED", 1)[0]
    written = set(re.findall(r"target\.(\w+)\s*=", set_clause))
    # loaded_at is refreshed server-side; everything else must be a fetched column.
    assert written == (set(COLS) - {"uid"}) | {"loaded_at"}
    sources = set(re.findall(r"src\.(\w+)", set_clause))
    assert sources == set(COLS) - {"uid"}


def test_insert_covers_exactly_the_fetched_cols():
    sql = ReviewRepository.merge_sql(1)
    insert_clause = sql.split("WHEN NOT MATCHED THEN INSERT", 1)[1]
    inserted = set(re.findall(r"src\.(\w+)", insert_clause))
    assert inserted == set(COLS)
