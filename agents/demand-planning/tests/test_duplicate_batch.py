import sys

import pandas as pd
import pytest

import config
from data import loader, queries
from tests.conftest import make_raw


class ReachedFitting(Exception):
    """Raised by a fake to prove the run got past the duplicate check."""


@pytest.mark.parametrize("first, latest, expected", [
    ("2026-10", "2026-10", True),    # re-run on unchanged data
    ("2026-09", "2026-10", True),    # anchor moved backwards
    ("2026-11", "2026-10", False),   # a new month
    ("2026-10", None, False),        # nothing delivered yet
])
def test_batch_already_delivered(first, latest, expected):
    assert loader.batch_already_delivered(first, latest) is expected


def fake_read_sql(table_id, first_month):
    def read(query, label, params=None):
        if query == queries.FORECAST_OUTPUT_EXISTS:
            return pd.DataFrame({"table_id": [table_id]})
        assert query == queries.LATEST_BATCH_FIRST_MONTH
        return pd.DataFrame({"first_month": [first_month]})
    return read


def test_first_month_is_read_from_the_latest_batch(monkeypatch):
    monkeypatch.setattr(loader, "_read_sql", fake_read_sql(12345, "2026-10"))
    assert loader.latest_batch_first_month() == "2026-10"


def test_missing_table_means_nothing_delivered(monkeypatch):
    monkeypatch.setattr(loader, "_read_sql", fake_read_sql(None, None))
    assert loader.latest_batch_first_month() is None


def test_empty_table_means_nothing_delivered(monkeypatch):
    monkeypatch.setattr(loader, "_read_sql", fake_read_sql(12345, None))
    assert loader.latest_batch_first_month() is None


def test_query_derives_first_month_at_max_run_date():
    sql = " ".join(queries.LATEST_BATCH_FIRST_MONTH.split())
    assert "MIN(year_month)" in sql
    assert "run_date = (SELECT MAX(run_date) FROM dbo.forecast_output)" in sql


@pytest.fixture
def main_run(monkeypatch, tmp_path):
    """main.main() with fakes up to the duplicate check; anchor 2026-09."""
    main = pytest.importorskip("main")

    def stop(master):
        raise ReachedFitting

    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(main, "load_all", lambda cached: (make_raw("2026-09"), None, None, None, None))
    monkeypatch.setattr(main.family, "build_family_map", stop)

    def run(*flags, latest="2026-10"):
        monkeypatch.setattr(loader, "latest_batch_first_month", lambda: latest)
        monkeypatch.setattr(sys, "argv", ["main.py", *flags])
        return main.main()
    return run


def test_duplicate_production_run_exits_3_before_fitting(main_run):
    assert main_run("--production", "--write-db", latest="2026-10") == 3


def test_new_month_proceeds(main_run):
    with pytest.raises(ReachedFitting):
        main_run("--production", "--write-db", latest="2026-09")


def test_force_overrides_the_duplicate_check(main_run):
    with pytest.raises(ReachedFitting):
        main_run("--production", "--write-db", "--force", latest="2026-10")


def test_force_without_write_db_is_a_bad_flag_combination(main_run):
    assert main_run("--production", "--force") == 2


def test_check_is_skipped_without_write_db(main_run, monkeypatch):
    def must_not_run():
        raise AssertionError("the database was queried on a run that writes nothing")

    main = pytest.importorskip("main")
    monkeypatch.setattr(sys, "argv", ["main.py", "--production", "--cached"])
    monkeypatch.setattr(loader, "latest_batch_first_month", must_not_run)
    with pytest.raises(ReachedFitting):
        main.main()


def test_development_publish_is_checked_too(main_run, monkeypatch):
    main = pytest.importorskip("main")
    monkeypatch.setattr(main.benchmark, "bdm_coverage_months", lambda bdm_df: [])
    config.TEST_END = "2026-08"
    assert main_run("--auto-window", "--write-db", latest="2026-10") == 3
