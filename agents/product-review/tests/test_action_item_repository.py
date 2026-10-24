"""ActionItemRepository's SQL and transaction handling, and the
sentiment_action_items migration. No database connection is made -- only SQL
text and a recording fake connection are inspected."""
import re
from datetime import date
from pathlib import Path

import pytest

from daylight_sentiment.infra.db import (
    ACTION_ITEMS_TABLE,
    REVIEWS_TABLE,
    RUNS_TABLE,
    ActionItemRepository,
)
from daylight_sentiment.infra.db import action_item_repository
from daylight_sentiment.infra.db.connection import MIGRATIONS

SQL_DIR = Path(__file__).resolve().parent.parent / "SQL"


def test_fetch_columns_pinned():
    assert ActionItemRepository.FETCH_COLUMNS == (
        "uid", "review_type", "review_source", "sentiment", "confidence", "rating",
        "sector_group", "primary_aspect", "product_region", "resolved_sku", "product_name",
        "key_themes", "review_date_parsed")
    assert ActionItemRepository.NEGATIVE_TEXT_COLUMNS == ("review_text_en", "review_text")


def test_fetch_sql_reads_analysed_rows_from_the_view_only():
    sql = ActionItemRepository.fetch_sql()
    assert "FROM dbo.vw_review_analysis" in sql
    assert "WHERE sentiment IS NOT NULL" in sql
    assert "reviewer_name" not in sql
    assert "SELECT *" not in sql
    for col in ActionItemRepository.NEGATIVE_TEXT_COLUMNS:
        assert f"CASE WHEN sentiment = 'negative' THEN {col} END AS {col}" in sql
    selected = sql.split("SELECT", 1)[1].split("FROM", 1)[0]
    for col in ActionItemRepository.FETCH_COLUMNS:
        assert re.search(rf"\b{col}\b", selected)


def test_insert_columns_pinned_and_placeholders_match():
    assert ActionItemRepository.INSERT_COLUMNS == (
        "generation_id", "run_timestamp", "[rank]", "title", "description", "rationale",
        "related_area", "evidence", "evidence_strength", "window_start", "window_end",
        "reviews_analysed", "llm_provider", "llm_model")
    sql = ActionItemRepository.insert_sql()
    assert sql.startswith(f"INSERT INTO dbo.{ACTION_ITEMS_TABLE} (")
    assert sql.count("?") == len(ActionItemRepository.INSERT_COLUMNS)
    # item_id / generated_at are server-generated, never written
    assert "item_id" not in sql and "generated_at" not in sql


ITEMS = [{"rank": r, "title": f"t{r}", "description": "d", "rationale": "r",
          "related_area": "overall", "evidence": "e", "evidence_strength": "moderate"}
         for r in (1, 2, 3)]


def test_rows_match_insert_columns():
    rows = ActionItemRepository.rows(ITEMS, "gid", date(2025, 9, 1), date(2026, 8, 20),
                                     710, "claude", "m", "2026-10-24T10:00:00")
    assert len(rows) == 3
    cols = [c.strip("[]") for c in ActionItemRepository.INSERT_COLUMNS]
    first = dict(zip(cols, rows[0]))
    assert first == {
        "generation_id": "gid", "run_timestamp": "2026-10-24T10:00:00", "rank": 1,
        "title": "t1", "description": "d", "rationale": "r", "related_area": "overall",
        "evidence": "e", "evidence_strength": "moderate", "window_start": date(2025, 9, 1),
        "window_end": date(2026, 8, 20),
        "reviews_analysed": 710, "llm_provider": "claude", "llm_model": "m"}
    assert {row[0] for row in rows} == {"gid"}


class RecordingConn:
    def __init__(self, fail_on=None):
        self.events = []
        self.fail_on = fail_on
        self.executes = 0

    def cursor(self):
        conn = self

        class Cursor:
            def execute(self, sql, *params):
                conn.executes += 1
                if conn.fail_on == conn.executes:
                    raise RuntimeError("connection dropped")
                conn.events.append(("execute", sql))

        return Cursor()

    def commit(self):
        self.events.append(("commit",))

    def rollback(self):
        self.events.append(("rollback",))

    def close(self):
        self.events.append(("close",))


def test_three_rows_inserted_in_one_transaction():
    conn = RecordingConn()
    rows = ActionItemRepository.rows(ITEMS, "gid", None, None, 1, None, None, None)
    ActionItemRepository.insert_on(conn, rows)
    assert [e[0] for e in conn.events] == ["execute", "execute", "execute", "commit"]


def test_failed_insert_rolls_back_and_commits_nothing():
    conn = RecordingConn(fail_on=2)
    rows = ActionItemRepository.rows(ITEMS, "gid", None, None, 1, None, None, None)
    with pytest.raises(RuntimeError):
        ActionItemRepository.insert_on(conn, rows)
    assert ("commit",) not in conn.events
    assert conn.events[-1] == ("rollback",)


def test_save_applies_migrations_then_inserts_once(monkeypatch):
    conn = RecordingConn()
    monkeypatch.setattr(action_item_repository, "connect", lambda settings: conn)
    gid = ActionItemRepository(settings=None).save(
        ITEMS, date(2025, 9, 1), date(2026, 8, 20), 710, "claude", "m")
    kinds = [e[0] for e in conn.events]
    # ensure_schema: one execute per migration + its own commit; then the
    # 3 inserts and a single commit; then close.
    n = len(MIGRATIONS)
    assert kinds == ["execute"] * n + ["commit"] + ["execute"] * 3 + ["commit", "close"]
    assert re.fullmatch(r"[0-9a-f-]{36}", gid)


def _migration(name):
    return dict(MIGRATIONS)[name]


class TestMigration:
    def test_appended_last_after_existing_entries(self):
        assert [name for name, _ in MIGRATIONS] == [
            "create_reviews_table", "add_model_number", "add_review_country",
            "create_runs_table", "create_action_items_table",
            "add_action_items_evidence_strength"]

    def test_create_guarded_and_idempotent(self):
        sql = _migration("create_action_items_table")
        assert sql.strip().startswith(f"IF OBJECT_ID('dbo.{ACTION_ITEMS_TABLE}', 'U') IS NULL")
        assert f"CREATE TABLE dbo.{ACTION_ITEMS_TABLE}" in sql
        assert "DROP" not in sql.upper() and "DELETE" not in sql.upper()
        assert REVIEWS_TABLE not in sql and RUNS_TABLE not in sql

    def test_create_entry_keeps_its_original_shape(self):
        # Already applied on existing databases: later columns get their
        # own ALTER entries instead.
        assert "evidence_strength" not in _migration("create_action_items_table")

    def test_evidence_strength_added_by_a_guarded_alter(self):
        name, sql = MIGRATIONS[-1]
        assert name == "add_action_items_evidence_strength"
        assert " ".join(sql.split()) == (
            "IF NOT EXISTS (SELECT 1 FROM sys.columns "
            f"WHERE object_id = OBJECT_ID('dbo.{ACTION_ITEMS_TABLE}') "
            "AND name = 'evidence_strength') "
            f"ALTER TABLE dbo.{ACTION_ITEMS_TABLE} ADD evidence_strength NVARCHAR(20) NULL;")

    def test_ensure_schema_applies_the_alter_after_the_create(self):
        conn = RecordingConn()
        from daylight_sentiment.infra.db.connection import ensure_schema
        ensure_schema(conn)
        sqls = [e[1] for e in conn.events if e[0] == "execute"]
        assert sqls[-1] == _migration("add_action_items_evidence_strength")
        assert sqls[-2] == _migration("create_action_items_table")

    def test_schema(self):
        sql = _migration("create_action_items_table")
        for fragment in (
                "item_id            INT IDENTITY PRIMARY KEY",
                "generation_id      UNIQUEIDENTIFIER NOT NULL",
                "generated_at       DATETIME2        NOT NULL DEFAULT SYSUTCDATETIME()",
                "run_timestamp      DATETIME2        NULL",
                "[rank]             TINYINT", "title              NVARCHAR(200)",
                "related_area       NVARCHAR(100)", "window_start       DATE",
                "reviews_analysed   INT", "llm_provider       NVARCHAR(50)",
                "llm_model          NVARCHAR(100)",
                f"CONSTRAINT UQ_{ACTION_ITEMS_TABLE}_generation_rank "
                "UNIQUE (generation_id, [rank])"):
            assert fragment in sql, fragment
        assert "FOREIGN KEY" not in sql.upper()

    def test_canonical_sql_file_is_the_final_shape(self):
        def columns(sql):
            body = sql.split("CREATE TABLE", 1)[1]
            return re.findall(r"^\s+(\[?\w+\]?)\s+(INT|UNIQUEIDENTIFIER|DATETIME2|TINYINT|"
                              r"NVARCHAR\(\w+\)|DATE)(?=\s)", body, re.M)
        canonical = (SQL_DIR / "tables" / "sentiment_action_items.sql").read_text(
            encoding="utf-8")
        expected = columns(_migration("create_action_items_table"))
        expected.insert(expected.index(("evidence", "NVARCHAR(MAX)")) + 1,
                        ("evidence_strength", "NVARCHAR(20)"))
        assert columns(canonical) == expected
        assert len(columns(canonical)) == 16
        # ...and it brings a table created before the column up to date.
        assert " ".join(_migration("add_action_items_evidence_strength").split()) in \
            " ".join(canonical.split())

    def test_view_reads_latest_generation_only(self):
        view = (SQL_DIR / "views" / "vw_latest_action_items.sql").read_text(encoding="utf-8")
        assert "CREATE OR ALTER VIEW dbo.vw_latest_action_items" in view
        assert "SELECT TOP (1) generation_id" in view
        assert "ORDER BY generated_at DESC, item_id DESC" in view
        assert "AS basis" in view

    def test_view_returns_evidence_strength(self):
        view = (SQL_DIR / "views" / "vw_latest_action_items.sql").read_text(encoding="utf-8")
        columns = view.split("CREATE OR ALTER VIEW", 1)[1].split(
            "FROM dbo.sentiment_action_items AS a", 1)[0]
        assert re.search(r"UPPER\(LEFT\(a\.evidence_strength, 1\)\) \+ "
                         r"SUBSTRING\(a\.evidence_strength, 2, 20\)\s+AS evidence_strength,",
                         columns)
