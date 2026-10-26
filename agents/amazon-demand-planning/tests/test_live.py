from datetime import date

import numpy as np
import pandas as pd
import pytest

from order_forecast.live import (build_live_forecast, data_is_current, expected_last_closed_month, load_settings,
                                 sanity_check, settings_for_pool)
from order_forecast.snapshot import Snapshot
from order_forecast.store import load_forecast, split_batches
from order_forecast.series import month_range

MONTHS = month_range("2023-09-01", "2026-09-01")


def make_snap(end="2026-09-01"):
    months = month_range("2023-09-01", end)
    seas = 1 + 0.3 * np.sin(np.arange(len(months)) / 12 * 2 * np.pi)
    rows = [dict(asin=a, month_start=m, ordered_units=50.0 * (i + 1) * s, shipped_units=1.0)
            for i, a in enumerate(["A1", "A2"]) for m, s in zip(months, seas)]
    rows += [dict(asin="NEW", month_start=m, ordered_units=5.0, shipped_units=1.0) for m in months[-4:]]
    sales = pd.DataFrame(rows)
    inv = sales.assign(net_received_units=10.0, sellable_units=100.0, open_po_units=20.0)[
        ["asin", "month_start", "net_received_units", "sellable_units", "open_po_units"]]
    return Snapshot("US", sales, inv, pd.DataFrame(columns=["marketplace", "product_code", "month_start", "gross_units", "credit_units"]),
                    {"marketplace": "US"})


def test_live_forecast_starts_after_last_month_and_skips_short_series():
    f = build_live_forecast(make_snap(), load_settings())
    assert f.table["month_start"].min() == pd.Timestamp("2026-10-01") and f.table["month_start"].max() == pd.Timestamp("2027-03-01")
    assert set(f.table["asin"]) == {"A1", "A2"} and f.skipped_short == 1
    assert (f.table["units_forecast"] >= 0).all() and 0.5 < f.sanity_ratio < 1.6
    assert sanity_check(f, load_settings()) is None


def test_sanity_check_blocks_a_wild_forecast():
    s = make_snap()
    f = build_live_forecast(s, load_settings())
    f.table["units_forecast"] *= 10
    f.sanity_ratio *= 10
    assert "pace" in sanity_check(f, load_settings())


def test_data_freshness():
    snap = make_snap("2026-09-01")
    assert expected_last_closed_month(date(2026, 10, 26)) == pd.Timestamp("2026-09-01")
    assert data_is_current(snap, date(2026, 10, 26))
    assert not data_is_current(snap, date(2026, 11, 8))       # October should be there by now


def test_unknown_method_is_rejected(tmp_path):
    p = tmp_path / "s.json"
    p.write_text('{"method": "magic"}')
    with pytest.raises(SystemExit):
        load_settings(p)


def test_split_batches_on_go():
    assert split_batches("A\nGO\nB\n go \nC") == ["A", "B", "C"]


class FakeCursor:
    def __init__(self, log, fail=False):
        self.log, self.fail, self.fast_executemany = log, fail, False

    def execute(self, sql, params=()):
        self.log.append(("execute", " ".join(sql.split())[:40]))
        return self

    def fetchone(self):
        return (42,)

    def executemany(self, sql, rows):
        if self.fail:
            raise RuntimeError("boom")
        self.log.append(("executemany", len(rows)))


class FakeConn:
    def __init__(self, fail=False):
        self.log, self.fail, self.autocommit = [], fail, True

    def cursor(self):
        return FakeCursor(self.log, self.fail)

    def commit(self):
        self.log.append(("commit",))

    def rollback(self):
        self.log.append(("rollback",))

    def close(self):
        pass


def test_load_is_one_transaction_and_replaces_same_run():
    f = build_live_forecast(make_snap(), load_settings())
    c = FakeConn()
    assert load_forecast(f, "US", "snap1", conn=c) == 42
    kinds = [x[0] for x in c.log]
    assert kinds.count("commit") == 1 and "rollback" not in kinds
    assert c.log[0][1].startswith("DELETE f FROM") and c.log[-2] == ("executemany", len(f.table))


def test_failed_load_rolls_back_and_commits_nothing():
    f = build_live_forecast(make_snap(), load_settings())
    c = FakeConn(fail=True)
    with pytest.raises(RuntimeError):
        load_forecast(f, "US", conn=c)
    assert ("rollback",) in c.log and ("commit",) not in c.log


def test_pool_settings_override(tmp_path):
    p = tmp_path / "s.json"
    p.write_text('{"method": "seasonal_half", "horizon": 6, "pools": {"UK": {"method": "mean12", "horizon": 3}}}')
    s = load_settings(p)
    assert settings_for_pool(s, "US")["method"] == "seasonal_half"
    uk = settings_for_pool(s, "UK")
    assert uk["method"] == "mean12" and uk["horizon"] == 3 and "pools" not in uk
    p.write_text('{"pools": {"UK": {"method": "magic"}}}')
    with pytest.raises(SystemExit):
        load_settings(p)


def test_init_db_reports_problems_and_keeps_going():
    from order_forecast.store import init_db

    class Cur:
        def __init__(self):
            self.n = 0
        def execute(self, sql):
            self.n += 1
            if "CREATE OR ALTER VIEW" in sql:
                raise RuntimeError("The CREATE VIEW permission was denied")

    class Conn:
        def __init__(self):
            self.c = Cur()
        def cursor(self):
            return self.c
        def close(self):
            pass

    applied, problems = init_db(Conn())
    assert applied >= 3 and len(problems) == 1
    assert "002_views.sql" in problems[0] and "ask an admin" in problems[0]


def test_tables_script_has_no_foreign_key():
    from order_forecast.store import SQL_DIR
    assert "FOREIGN KEY" not in (SQL_DIR / "001_order_forecast_tables.sql").read_text().replace("-- ", "").split("PK_amazon_order_forecast PRIMARY KEY (order_forecast_run_id, asin, month_start)")[1]
