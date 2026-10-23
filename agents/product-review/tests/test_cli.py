"""ISSUE-04: the decomposed CLI phases -- arg parsing, config overrides, the
rerun-flag logic, and checkpoint writing (all offline)."""
import json
from pathlib import Path

import pytest

from daylight_sentiment import cli
from daylight_sentiment.application.checkpoint import CheckpointWriter, load_checkpoint
from daylight_sentiment.cli import apply_rerun_flags, build_config, parse_args
from daylight_sentiment.config import Config, DatabaseSettings, LLMProvider
from daylight_sentiment.infra.sharepoint_sync import (
    SharePointSignInRequiredError,
    SourceSyncResult,
)


class TestParseArgs:
    def test_defaults(self):
        args = parse_args([])
        assert args.provider == "claude"
        assert args.workers is None
        assert args.batch_size is None
        assert not args.fresh_start
        assert not args.load_db_only

    def test_provider_choices_come_from_llm_provider(self, capsys):
        with pytest.raises(SystemExit):
            parse_args(["--provider", "not-a-provider"])
        assert "choose from 'claude'" in capsys.readouterr().err

    def test_all_flags_still_exist(self):
        # The production flag surface must not shrink.
        args = parse_args([
            "--provider", "claude", "--model", "m", "--limit", "5",
            "--workers", "2", "--batch-size", "1", "--fresh-start",
            "--rerun-product-reviews", "--rerun-service-reviews",
            "--skip-sharepoint-sync", "--skip-db-load", "--skip-translation",
            "--keep-output-files"])
        assert args.provider == "claude" and args.batch_size == 1
        assert args.rerun_product_reviews and args.rerun_service_reviews


class TestBuildConfig:
    def test_tuning_defaults_come_from_config_not_argparse(self):
        config = build_config(parse_args([]))
        # The old parse_args hardcoded 8/10 as its own defaults, shadowing
        # Config's -- now Config is the single source of truth.
        assert config.max_workers == 8
        assert config.batch_size == 10

    def test_explicit_overrides_apply(self):
        config = build_config(parse_args(["--workers", "3", "--batch-size", "2"]))
        assert config.max_workers == 3
        assert config.batch_size == 2

    def test_model_override_targets_active_provider(self):
        config = build_config(parse_args(["--provider", "claude", "--model", "claude-x"]))
        assert config.llm_provider is LLMProvider.CLAUDE
        assert config.claude_model == "claude-x"
        assert config.model_name == "claude-x"


    def test_claude_model_env_applies_without_flag(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_MODEL", "claude-env-model")
        assert build_config(parse_args([])).model_name == "claude-env-model"

    def test_model_flag_overrides_claude_model_env(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_MODEL", "claude-env-model")
        config = build_config(parse_args(["--model", "claude-flag-model"]))
        assert config.model_name == "claude-flag-model"


class TestApplyRerunFlags:
    DONE = {
        "trustpilot:product:1": {"review_type": "product"},
        "amazon:product:2": {"review_type": "product"},
        "trustpilot:service:3": {"review_type": "service"},
    }

    def test_no_flags_no_change(self):
        assert apply_rerun_flags(dict(self.DONE), False, False) == self.DONE

    def test_rerun_product_reopens_both_product_sources(self):
        out = apply_rerun_flags(dict(self.DONE), True, False)
        assert set(out) == {"trustpilot:service:3"}

    def test_rerun_service_keeps_product(self):
        out = apply_rerun_flags(dict(self.DONE), False, True)
        assert set(out) == {"trustpilot:product:1", "amazon:product:2"}

    def test_both_flags_reopen_everything(self):
        assert apply_rerun_flags(dict(self.DONE), True, True) == {}


class TestCheckpointWriter:
    def test_fresh_run_truncates(self, tmp_path):
        p = tmp_path / "c.jsonl"
        p.write_text('{"uid": "stale", "analysis": {"sentiment": "positive"}}\n',
                     encoding="utf-8")
        with CheckpointWriter(p, resume=False) as w:
            w.write({"uid": "new", "analysis": {"sentiment": "negative"}})
        assert set(load_checkpoint(p)) == {"new"}

    def test_resume_appends_and_last_wins(self, tmp_path):
        p = tmp_path / "c.jsonl"
        with CheckpointWriter(p, resume=False) as w:
            w.write({"uid": "a", "analysis": {"sentiment": "negative"}})
        with CheckpointWriter(p, resume=True) as w:
            w.write({"uid": "a", "analysis": {"sentiment": "positive"}})
        done = load_checkpoint(p)
        assert done["a"]["analysis"]["sentiment"] == "positive"
        assert len(p.read_text(encoding="utf-8").strip().splitlines()) == 2

    def test_non_serialisable_values_stringified(self, tmp_path):
        from datetime import datetime
        p = tmp_path / "c.jsonl"
        with CheckpointWriter(p, resume=False) as w:
            w.write({"uid": "a", "analysis": {"sentiment": "neutral"},
                     "review_date": datetime(2026, 1, 1)})
        rec = json.loads(p.read_text(encoding="utf-8"))
        assert rec["review_date"] == "2026-01-01 00:00:00"


SOURCE_KEYS = ("trustpilot_product", "trustpilot_service", "amazon_brightdata")
# Everything after the sync that analyses, writes, loads, moves or deletes.
WORK_PHASES = {"load_reviews", "build_analyzer", "process_all_reviews", "write_outputs",
               "load_database", "delete_analysis_outputs",
               "move_synced_files_to_processed", "run_translation"}


def _sync_results(*synced_keys):
    results = {}
    for key in SOURCE_KEYS:
        if key in synced_keys:
            item = {"id": f"id-{key}", "name": f"{key}.xlsx"}
            results[key] = SourceSyncResult(key, "x", Path(f"{key}.xlsx"), item)
        else:
            results[key] = SourceSyncResult(key, "x")
    return results


class TestMainPhases:
    """main() wiring with every phase faked -- no SharePoint, LLM or database."""

    @pytest.fixture
    def run(self, monkeypatch, tmp_path):
        calls = []
        state = {"sync": None, "move": None}
        config = Config(db=DatabaseSettings(server="s", name="n", user="u", password="p"))
        config.output_dir = tmp_path

        monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
        monkeypatch.setattr(cli, "build_config", lambda args: config)
        monkeypatch.setattr(cli, "configure_logging", lambda: None)

        def fake_sync(config, skip):
            calls.append("sync_sharepoint")
            if isinstance(state["sync"], Exception):
                raise state["sync"]
            return None if skip else state["sync"]

        def record(name, result=None):
            def fake(*a, **k):
                calls.append(name)
                return result
            return fake

        class FakePipeline:
            def __init__(self, config, analyzer):
                pass

            def process_all_reviews(self, *a, **k):
                calls.append("process_all_reviews")
                return []

        def fake_move(config, sync_results):
            calls.append("move_synced_files_to_processed")
            if state["move"]:
                raise state["move"]

        summary = {"total_reviews_analyzed": 0, "failed_reviews": 0, "overall_sentiment": {}}
        monkeypatch.setattr(cli, "sync_sharepoint", fake_sync)
        monkeypatch.setattr(cli, "load_reviews", record("load_reviews", []))
        monkeypatch.setattr(cli, "build_analyzer", record("build_analyzer", object()))
        monkeypatch.setattr(cli, "resume_state", record("resume_state", {}))
        monkeypatch.setattr(cli, "MultiSourcePipeline", FakePipeline)
        monkeypatch.setattr(cli, "write_outputs", record(
            "write_outputs", ({"summary": summary}, tmp_path / "s.json", tmp_path / "d.json")))
        monkeypatch.setattr(cli, "load_database", record("load_database"))
        monkeypatch.setattr(cli, "delete_analysis_outputs", record("delete_analysis_outputs"))
        monkeypatch.setattr(cli, "move_synced_files_to_processed", fake_move)
        monkeypatch.setattr(cli, "run_translation", record("run_translation"))

        def go(argv=(), sync=None, move=None):
            state["sync"], state["move"] = sync, move
            return cli.main(list(argv)), calls
        return go

    def test_sign_in_required_during_sync_exits_2_and_does_nothing(self, run, caplog):
        code, calls = run(sync=SharePointSignInRequiredError("sign in please"))
        assert code == 2
        assert calls == ["sync_sharepoint"]
        assert "sign in please" in caplog.text

    def test_sign_in_required_during_sync_only_exits_2(self, run):
        code, calls = run(["--sync-sharepoint-only"],
                          sync=SharePointSignInRequiredError("sign in please"))
        assert code == 2 and calls == ["sync_sharepoint"]

    def test_sign_in_required_during_move_exits_2(self, run):
        code, calls = run(sync=_sync_results("trustpilot_product"),
                          move=SharePointSignInRequiredError("sign in please"))
        assert code == 2
        assert calls[-1] == "move_synced_files_to_processed"
        assert "run_translation" not in calls

    def test_nothing_new_in_sharepoint_exits_0_without_work(self, run, caplog):
        with caplog.at_level("INFO", logger="multi_source_sentiment"):
            code, calls = run(sync=_sync_results())
        assert code == 0
        assert calls == ["sync_sharepoint"]
        assert not WORK_PHASES & set(calls)
        assert "No new review files waiting in SharePoint; nothing to do" in caplog.text

    def test_some_sources_synced_runs_the_normal_path(self, run):
        code, calls = run(sync=_sync_results("amazon_brightdata"))
        assert code == 0
        assert WORK_PHASES <= set(calls)

    def test_skip_sharepoint_sync_runs_the_normal_path(self, run):
        code, calls = run(["--skip-sharepoint-sync"], sync=_sync_results())
        assert code == 0
        assert (WORK_PHASES - {"move_synced_files_to_processed"}) <= set(calls)
        assert "move_synced_files_to_processed" not in calls

    def test_sharepoint_not_configured_runs_the_normal_path(self, run):
        code, calls = run(sync=None)
        assert code == 0
        assert (WORK_PHASES - {"move_synced_files_to_processed"}) <= set(calls)

    def test_load_db_only_is_unchanged(self, run):
        code, calls = run(["--load-db-only"], sync=_sync_results())
        assert code == 0
        assert calls == ["load_database", "delete_analysis_outputs"]

    def test_sync_only_with_nothing_new_exits_0(self, run):
        code, calls = run(["--sync-sharepoint-only"], sync=_sync_results())
        assert code == 0 and calls == ["sync_sharepoint"]
