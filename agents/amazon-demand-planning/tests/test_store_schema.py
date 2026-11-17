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


# ---- the schema-aware loader --------------------------------------------------------------------

import datetime as dt                                                      # noqa: E402
from pathlib import Path                                                   # noqa: E402

import pandas as pd                                                        # noqa: E402

from fakedb import FakeConn, schema                                        # noqa: E402
from order_forecast import adjustments as adj                              # noqa: E402
from order_forecast.live import build_live_forecast, load_settings        # noqa: E402
from order_forecast.replenish import build_replenish_forecast             # noqa: E402
from order_forecast.snapshot import Snapshot                               # noqa: E402
from order_forecast.store import STATUS_COLUMNS, SUMMARY_COLUMNS, load_forecast   # noqa: E402
from test_replenish import SETTINGS                                        # noqa: E402
from test_young import am_for, young_snap                                  # noqa: E402


def TEN(a):
    """R1 -> B0000000R1: a real-width (10 character) ASIN."""
    return ("B" + a.rjust(9, "0"))[:10]


def ten(snap):
    """The young-product fixture with real-width ASINs."""
    def rn(df):
        return df.assign(asin=df["asin"].map(TEN))
    return Snapshot(snap.pool, rn(snap.sales), rn(snap.inventory), snap.invoices, snap.manifest)


def replenish_forecast():
    return build_replenish_forecast(ten(young_snap()), SETTINGS, weekly=am_for(TEN("R1"), TEN("Y1")),
                                    adjustments=adj.empty())


def _many_cols(sql: str) -> list:
    return [c.strip() for c in re.search(r"\(([^)]*)\) VALUES", sql).group(1).split(",")]


def test_without_the_new_ddl_the_forecast_loads_with_legacy_columns_and_warns(capsys):
    f = replenish_forecast()
    c = FakeConn(schema())                                                  # 001 + 004 only
    run_id = load_forecast(f, "UK", "snap1", conn=c)
    assert c.committed and not c.rolled_back and run_id > 100
    rows = c.many("amazon_order_forecast")
    assert len(rows) == 1 and _many_cols(rows[0][1]) == ["order_forecast_run_id", "asin", "month_start",
                                                         "units_forecast", "units_upper", "order_chance"]
    assert len(rows[0][2]) == len(f.table) and len(rows[0][2][0]) == 6
    assert not c.many("amazon_order_forecast_asin_status")
    assert not c.statements("INSERT INTO dbo.amazon_order_forecast_run_summary")
    assert not [s for _, s, _ in c.statements("DELETE") if "asin_status" in s or "run_summary" in s]
    out = capsys.readouterr().out
    assert "WARNING" in out and "sql/005_run_explanations.sql" in out and "amazon_order_forecast_asin_status" in out


def test_with_the_new_ddl_rows_status_and_summary_are_stored():
    f = replenish_forecast()
    c = FakeConn(schema(new_columns=True, status=True, summary=True))
    run_id = load_forecast(f, "UK", "snap1", conn=c)
    rows = c.many("amazon_order_forecast")[0]
    assert _many_cols(rows[1])[-4:] == ["basis", "reason_code", "reason_detail", "confidence"]
    first = dict(zip(_many_cols(rows[1]), rows[2][0]))
    assert first["reason_code"] == f.table.iloc[0]["reason_code"] and first["basis"] == f.table.iloc[0]["basis"]
    st = c.many("amazon_order_forecast_asin_status")[0]
    assert _many_cols(st[1]) == ["order_forecast_run_id", *STATUS_COLUMNS]
    assert len(st[2]) == len(f.extras["decisions"]) and all(r[0] == run_id for r in st[2])
    assert all(v is None or isinstance(v, (str, int, float, bool)) for r in st[2] for v in r)
    summ = c.statements("INSERT INTO dbo.amazon_order_forecast_run_summary")
    assert len(summ) == 1 and len(summ[0][2]) == len(SUMMARY_COLUMNS) + 2
    p = dict(zip(["order_forecast_run_id", *SUMMARY_COLUMNS, "summary_json"], summ[0][2]))
    assert p["young_product_backups"] == 2 and p["amazon_generation_date"] == dt.date(2026, 10, 11)
    assert p["order_forecast_run_id"] == run_id and '"young_product_backups": 2' in p["summary_json"]
    # the explanation tables are cleared for this run's key before the forecast rows, in the same transaction
    order = [s.split(" JOIN")[0].split(" WHERE")[0] for _, s, _ in c.statements("DELETE")]
    assert order == ["DELETE x FROM dbo.amazon_order_forecast_asin_status x",
                     "DELETE x FROM dbo.amazon_order_forecast_run_summary x",
                     "DELETE f FROM dbo.amazon_order_forecast f",
                     "DELETE FROM dbo.amazon_order_forecast_run"]
    assert c.log[-1] == ("commit",)


def test_partly_applied_ddl_writes_what_exists(capsys):
    f = replenish_forecast()
    c = FakeConn(schema(new_columns=True))                                 # columns yes, tables no
    load_forecast(f, "UK", conn=c)
    assert "reason_code" in _many_cols(c.many("amazon_order_forecast")[0][1])
    assert not c.many("amazon_order_forecast_asin_status")
    warning = capsys.readouterr().out
    assert "amazon_order_forecast_run_summary" in warning and "columns basis" not in warning


def test_a_failing_schema_probe_still_loads_the_legacy_forecast(capsys):
    c = FakeConn(schema(new_columns=True, status=True, summary=True), probe_fails=True)
    load_forecast(replenish_forecast(), "UK", conn=c)
    assert c.committed and len(c.many("amazon_order_forecast")[0][2][0]) == 6
    assert "could not read INFORMATION_SCHEMA" in capsys.readouterr().out


def test_seasonal_loads_never_probe_the_schema():
    f = build_live_forecast(ten(young_snap()), load_settings())
    c = FakeConn(schema(new_columns=True, status=True, summary=True))
    load_forecast(f, "US", conn=c)
    assert not c.statements("SELECT TABLE_NAME") and len(c.many("amazon_order_forecast")[0][2][0]) == 4


def test_a_de_rerun_deletes_only_the_de_run_in_every_table():
    f = replenish_forecast()
    te = f.train_end.date()
    runs = [dict(id=1, marketplace="UK", method="replenish", target="ordered_units", train_end=te),
            dict(id=2, marketplace="FR", method="replenish", target="ordered_units", train_end=te),
            dict(id=3, marketplace="DE", method="replenish", target="ordered_units", train_end=te),
            dict(id=4, marketplace="DE", method="replenish_unadjusted", target="ordered_units", train_end=te)]
    c = FakeConn(schema(new_columns=True, status=True, summary=True), runs=runs)
    new_id = load_forecast(f, "DE", conn=c)
    assert c.deleted == [("amazon_order_forecast_asin_status", (3,)), ("amazon_order_forecast_run_summary", (3,)),
                         ("amazon_order_forecast", (3,)), ("amazon_order_forecast_run", (3,))]
    assert sorted(r["id"] for r in c.runs) == [1, 2, 4, new_id]
    assert all(p == ("DE", "replenish", "ordered_units", te) for _, _, p in c.statements("DELETE"))


def test_the_unadjusted_run_stores_its_own_explanations(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("marketplace,asin,month_start,type,value,note,entered_by,valid_until\n"
                 + "UK," + TEN("R1") + ",,demand_multiplier,2,deal,x,\n")
    f = build_replenish_forecast(ten(young_snap()), SETTINGS, weekly=None,
                                 adjustments=adj.load_adjustments(p, "UK", pd.Timestamp("2026-10-01")))
    u = f.extras["unadjusted"]
    assert u.extras["summary"]["method"] == "replenish_unadjusted" and len(u.extras["decisions"])
    c = FakeConn(schema(new_columns=True, status=True, summary=True))
    load_forecast(u, "UK", conn=c)
    assert c.many("amazon_order_forecast_asin_status") and c.statements("INSERT INTO dbo.amazon_order_forecast_run_summary")
    assert c.statements("INSERT INTO dbo.amazon_order_forecast_run ")[0][2][1] == "replenish_unadjusted"


def test_status_rows_with_odd_asins_are_kept_out_with_a_warning(capsys):
    f = build_replenish_forecast(young_snap(), SETTINGS, weekly=None, adjustments=adj.empty())   # 2-character ASINs
    c = FakeConn(schema(new_columns=True, status=True, summary=True))
    load_forecast(f, "UK", conn=c)
    assert c.committed and not c.many("amazon_order_forecast_asin_status")
    assert "not 10 characters" in capsys.readouterr().out


def test_order_forecast_never_references_the_sku_map():
    pkg = Path(__file__).resolve().parent.parent / "order_forecast"
    hits = [p.name for p in pkg.rglob("*.py") if "amazon_asin_sku_map" in p.read_text(encoding="utf-8").lower()]
    assert hits == []
