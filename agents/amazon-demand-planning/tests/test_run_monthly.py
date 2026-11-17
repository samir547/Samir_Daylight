"""Configuration errors exit 2 and are never retried; offline, nothing reaches a database."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from order_forecast import pull
from order_forecast.cli import main
from order_forecast.errors import ConfigError

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "run_monthly.sh"


# ---- Python side --------------------------------------------------------------------------------

def test_unsupported_pool_is_a_configuration_error_with_code_2(tmp_path):
    with pytest.raises(SystemExit) as e:                       # argparse rejects it before anything runs
        main(["run-cycle", "--pool", "XX", "--no-load"])
    assert e.value.code == 2
    with pytest.raises(ConfigError) as e2:                     # and the pull refuses it if called directly
        pull.pull_snapshot("XX", tmp_path)
    assert e2.value.code == 2 and "XX" in str(e2.value)


def test_missing_settings_file_or_unknown_method_returns_2(tmp_path, capsys):
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "manifest.json").write_text(json.dumps({"pool": "DE", "marketplace": "DE"}))
    for f in ("sales_monthly.csv", "inventory_monthly.csv", "invoices_monthly.csv"):
        (snap / f).write_text("asin,month_start\n")
    assert main(["forecast", "--snapshot", str(snap), "--settings", str(tmp_path / "nope.json")]) == 2
    assert "CANNOT RUN" in capsys.readouterr().out
    bad = tmp_path / "bad.json"
    bad.write_text('{"method": "replenish", "pools": {"DE": {"demand_method": "magic"}}}')
    assert main(["forecast", "--snapshot", str(snap), "--settings", str(bad)]) == 2
    bad.write_text("{not json")
    assert main(["forecast", "--snapshot", str(snap), "--settings", str(bad)]) == 2


# ---- the shell wrapper --------------------------------------------------------------------------

def _bash():
    """A real bash (Git Bash on Windows, never WSL's launcher), or None."""
    cands = [os.environ.get("BASH_FOR_TESTS"), shutil.which("bash"),
             r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"]
    for c in cands:
        if c and Path(c).exists() and "system32" not in c.lower() and "windowsapps" not in c.lower():
            return c
    return None


BASH = _bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available")


def _run(tmp_path, rc_from_python: int, env_extra: dict, argc: bool = False):
    """Copy the script into a scratch agent folder with a fake python that exits with `rc_from_python`.
    The fake prints its argument count, so a test can see how the script split the command line."""
    root = tmp_path / "agent"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts" / "run_monthly.sh")
    fake = root / "fakepy"
    fake.write_text(f'#!/usr/bin/env bash\necho "fake python argc=$# $*"\nexit {rc_from_python}\n', newline="\n")
    os.chmod(fake, 0o755)
    env = {**os.environ, "PY": "./fakepy", "POOL": "DE", **env_extra}
    # Git Bash has no flock; a no-op stand-in keeps the test about exit codes (Linux uses the real one)
    cmd = 'command -v flock >/dev/null 2>&1 || { flock() { return 0; }; export -f flock; }; exec bash scripts/run_monthly.sh'
    p = subprocess.run([BASH, "-c", cmd], cwd=root, env=env, capture_output=True, text=True, timeout=60)
    logs = list((root / "logs").glob("monthly_DE_*.log"))
    return p.returncode, (logs[0].read_text() if logs else ""), root


@needs_bash
def test_script_exit_2_from_python_is_not_retried(tmp_path):
    rc, log, root = _run(tmp_path, 2, {})
    assert rc == 2
    assert log.count("=== attempt") == 1 and "retrying" not in log
    assert not list((root / "logs").glob(".done_*"))


@needs_bash
def test_script_missing_settings_file_exits_2_before_running(tmp_path):
    rc, log, root = _run(tmp_path, 0, {"SETTINGS": "config/does_not_exist.json"})
    assert rc == 2 and "not found" in log and "=== attempt" not in log


@needs_bash
def test_script_success_marks_the_pool_month_done(tmp_path):
    (tmp_path / "agent" / "config").mkdir(parents=True)
    (tmp_path / "agent" / "config" / "s.json").write_text("{}")
    rc, log, root = _run(tmp_path, 0, {"SETTINGS": "config/s.json"})
    assert rc == 0 and "settings config/s.json" in log
    assert len(list((root / "logs").glob(".done_DE_*"))) == 1


def test_crontab_example_runs_every_pool_with_replenish_settings_an_hour_apart():
    lines = [ln for ln in (SCRIPT.parent / "crontab.example").read_text().splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    pools = [ln.split("POOL=")[1].split()[0] for ln in lines]
    assert sorted(pools) == sorted(pull.POOL_MARKETPLACES)
    assert all("SETTINGS=config/forecast_settings.replenish.json" in ln for ln in lines)
    hours = [int(ln.split()[1]) for ln in lines]
    assert len(set(hours)) == len(hours) and all(ln.split()[2] == "6-12" for ln in lines)
    assert (SCRIPT.parent.parent / "config" / "forecast_settings.replenish.json").exists()


# ---- review fixes: load order, config errors, SETTINGS quoting -----------------------------------

def test_run_cycle_loads_the_unadjusted_run_first_so_the_main_run_is_latest(tmp_path, monkeypatch):
    from order_forecast import amazon_forecast, store
    from order_forecast.snapshot import save_snapshot
    from test_young import young_snap
    s = young_snap()
    d = save_snapshot(tmp_path / "snaps" / "UK" / "20261110T120000Z", "UK", s.sales, s.inventory, s.invoices,
                      {"marketplace": "UK"})
    adj = tmp_path / "adj.csv"
    adj.write_text("marketplace,asin,month_start,type,value,note,entered_by,valid_until\nUK,R1,,demand_multiplier,2,deal,x,\n")
    cfg = tmp_path / "s.json"
    cfg.write_text(json.dumps({"method": "replenish", "adjustments_file": str(adj)}))
    loaded = []
    monkeypatch.setattr(pull, "pull_snapshot", lambda pool, out, **kw: d)
    monkeypatch.setattr(amazon_forecast, "pull_forecast", lambda snap_dir, mk: Path(snap_dir) / "amazon_forecast.csv")
    monkeypatch.setattr(store, "load_forecast", lambda f, mk, snapshot_ref="", conn=None: loaded.append(f.method) or len(loaded))
    monkeypatch.setattr("order_forecast.cli.ROOT", tmp_path)          # results/live goes to the temp folder
    assert main(["run-cycle", "--pool", "UK", "--settings", str(cfg), "--allow-stale"]) == 0
    assert loaded == ["replenish_unadjusted", "replenish"]


@pytest.mark.parametrize("content,needle", [
    (b"\xff\xfe\x00b\x00a\x00d", "cannot be read"),                    # not UTF-8
    (b"", "cannot be read"),                                            # empty file
    (b"market;asin;type\nUK;R1;discontinue\n", "has no asin or type column"),   # wrong separator: one column
])
def test_unreadable_or_headerless_adjustments_file_is_a_configuration_error(tmp_path, content, needle):
    import pandas as pd
    from order_forecast.adjustments import load_adjustments
    p = tmp_path / "a.csv"
    p.write_bytes(content)
    with pytest.raises(ConfigError) as e:
        load_adjustments(p, "UK", pd.Timestamp("2026-10-01"))
    assert e.value.code == 2 and needle in str(e.value)


def test_malformed_pools_block_is_a_configuration_error(tmp_path):
    from order_forecast.live import load_settings
    p = tmp_path / "s.json"
    for bad in ('{"pools": ["ES"]}', '{"pools": {"ES": 2}}'):
        p.write_text(bad)
        with pytest.raises(ConfigError):
            load_settings(p)


@needs_bash
def test_script_keeps_a_settings_path_with_spaces_as_one_argument(tmp_path):
    (tmp_path / "agent" / "config").mkdir(parents=True)
    (tmp_path / "agent" / "config" / "my settings.json").write_text("{}")
    rc, log, root = _run(tmp_path, 0, {"SETTINGS": "config/my settings.json"}, argc=True)
    assert rc == 0
    assert "argc=7 -m order_forecast run-cycle --pool DE --settings config/my settings.json" in log
