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
        "related_area", "evidence", "evidence_strength", "window_label", "window_months",
        "window_start", "window_end", "reviews_analysed", "llm_provider", "llm_model",
        "source_scope", "review_type_scope", "is_placeholder")
    sql = ActionItemRepository.insert_sql()
    assert sql.startswith(f"INSERT INTO dbo.{ACTION_ITEMS_TABLE} (")
    assert sql.count("?") == len(ActionItemRepository.INSERT_COLUMNS)
    # item_id / generated_at are server-generated, never written
    assert "item_id" not in sql and "generated_at" not in sql


ITEMS = [{"rank": r, "title": f"t{r}", "description": "d", "rationale": "r",
          "related_area": "overall", "evidence": "e", "evidence_strength": "moderate"}
         for r in (1, 2, 3)]


def test_rows_match_insert_columns():
    rows = ActionItemRepository.rows(ITEMS, "gid", "Last 12 months", 12, date(2025, 9, 1),
                                     date(2026, 8, 20), 710, "claude", "m",
                                     "2026-10-24T10:00:00")
    assert len(rows) == 3
    cols = [c.strip("[]") for c in ActionItemRepository.INSERT_COLUMNS]
    first = dict(zip(cols, rows[0]))
    assert first == {
        "generation_id": "gid", "run_timestamp": "2026-10-24T10:00:00", "rank": 1,
        "title": "t1", "description": "d", "rationale": "r", "related_area": "overall",
        "evidence": "e", "evidence_strength": "moderate", "window_label": "Last 12 months",
        "window_months": 12, "window_start": date(2025, 9, 1),
        "window_end": date(2026, 8, 20),
        "reviews_analysed": 710, "llm_provider": "claude", "llm_model": "m",
        # Without a scope: every review, and real items.
        "source_scope": "All", "review_type_scope": "All", "is_placeholder": False}
    assert {row[0] for row in rows} == {"gid"}


def test_rows_carry_the_scope_on_every_row():
    rows = ActionItemRepository.rows(ITEMS, "gid", "Last 3 months", 3, date(2026, 5, 21),
                                     date(2026, 8, 20), 64, "claude", "m", None,
                                     source_scope="Amazon", review_type_scope="Product")
    assert len(rows) == 3
    assert {row[-3:] for row in rows} == {("Amazon", "Product", False)}


PLACEHOLDER = {"rank": 1, "title": "Not enough reviews yet (18 of 30 needed)",
               "description": "No to-do items were generated.", "rationale": "Too few.",
               "related_area": "overall", "evidence": "18 analysed reviews.",
               "evidence_strength": None, "is_placeholder": True}


def test_placeholder_is_one_row_with_no_evidence_strength():
    [row] = ActionItemRepository.rows(
        [PLACEHOLDER], "gid", "Last 3 months", 3, date(2026, 5, 21), date(2026, 8, 20), 18,
        None, None, "2026-10-24T10:00:00", source_scope="Trustpilot",
        review_type_scope="Service", is_placeholder=True)
    cols = [c.strip("[]") for c in ActionItemRepository.INSERT_COLUMNS]
    saved = dict(zip(cols, row))
    assert len(row) == len(cols)
    assert saved["rank"] == 1 and saved["evidence_strength"] is None
    assert saved["title"] == "Not enough reviews yet (18 of 30 needed)"
    assert saved["reviews_analysed"] == 18
    assert (saved["source_scope"], saved["review_type_scope"]) == ("Trustpilot", "Service")
    assert saved["is_placeholder"] is True
    assert saved["llm_provider"] is None and saved["llm_model"] is None
    # The NOT NULL columns of dbo.sentiment_action_items are all filled.
    for name in ("generation_id", "rank", "title", "description", "rationale",
                 "related_area", "evidence", "window_start", "window_end",
                 "reviews_analysed", "is_placeholder"):
        assert saved[name] is not None, name


class RecordingConn:
    def __init__(self, fail_on=None):
        self.events = []
        self.params = []
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
                conn.params.append(params)

        return Cursor()

    def commit(self):
        self.events.append(("commit",))

    def rollback(self):
        self.events.append(("rollback",))

    def close(self):
        self.events.append(("close",))


def test_three_rows_inserted_in_one_transaction():
    conn = RecordingConn()
    rows = ActionItemRepository.rows(ITEMS, "gid", "Last 3 months", 3, None, None, 1,
                                     None, None, None)
    ActionItemRepository.insert_on(conn, rows)
    assert [e[0] for e in conn.events] == ["execute", "execute", "execute", "commit"]


def test_failed_insert_rolls_back_and_commits_nothing():
    conn = RecordingConn(fail_on=2)
    rows = ActionItemRepository.rows(ITEMS, "gid", "Last 3 months", 3, None, None, 1,
                                     None, None, None)
    with pytest.raises(RuntimeError):
        ActionItemRepository.insert_on(conn, rows)
    assert ("commit",) not in conn.events
    assert conn.events[-1] == ("rollback",)


def test_save_applies_migrations_then_inserts_once(monkeypatch):
    conn = RecordingConn()
    monkeypatch.setattr(action_item_repository, "connect", lambda settings: conn)
    gid = ActionItemRepository(settings=None).save(
        ITEMS, "Last 6 months", 6, date(2025, 9, 1), date(2026, 8, 20), 710, "claude", "m")
    kinds = [e[0] for e in conn.events]
    # ensure_schema: one execute per migration + its own commit; then the
    # 3 inserts and a single commit; then close.
    n = len(MIGRATIONS)
    assert kinds == ["execute"] * n + ["commit"] + ["execute"] * 3 + ["commit", "close"]
    assert re.fullmatch(r"[0-9a-f-]{36}", gid)


def test_save_writes_the_scope_and_the_placeholder_flag(monkeypatch):
    conn = RecordingConn()
    monkeypatch.setattr(action_item_repository, "connect", lambda settings: conn)
    gid = ActionItemRepository(settings=None).save(
        ITEMS, "Last 6 months", 6, date(2026, 2, 21), date(2026, 8, 20), 64, "claude", "m",
        source_scope="Amazon", review_type_scope="Product")
    inserts = conn.params[-3:]
    cols = [c.strip("[]") for c in ActionItemRepository.INSERT_COLUMNS]
    for (row,), rank in zip(inserts, (1, 2, 3)):
        saved = dict(zip(cols, row))
        assert (saved["generation_id"], saved["rank"]) == (gid, rank)
        assert (saved["source_scope"], saved["review_type_scope"],
                saved["is_placeholder"]) == ("Amazon", "Product", False)


def test_save_placeholder_inserts_one_row_in_one_transaction(monkeypatch):
    # One row, rank 1, under its own generation_id: UNIQUE (generation_id,
    # [rank]) holds exactly as it does for a generation of 3 items.
    conn = RecordingConn()
    monkeypatch.setattr(action_item_repository, "connect", lambda settings: conn)
    gid = ActionItemRepository(settings=None).save(
        [PLACEHOLDER], "Last 3 months", 3, date(2026, 5, 21), date(2026, 8, 20), 18,
        None, None, source_scope="Trustpilot", review_type_scope="Service",
        is_placeholder=True)
    n = len(MIGRATIONS)
    assert [e[0] for e in conn.events] == (
        ["execute"] * n + ["commit"] + ["execute", "commit", "close"])
    [(row,)] = conn.params[n:]
    saved = dict(zip([c.strip("[]") for c in ActionItemRepository.INSERT_COLUMNS], row))
    assert (saved["generation_id"], saved["rank"], saved["is_placeholder"]) == (gid, 1, True)
    assert saved["evidence_strength"] is None


def test_two_generations_never_share_a_generation_id(monkeypatch):
    monkeypatch.setattr(action_item_repository, "connect", lambda settings: RecordingConn())
    repo = ActionItemRepository(settings=None)
    ids = {repo.save([PLACEHOLDER], "Last 3 months", 3, date(2026, 5, 21), date(2026, 8, 20),
                     18, None, None, source_scope=source, review_type_scope="Product",
                     is_placeholder=True)
           for source in ("Amazon", "Trustpilot", "All")}
    assert len(ids) == 3


def _migration(name):
    return dict(MIGRATIONS)[name]


class TestMigration:
    def test_appended_last_after_existing_entries(self):
        assert [name for name, _ in MIGRATIONS] == [
            "create_reviews_table", "add_model_number", "add_review_country",
            "create_runs_table", "create_action_items_table",
            "add_action_items_evidence_strength", "add_action_items_window_label",
            "add_action_items_window_months", "add_action_items_source_scope",
            "add_action_items_review_type_scope", "add_action_items_is_placeholder"]

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

    @pytest.mark.parametrize("name, column, sql_type", [
        ("add_action_items_evidence_strength", "evidence_strength", "NVARCHAR(20)"),
        ("add_action_items_window_label", "window_label", "NVARCHAR(30)"),
        ("add_action_items_window_months", "window_months", "TINYINT"),
        ("add_action_items_source_scope", "source_scope", "NVARCHAR(20)"),
        ("add_action_items_review_type_scope", "review_type_scope", "NVARCHAR(20)"),
    ])
    def test_later_columns_added_by_guarded_alters(self, name, column, sql_type):
        assert " ".join(_migration(name).split()) == (
            "IF NOT EXISTS (SELECT 1 FROM sys.columns "
            f"WHERE object_id = OBJECT_ID('dbo.{ACTION_ITEMS_TABLE}') "
            f"AND name = '{column}') "
            f"ALTER TABLE dbo.{ACTION_ITEMS_TABLE} ADD {column} {sql_type} NULL;")

    def test_is_placeholder_is_added_not_null_with_a_default(self):
        # NOT NULL DEFAULT 0: the rows already in the table become 0 (real
        # items), and an insert that doesn't name the column still works.
        assert " ".join(_migration("add_action_items_is_placeholder").split()) == (
            "IF NOT EXISTS (SELECT 1 FROM sys.columns "
            f"WHERE object_id = OBJECT_ID('dbo.{ACTION_ITEMS_TABLE}') "
            "AND name = 'is_placeholder') "
            f"ALTER TABLE dbo.{ACTION_ITEMS_TABLE} ADD is_placeholder BIT NOT NULL "
            f"CONSTRAINT DF_{ACTION_ITEMS_TABLE}_is_placeholder DEFAULT 0;")

    def test_scope_columns_are_the_last_three_entries_after_the_window_ones(self):
        assert [name for name, _ in MIGRATIONS[-5:]] == [
            "add_action_items_window_label", "add_action_items_window_months",
            "add_action_items_source_scope", "add_action_items_review_type_scope",
            "add_action_items_is_placeholder"]

    def test_existing_entries_unchanged(self):
        # Already applied on existing databases: never edited in place.
        create = _migration("create_action_items_table")
        assert "window_label" not in create and "window_months" not in create
        assert "window_" not in _migration("add_action_items_evidence_strength")
        for name in ("create_action_items_table", "add_action_items_evidence_strength",
                     "add_action_items_window_label", "add_action_items_window_months"):
            assert "scope" not in _migration(name) and "placeholder" not in _migration(name)

    def test_window_and_scope_alters_touch_only_this_table(self):
        for name in ("add_action_items_window_label", "add_action_items_window_months",
                     "add_action_items_source_scope", "add_action_items_review_type_scope",
                     "add_action_items_is_placeholder"):
            sql = _migration(name)
            assert sql.strip().startswith("IF NOT EXISTS (SELECT 1 FROM sys.columns")
            assert "DROP" not in sql.upper() and "UPDATE" not in sql.upper()
            assert REVIEWS_TABLE not in sql and RUNS_TABLE not in sql

    def test_ensure_schema_applies_the_alters_after_the_create(self):
        conn = RecordingConn()
        from daylight_sentiment.infra.db.connection import ensure_schema
        ensure_schema(conn)
        sqls = [e[1] for e in conn.events if e[0] == "execute"]
        assert sqls[-7:] == [_migration(n) for n in (
            "create_action_items_table", "add_action_items_evidence_strength",
            "add_action_items_window_label", "add_action_items_window_months",
            "add_action_items_source_scope", "add_action_items_review_type_scope",
            "add_action_items_is_placeholder")]

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
                              r"NVARCHAR\(\w+\)|DATE|BIT)(?=\s)", body, re.M)
        canonical = (SQL_DIR / "tables" / "sentiment_action_items.sql").read_text(
            encoding="utf-8")
        expected = columns(_migration("create_action_items_table"))
        after = expected.index(("evidence", "NVARCHAR(MAX)")) + 1
        expected[after:after] = [("evidence_strength", "NVARCHAR(20)"),
                                 ("window_label", "NVARCHAR(30)"), ("window_months", "TINYINT"),
                                 ("source_scope", "NVARCHAR(20)"),
                                 ("review_type_scope", "NVARCHAR(20)"),
                                 ("is_placeholder", "BIT")]
        assert columns(canonical) == expected
        assert len(columns(canonical)) == 21
        create = " ".join(canonical.split("CREATE TABLE", 1)[1].split("GO", 1)[0].split())
        assert ("is_placeholder BIT NOT NULL -- 1 = \"not enough reviews\" row "
                f"CONSTRAINT DF_{ACTION_ITEMS_TABLE}_is_placeholder DEFAULT 0,") in create
        # ...and it brings a table created before the columns up to date.
        for name in ("add_action_items_evidence_strength", "add_action_items_window_label",
                     "add_action_items_window_months", "add_action_items_source_scope",
                     "add_action_items_review_type_scope",
                     "add_action_items_is_placeholder"):
            assert " ".join(_migration(name).split()) in " ".join(canonical.split())

    def test_every_insert_column_exists_in_the_canonical_table(self):
        canonical = (SQL_DIR / "tables" / "sentiment_action_items.sql").read_text(
            encoding="utf-8")
        create = canonical.split("CREATE TABLE", 1)[1].split("GO", 1)[0]
        for column in ActionItemRepository.INSERT_COLUMNS:
            assert re.search(rf"^\s+{re.escape(column)}\s", create, re.M), column


VIEW_FILE = SQL_DIR / "views" / "vw_latest_action_items.sql"


def _view():
    return VIEW_FILE.read_text(encoding="utf-8")


def _view_body():
    body = _view().split("CREATE OR ALTER VIEW dbo.vw_latest_action_items AS", 1)[1]
    return body.split("GO", 1)[0]


def _flat(text):
    return " ".join(text.split())


class TestView:
    def test_create_or_alter_and_no_order_by_in_the_view(self):
        assert "CREATE OR ALTER VIEW dbo.vw_latest_action_items AS" in _view()
        body = _view_body()
        # ORDER BY only inside ROW_NUMBER(), never on the view itself.
        assert body.count("ORDER BY") == 1
        assert "OVER (PARTITION BY" in _flat(body)

    def test_newest_generation_per_window_label_and_scope(self):
        flat = _flat(_view_body())
        assert "COALESCE(a.window_label, N'Custom window') AS window_group" in flat
        assert ("ROW_NUMBER() OVER (PARTITION BY window_group, stored_source_scope, "
                "stored_review_type_scope "
                "ORDER BY generated_at DESC, item_id DESC) AS newest") in flat
        assert "JOIN shown_scope AS x ON x.generation_id = l.generation_id" in flat
        # Only newest generations reach shown_scope, in each of its 3 parts.
        shown = flat.split("shown_scope AS (", 1)[1].split(") SELECT l.window_group", 1)[0]
        assert shown.count("FROM ranked WHERE newest = 1") == 3
        assert "TOP (1)" not in flat  # not one generation across every window

    def test_rows_saved_before_the_scope_columns_read_as_all_all(self):
        flat = _flat(_view_body())
        assert "COALESCE(a.source_scope, N'All') AS stored_source_scope" in flat
        assert "COALESCE(a.review_type_scope, N'All') AS stored_review_type_scope" in flat

    def test_alias_scopes_are_copies_made_with_union_all(self):
        flat = _flat(_view_body())
        shown = flat.split("shown_scope AS (", 1)[1].split(") SELECT l.window_group", 1)[0]
        parts = shown.split("UNION ALL")
        assert len(parts) == 3
        assert ("SELECT generation_id, stored_source_scope AS source_scope, "
                "stored_review_type_scope AS review_type_scope "
                "FROM ranked WHERE newest = 1") in parts[0]
        # (Amazon, Product) is also shown as (Amazon, All)...
        assert ("SELECT generation_id, N'Amazon', N'All' FROM ranked WHERE newest = 1 "
                "AND stored_source_scope = N'Amazon' "
                "AND stored_review_type_scope = N'Product'") in parts[1]
        # ...and (Trustpilot, Service) as (All, Service).
        assert ("SELECT generation_id, N'All', N'Service' FROM ranked WHERE newest = 1 "
                "AND stored_source_scope = N'Trustpilot' "
                "AND stored_review_type_scope = N'Service'") in parts[2]
        # No row is made up for Amazon / Service.
        assert "N'Amazon', N'Service'" not in flat

    def test_alias_scopes_are_never_stored_by_the_pipeline(self):
        # The view would otherwise show a stored generation next to its copy.
        from daylight_sentiment.domain.action_items import REVIEW_SCOPES
        stored = [(s.source_scope, s.review_type_scope) for s in REVIEW_SCOPES]
        assert len(set(stored)) == len(stored) == 6
        assert ("Amazon", "All") not in stored and ("All", "Service") not in stored
        assert ("Amazon", "Service") not in stored
        assert ("Amazon", "Product") in stored and ("Trustpilot", "Service") in stored
        flat = _flat(_view_body())
        for source, review_type in stored:
            assert f"N'{source}'" in flat and f"N'{review_type}'" in flat

    def test_returns_the_columns_power_bi_needs(self):
        # The final SELECT: after the last CTE, up to its FROM.
        select = "l.window_group" + _flat(_view_body()).split(
            ") SELECT l.window_group", 1)[1].split("FROM labelled AS l", 1)[0]
        for column in ("l.window_group AS window_label", "l.window_months",
                       "x.source_scope,", "x.review_type_scope,", "l.is_placeholder,",
                       "l.[rank] AS item_rank", "l.title", "l.description", "l.rationale",
                       "AS related_area", "l.evidence,", "AS evidence_strength", "AS basis",
                       "l.window_start", "l.window_end", "l.reviews_analysed",
                       "l.generated_at", "l.generation_id", "l.run_timestamp",
                       "l.llm_provider", "l.llm_model"):
            assert column in select, column

    def test_readable_related_area_and_evidence_strength(self):
        flat = _flat(_view_body())
        assert ("CASE WHEN l.related_area = 'overall' THEN 'Overall' ELSE l.related_area END "
                "AS related_area") in flat
        assert ("UPPER(LEFT(l.evidence_strength, 1)) + SUBSTRING(l.evidence_strength, 2, 20) "
                "AS evidence_strength") in flat

    def test_basis_and_the_out_of_date_note(self):
        flat = _flat(_view_body())
        assert ("ELSE CONCAT(N'Based on ', l.reviews_analysed, N' reviews from ', "
                "CONVERT(CHAR(10), l.window_start, 23), N' to ', "
                "CONVERT(CHAR(10), l.window_end, 23)) END,") in flat
        assert ("CASE WHEN l.window_end < DATEADD(DAY, -s.stale_after_days, "
                "CAST(SYSDATETIME() AS DATE))") in flat
        assert ("THEN CONCAT(N'. Newest review is from ', CONVERT(CHAR(10), l.window_end, 23), "
                "N', so this may be out of date.') ELSE N'' END) AS basis") in flat

    def test_basis_of_a_placeholder_does_not_read_as_based_on(self):
        flat = _flat(_view_body())
        basis = flat.split("AS evidence_strength,", 1)[1].split("AS basis", 1)[0]
        # The placeholder wordings come first, so "Based on" is only reached
        # by real items.
        assert basis.index("WHEN l.is_placeholder = 1") < basis.index("N'Based on '")
        assert ("WHEN l.is_placeholder = 1 THEN CONCAT(N'Too few reviews for to-do items: ', "
                "l.reviews_analysed, N' from ', CONVERT(CHAR(10), l.window_start, 23), "
                "N' to ', CONVERT(CHAR(10), l.window_end, 23))") in basis
        # No dated reviews at all: the stored window isn't the scope's own,
        # so neither its dates nor the out-of-date note are shown.
        assert ("CASE WHEN l.is_placeholder = 1 AND l.reviews_analysed = 0 THEN N'Too few "
                "reviews for to-do items: none with a review date in this selection'") in basis
        assert "AND NOT (l.is_placeholder = 1 AND l.reviews_analysed = 0)" in basis

    def test_verification_queries_show_every_scope_and_window(self):
        verification = _flat(_view().split("GO", 1)[1])
        assert ("GROUP BY source_scope, review_type_scope, window_label, window_months"
                in verification)
        assert "COUNT(DISTINCT generation_id) AS generations" in verification
        assert ("ORDER BY source_scope, review_type_scope, window_months, window_label, "
                "item_rank;") in verification
        assert "8 scopes (the 6 stored ones plus Amazon / All and All / Service) x 3" in (
            _flat(" ".join(line.lstrip("- ") for line in
                           _view().split("GO", 1)[1].splitlines())))

    def test_45_day_constant_is_named_and_documented(self):
        flat = _flat(_view_body())
        assert "SELECT 45 AS stale_after_days" in flat
        assert "CROSS JOIN settings AS s" in flat
        assert "STALE_AFTER_DAYS" in _view_body()
        assert "stale_after_days (45" in _view()

    def test_header_describes_deployment_and_power_bi(self):
        header = _flat(" ".join(line.lstrip("- ") for line in
                                _view().split("CREATE OR ALTER VIEW", 1)[0].splitlines()))
        assert "applied by an administrator in SSMS" in header
        assert ("single-select slicers on window_label (default \"Last 6 months\"), "
                "source_scope (default \"All\") and review_type_scope (default \"All\")"
                in header)
        assert "date slicer does not drive these items" in header
        assert "run ../tables/sentiment_action_items.sql first" in header
        assert "Amazon / Service does not exist" in header
