"""The sanity check as a warning (settings "sanity_action": "warn"), and what is stored and exposed for it. Offline only."""
import argparse
import json
import re
from pathlib import Path

import numpy as np
import pytest

from fakedb import FakeConn, schema
from order_forecast import cli
from order_forecast.errors import ConfigError
from order_forecast.live import load_settings, sanity_check, sanity_verdict
from order_forecast.store import SQL_DIR, SQL_FILES, SUMMARY_COLUMNS, load_forecast
from test_store_schema import _code, replenish_forecast, ten
from test_young import young_snap

ROOT = Path(__file__).resolve().parent.parent
BLOCK = load_settings()                                        # the shared defaults: block, as before
WARN = {**BLOCK, "sanity_action": "warn"}


def fc(ratio):
    f = replenish_forecast()
    f.sanity_ratio = ratio
    return f


# ---- the verdict ----------------------------------------------------------------------------------

def test_inside_the_usual_range_is_ok_in_both_modes():
    for s in (BLOCK, WARN):
        assert sanity_verdict(fc(1.0), s) == ("ok", None)
        assert sanity_verdict(fc(0.5), s) == ("ok", None) and sanity_verdict(fc(1.6), s) == ("ok", None)


def test_block_mode_is_the_original_behaviour():
    verdict, msg = sanity_verdict(fc(3.17), BLOCK)
    assert verdict == "block" and "3.17x" in msg and "pace" in msg and "allowed 0.5 to 1.6" in msg
    assert sanity_check(fc(3.17), BLOCK) == msg


@pytest.mark.parametrize("ratio", [1.7, 2.85, 3.17, 4.99, 0.3, 0.26])
def test_warn_mode_loads_inside_the_hard_limits_with_a_warning(ratio):
    verdict, msg = sanity_verdict(fc(ratio), WARN)
    assert verdict == "warn" and f"{ratio:.2f}x" in msg and "pace" in msg and "review" in msg
    assert sanity_check(fc(ratio), WARN) is None                 # not a reason to refuse


@pytest.mark.parametrize("ratio", [5.01, 9.0, 0.24, 0.0, float("nan"), float("inf")])
def test_warn_mode_still_refuses_outside_the_hard_limits_or_without_a_number(ratio):
    verdict, msg = sanity_verdict(fc(ratio), WARN)
    assert verdict == "block" and sanity_check(fc(ratio), WARN) == msg


def test_an_empty_forecast_is_always_refused():
    f = fc(1.0)
    f.table = f.table.iloc[0:0]
    assert sanity_verdict(f, WARN) == ("block", "the forecast is empty")


# ---- settings -------------------------------------------------------------------------------------

def _settings_file(tmp_path, **over):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"method": "replenish", **over}))
    return p


def test_a_bad_sanity_action_or_limits_are_configuration_errors(tmp_path):
    with pytest.raises(ConfigError, match="sanity_action"):
        load_settings(_settings_file(tmp_path, sanity_action="ignore"))
    with pytest.raises(ConfigError, match="hard_min"):
        load_settings(_settings_file(tmp_path, sanity_hard_min=0.6))
    with pytest.raises(ConfigError, match="hard_max"):
        load_settings(_settings_file(tmp_path, sanity_hard_max=1.2))


def test_the_replenish_settings_warn_and_the_defaults_still_block():
    assert load_settings(ROOT / "config" / "forecast_settings.replenish.json")["sanity_action"] == "warn"
    assert load_settings()["sanity_action"] == "block"
    assert load_settings(tmp := ROOT / "config" / "forecast_settings.json")["sanity_action"] == "block" and tmp.exists()


# ---- the command line -----------------------------------------------------------------------------

def _run(monkeypatch, tmp_path, ratio, settings, force=False):
    f = fc(ratio)
    monkeypatch.setattr("order_forecast.snapshot.load_snapshot", lambda p: ten(young_snap()))
    monkeypatch.setattr("order_forecast.live.build_live_forecast", lambda snap, s: f)
    sfile = tmp_path / "settings.json"
    sfile.write_text(json.dumps(settings))
    a = argparse.Namespace(cmd="forecast", snapshot=str(tmp_path / "snap"), settings=str(sfile), out=str(tmp_path), force=force)
    return cli._forecast_and_maybe_load(a), f


def _saved_summary(tmp_path):
    return json.loads(next(tmp_path.rglob("run_summary.json")).read_text())


def test_cli_warns_loads_and_records_the_warning(monkeypatch, tmp_path, capsys):
    rc, f = _run(monkeypatch, tmp_path, 3.17, {"method": "replenish", "sanity_action": "warn"})
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK and "SANITY WARNING:" in out and "3.17x" in out and "SANITY CHECK FAILED" not in out
    s = _saved_summary(tmp_path)
    assert s["sanity_status"] == "warn" and "3.17x" in s["sanity_warning"]


def test_cli_ok_records_ok_and_prints_nothing_extra(monkeypatch, tmp_path, capsys):
    rc, f = _run(monkeypatch, tmp_path, 1.1, {"method": "replenish", "sanity_action": "warn"})
    assert rc == cli.EXIT_OK and "SANITY" not in capsys.readouterr().out
    s = _saved_summary(tmp_path)
    assert s["sanity_status"] == "ok" and s["sanity_warning"] is None


def test_cli_block_mode_and_hard_limits_still_exit_4_and_write_nothing(monkeypatch, tmp_path, capsys):
    rc, _ = _run(monkeypatch, tmp_path, 3.17, {"method": "replenish"})
    assert rc == cli.EXIT_SANITY and "SANITY CHECK FAILED" in capsys.readouterr().out
    rc, _ = _run(monkeypatch, tmp_path, 9.0, {"method": "replenish", "sanity_action": "warn"})
    assert rc == cli.EXIT_SANITY and not list(tmp_path.rglob("forecast.csv"))


def test_cli_force_loads_and_is_recorded_as_forced(monkeypatch, tmp_path, capsys):
    rc, _ = _run(monkeypatch, tmp_path, 9.0, {"method": "replenish", "sanity_action": "warn"}, force=True)
    assert rc == cli.EXIT_OK and "OVERRIDDEN" in capsys.readouterr().out
    assert _saved_summary(tmp_path)["sanity_status"] == "forced"


# ---- the database ---------------------------------------------------------------------------------

def _summary_insert(c):
    (stmt,) = c.statements("INSERT INTO dbo.amazon_order_forecast_run_summary")
    cols = re.search(r"\((.*?)\) VALUES", " ".join(stmt[1].split())).group(1).split(", ")
    return dict(zip(cols, stmt[2]))


def test_the_warning_is_stored_when_sql_007_is_applied():
    f = replenish_forecast()
    f.extras["summary"].update(sanity_status="warn", sanity_warning="Forecast is 3.17x the pace")
    c = FakeConn(schema(new_columns=True, status=True, summary=True, sanity_flags=True))
    run_id = load_forecast(f, "IT", "snap1", conn=c)
    row = _summary_insert(c)
    assert row["order_forecast_run_id"] == run_id and row["sanity_status"] == "warn"
    assert row["sanity_warning"] == "Forecast is 3.17x the pace"
    assert list(row)[1:len(SUMMARY_COLUMNS) + 1] == list(SUMMARY_COLUMNS) and list(row)[-1] == "summary_json"


def test_the_warning_text_is_cut_to_the_column_width():
    f = replenish_forecast()
    f.extras["summary"].update(sanity_status="warn", sanity_warning="x" * 900)
    c = FakeConn(schema(new_columns=True, status=True, summary=True, sanity_flags=True))
    load_forecast(f, "IT", "snap1", conn=c)
    assert len(_summary_insert(c)["sanity_warning"]) == 500


def test_without_sql_007_the_forecast_still_loads_and_the_log_says_what_to_apply(capsys):
    f = replenish_forecast()
    f.extras["summary"].update(sanity_status="warn", sanity_warning="Forecast is 3.17x the pace")
    c = FakeConn(schema(new_columns=True, status=True, summary=True))
    load_forecast(f, "IT", "snap1", conn=c)
    row = _summary_insert(c)
    assert "sanity_status" not in row and "sanity_warning" not in row
    assert "sql/007" in capsys.readouterr().out and c.log[-1] == ("commit",)


def test_a_run_that_was_ok_without_sql_007_says_nothing_extra(capsys):
    f = replenish_forecast()
    f.extras["summary"].update(sanity_status="ok", sanity_warning=None)
    load_forecast(f, "IT", "snap1", conn=FakeConn(schema(new_columns=True, status=True, summary=True)))
    assert "sql/007" not in capsys.readouterr().out


# ---- the SQL file ---------------------------------------------------------------------------------

def test_007_is_by_hand_adds_only_guarded_nullable_columns_and_keeps_is_latest():
    assert "007_sanity_warning_and_views.sql" not in SQL_FILES
    code = _code("007_sanity_warning_and_views.sql")
    adds = re.findall(r"ALTER TABLE dbo\.amazon_order_forecast_run_summary ADD (\w+) (\w+(?:\(\d+\))?) (NOT NULL|NULL)", code)
    assert adds == [("sanity_status", "VARCHAR(10)", "NULL"), ("sanity_warning", "VARCHAR(500)", "NULL")]
    assert code.count("IF COL_LENGTH('dbo.amazon_order_forecast_run_summary'") == 2
    latest = re.search(r"CASE WHEN r\.order_forecast_run_id = .*? END AS is_latest", _code("006_view_explanations.sql")).group(0)
    assert code.count(latest) == 2                                # both views pick the latest run exactly as 006 does
    assert "ROLLBACK" in (SQL_DIR / "007_sanity_warning_and_views.sql").read_text()
    assert "DROP TABLE" not in code and "DELETE" not in code
