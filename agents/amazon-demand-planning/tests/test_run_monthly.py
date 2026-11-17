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


def _run(tmp_path, rc_from_python: int, env_extra: dict):
    """Copy the script into a scratch agent folder with a fake python that exits with `rc_from_python`."""
    root = tmp_path / "agent"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts" / "run_monthly.sh")
    fake = root / "fakepy"
    fake.write_text(f'#!/usr/bin/env bash\necho "fake python $*" \nexit {rc_from_python}\n', newline="\n")
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
