"""The by-hand DDL (sql/005, sql/006), the init-db guard and the schema-aware loader. Offline only."""
import re

from order_forecast.store import SQL_DIR, SQL_FILES, init_db, split_batches


def _code(name: str) -> str:
    """The SQL without comments, whitespace collapsed."""
    text = (SQL_DIR / name).read_text(encoding="utf-8")
    text = re.sub(r"--[^\n]*", "", text)
    return " ".join(text.split())


def _view(name: str) -> str:
    v = re.search(r"CREATE OR ALTER VIEW dbo\.vw_amazon_order_forecast_sku AS (.*?) GO", _code(name)).group(1)
    return v


# ---- the SQL files ------------------------------------------------------------------------------

def test_new_ddl_is_never_applied_by_application_code():
    assert "005_run_explanations.sql" not in SQL_FILES and "006_view_explanations.sql" not in SQL_FILES


def test_005_adds_only_nullable_columns_and_guards_every_statement():
    code = _code("005_run_explanations.sql")
    adds = re.findall(r"ALTER TABLE dbo\.amazon_order_forecast ADD (\w+) (\w+(?:\(\d+\))?) (NOT NULL|NULL)", code)
    assert {a[0] for a in adds} == {"basis", "reason_code", "reason_detail", "confidence"}
    assert all(a[2] == "NULL" for a in adds)
    assert code.count("IF COL_LENGTH('dbo.amazon_order_forecast'") == 4
    assert "IF OBJECT_ID('dbo.amazon_order_forecast_asin_status', 'U') IS NULL" in code
    assert "IF OBJECT_ID('dbo.amazon_order_forecast_run_summary', 'U') IS NULL" in code
    assert "FOREIGN KEY" not in code
    assert "ROLLBACK" in (SQL_DIR / "005_run_explanations.sql").read_text()
    assert "ROLLBACK" in (SQL_DIR / "006_view_explanations.sql").read_text()


def test_006_is_002_plus_the_four_columns_and_keeps_is_latest():
    old, new = _view("002_views.sql"), _view("006_view_explanations.sql")
    added = ", f.basis, f.reason_code, f.reason_detail, f.confidence"
    assert added in new and new.replace(added, "") == old
    latest = re.search(r"CASE WHEN r\.order_forecast_run_id = .*? END AS is_latest", old).group(0)
    assert latest in new


def test_reason_detail_column_fits_the_longest_possible_detail():
    from order_forecast.replenish import REASON_CODES
    width = int(re.search(r"ADD reason_detail VARCHAR\((\d+)\)", _code("005_run_explanations.sql")).group(1))
    # a row can never carry every code, but even all of them must not overflow by much: the loader also trims
    assert len("|".join(REASON_CODES)) < 2 * width
    assert max(len(c) for c in REASON_CODES) <= 40


# ---- init-db guard ------------------------------------------------------------------------------

class _Cur:
    def __init__(self, view_newer):
        self.view_newer, self.batches = view_newer, []

    def execute(self, sql, *params):
        if sql.startswith("SELECT COL_LENGTH('dbo.vw_amazon_order_forecast_sku'"):
            self._row = (4,) if self.view_newer else (None,)
            return self
        self.batches.append(sql)
        return self

    def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, view_newer):
        self.c = _Cur(view_newer)

    def cursor(self):
        return self.c

    def close(self):
        pass


def test_init_db_skips_002_once_the_view_has_the_006_columns(capsys):
    newer = _Conn(True)
    applied, problems = init_db(newer)
    assert problems == [] and not any("CREATE OR ALTER VIEW" in b for b in newer.c.batches)
    assert "Skipped 002_views.sql" in capsys.readouterr().out
    older = _Conn(False)
    init_db(older)
    assert any("CREATE OR ALTER VIEW" in b for b in older.c.batches)
    n002 = len(split_batches((SQL_DIR / "002_views.sql").read_text()))
    assert len(older.c.batches) == len(newer.c.batches) + n002
