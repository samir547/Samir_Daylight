"""ISSUE-04: the decomposed CLI phases -- arg parsing, config overrides, the
rerun-flag logic, and checkpoint writing (all offline)."""
import json
import re
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
        # Python 3.12.8+ prints the choices without quotes.
        assert re.search(r"choose from '?claude'?\)", capsys.readouterr().err)

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
               "move_synced_files_to_processed", "run_translation", "run_action_items"}
NEW_RESULT = {"uid": "trustpilot:product:1", "analysis": {"sentiment": "positive"}}


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
        state = {"sync": None, "move": None, "results": [NEW_RESULT], "done": {},
                 "action": None, "action_kwargs": []}
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
                return list(state["results"])

        def fake_move(config, sync_results):
            calls.append("move_synced_files_to_processed")
            if state["move"]:
                raise state["move"]

        def fake_action_items(config, analyzer, **kwargs):
            calls.append("run_action_items")
            state["action_kwargs"].append(kwargs)
            if state["action"]:
                raise state["action"]

        summary = {"total_reviews_analyzed": 0, "failed_reviews": 0, "overall_sentiment": {}}
        monkeypatch.setattr(cli, "sync_sharepoint", fake_sync)
        monkeypatch.setattr(cli, "load_reviews", record("load_reviews", []))
        monkeypatch.setattr(cli, "build_analyzer", record("build_analyzer", object()))
        monkeypatch.setattr(cli, "resume_state", lambda *a: dict(state["done"]))
        monkeypatch.setattr(cli, "MultiSourcePipeline", FakePipeline)
        monkeypatch.setattr(cli, "write_outputs", record(
            "write_outputs", ({"summary": summary, "metadata": {"timestamp": "2026-10-24T09:00:00"}},
                              tmp_path / "s.json", tmp_path / "d.json")))
        monkeypatch.setattr(cli, "load_database", record("load_database"))
        monkeypatch.setattr(cli, "delete_analysis_outputs", record("delete_analysis_outputs"))
        monkeypatch.setattr(cli, "move_synced_files_to_processed", fake_move)
        monkeypatch.setattr(cli, "run_translation", record("run_translation"))
        monkeypatch.setattr(cli, "run_action_items", fake_action_items)

        def go(argv=(), sync=None, move=None, results=None, done=None, action=None):
            state["sync"], state["move"], state["action"] = sync, move, action
            if results is not None:
                state["results"] = results
            if done is not None:
                state["done"] = done
            calls.clear()
            return cli.main(list(argv)), calls
        go.state = state
        go.config = config
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


class TestActionItemsWiring:
    """The action-items step's position, gating and failure handling in
    main(), all phases faked."""

    run = TestMainPhases.run

    def test_parse_args_flags(self):
        args = parse_args(["--skip-action-items", "--action-items-months", "6",
                           "--action-items-only", "--action-items-dry-run"])
        assert args.skip_action_items and args.action_items_only
        assert args.action_items_dry_run and args.action_items_months == 6
        defaults = parse_args([])
        assert defaults.action_items_months is None
        assert not (defaults.skip_action_items or defaults.action_items_only
                    or defaults.action_items_dry_run)

    @pytest.mark.parametrize("value", ["0", "-3", "six"])
    def test_months_must_be_positive(self, value):
        with pytest.raises(SystemExit):
            parse_args(["--action-items-months", value])

    def test_months_flag_overrides_config(self, monkeypatch):
        monkeypatch.setenv("ACTION_ITEMS_WINDOW_MONTHS", "18")
        assert build_config(parse_args([])).action_items.window_months == 18
        config = build_config(parse_args(["--action-items-months", "6"]))
        assert config.action_items.window_months == 6

    def test_runs_after_load_move_and_translation(self, run):
        code, calls = run(sync=_sync_results("trustpilot_product"))
        assert code == 0
        order = [c for c in calls if c in ("load_database", "move_synced_files_to_processed",
                                           "run_translation", "run_action_items")]
        assert order == ["load_database", "move_synced_files_to_processed",
                         "run_translation", "run_action_items"]
        assert run.state["action_kwargs"] == [
            {"run_timestamp": "2026-10-24T09:00:00", "dry_run": False}]

    def test_skipped_when_nothing_new_in_sharepoint(self, run):
        code, calls = run(sync=_sync_results())
        assert code == 0 and "run_action_items" not in calls

    def test_skipped_when_no_review_was_newly_analysed(self, run):
        # Everything replayed from the checkpoint...
        code, calls = run(done={NEW_RESULT["uid"]: NEW_RESULT})
        assert code == 0 and "run_action_items" not in calls
        # ...or every new review failed this run.
        code, calls = run(results=[{"uid": "x", "analysis": None, "error": "boom"}], done={})
        assert code == 0 and "run_action_items" not in calls

    def test_count_newly_analysed(self):
        assert cli.count_newly_analysed([NEW_RESULT], {}) == 1
        assert cli.count_newly_analysed([NEW_RESULT], {NEW_RESULT["uid"]: NEW_RESULT}) == 0

    def test_skip_flag(self, run):
        code, calls = run(["--skip-action-items"])
        assert code == 0 and "run_action_items" not in calls
        assert "run_translation" in calls

    def test_skip_db_load_skips_it(self, run):
        code, calls = run(["--skip-db-load"])
        assert code == 0 and "run_action_items" not in calls

    def test_no_database_configured_skips_it(self, run):
        run.config.db = DatabaseSettings()
        code, calls = run()
        assert code == 0 and "run_action_items" not in calls

    def test_load_db_only_skips_it(self, run):
        code, calls = run(["--load-db-only"])
        assert code == 0 and "run_action_items" not in calls

    def test_runs_with_skip_translation(self, run):
        code, calls = run(["--skip-translation"])
        assert code == 0 and "run_translation" not in calls
        assert calls[-1] == "run_action_items"

    @pytest.mark.parametrize("error", [
        RuntimeError("Invalid object name 'dbo.vw_review_analysis'"),
        ValueError("expected 3 items, got 2"),
    ])
    def test_failure_keeps_the_exit_code_and_logs_one_error(self, run, caplog, error):
        with caplog.at_level("INFO", logger="multi_source_sentiment"):
            code, calls = run(sync=_sync_results("trustpilot_product"), action=error)
        assert code == 0
        assert calls[-1] == "run_action_items"
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1
        assert "Action items were not generated" in errors[0].getMessage()
        assert str(error) in errors[0].getMessage()

    def test_fatal_provider_error_keeps_its_handling(self, run):
        from daylight_sentiment.infra.analyzers import FatalProviderError
        code, _ = run(action=FatalProviderError("invalid x-api-key"))
        assert code == 1

    def test_dry_run_flag_passed_through(self, run):
        run(["--action-items-dry-run"])
        assert run.state["action_kwargs"][-1]["dry_run"] is True

    def test_only_mode_skips_everything_else(self, run):
        code, calls = run(["--action-items-only"], sync=_sync_results("amazon_brightdata"))
        assert code == 0
        assert calls == ["build_analyzer", "run_action_items"]
        assert run.state["action_kwargs"] == [{"dry_run": False}]

    def test_only_mode_dry_run(self, run):
        code, calls = run(["--action-items-only", "--action-items-dry-run"])
        assert code == 0 and calls == ["build_analyzer", "run_action_items"]
        assert run.state["action_kwargs"] == [{"dry_run": True}]

    def test_only_mode_failure_exits_1(self, run, caplog):
        code, _ = run(["--action-items-only"], action=ValueError("expected 3 items, got 4"))
        assert code == 1
        assert "Action items were not generated: ValueError" in caplog.text

    def test_only_mode_fatal_provider_error_exits_1(self, run, caplog):
        from daylight_sentiment.infra.analyzers import FatalProviderError
        code, _ = run(["--action-items-only"], action=FatalProviderError("no credit"))
        assert code == 1
        assert "Stopping: the claude API rejected the request" in caplog.text

    def test_only_mode_without_database_exits_1(self, run):
        run.config.db = DatabaseSettings()
        code, calls = run(["--action-items-only"])
        assert code == 1 and calls == []


class TestActionItemsEndToEndOffline:
    """The real run_action_items() + generate_action_items() with a fake
    repository and a fake second provider registered in the analyzer factory."""

    REPLY = {"items": [
        {"rank": r, "title": f"Reduce glare {r}", "description": "Do it.",
         "rationale": "38 of 150 reviews are negative.", "related_area": "Beauty",
         "evidence": "38 negative reviews mention glare."} for r in (1, 2, 3)]}

    def _setup(self, monkeypatch, reply):
        from datetime import date
        from enum import Enum
        from types import SimpleNamespace

        from daylight_sentiment.infra.analyzers import SentimentAnalyzer, factory

        class OtherProvider(Enum):
            FAKE = "fake"

        class FakeProviderAnalyzer(SentimentAnalyzer):
            def __init__(self, config):
                pass

            def complete_json(self, prompt, max_tokens=None):
                return reply

            def analyze_product_review(self, text, product_name=None, rating=None):
                return {}

            def analyze_service_review(self, text, rating=None):
                return {}

        rows = [{"uid": f"u{i}", "review_type": "product", "review_source": "amazon",
                 "sentiment": "negative" if i % 4 == 0 else "positive", "confidence": 0.8,
                 "rating": 3, "sector_group": "Beauty", "primary_aspect": None,
                 "product_region": "UK", "key_themes": '["glare"]',
                 "review_date_parsed": date(2026, 7, 1), "review_text_en": None,
                 "review_text": "too much glare" if i % 4 == 0 else None}
                for i in range(150)]
        saved = []

        class FakeRepository:
            def __init__(self, settings):
                pass

            def fetch_analysed_rows(self):
                return rows

            def save(self, items, *args):
                saved.append((items, args))
                return "gid"

        monkeypatch.setitem(factory._ANALYZERS, OtherProvider.FAKE, FakeProviderAnalyzer)
        monkeypatch.setattr(cli, "ActionItemRepository", FakeRepository)
        config = Config(llm_provider=OtherProvider.FAKE,
                        db=DatabaseSettings(server="s", name="n", user="u", password="p"))
        object.__setattr__(config, "fake", SimpleNamespace(model="fake-model"))
        return config, saved

    def test_second_provider_generates_and_saves(self, monkeypatch):
        config, saved = self._setup(monkeypatch, self.REPLY)
        assert cli.action_items_only(config, dry_run=False) == 0
        [(items, args)] = saved
        assert [i["rank"] for i in items] == [1, 2, 3]
        assert args[2:5] == (150, "fake", "fake-model")

    def test_dry_run_prints_and_writes_nothing(self, monkeypatch, capsys):
        config, saved = self._setup(monkeypatch, self.REPLY)
        assert cli.action_items_only(config, dry_run=True) == 0
        assert saved == []
        out = capsys.readouterr().out
        assert "Based on 150 reviews from 2025-07-02 to 2026-07-01" in out
        assert "1. Reduce glare 1  [Beauty]" in out

    def test_invalid_reply_saves_nothing_and_exits_1(self, monkeypatch):
        config, saved = self._setup(monkeypatch, {"items": self.REPLY["items"][:2]})
        assert cli.action_items_only(config, dry_run=False) == 1
        assert saved == []
