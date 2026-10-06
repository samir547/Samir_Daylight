"""ISSUE-04: the decomposed CLI phases -- arg parsing, config overrides, the
rerun-flag logic, and checkpoint writing (all offline)."""
import json

from daylight_sentiment.application.checkpoint import CheckpointWriter, load_checkpoint
from daylight_sentiment.cli import apply_rerun_flags, build_config, parse_args
from daylight_sentiment.config import LLMProvider


class TestParseArgs:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        args = parse_args([])
        assert args.provider == "groq"
        assert args.workers is None
        assert args.batch_size is None
        assert not args.fresh_start
        assert not args.load_db_only

    def test_provider_env_default(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "claude")
        assert parse_args([]).provider == "claude"

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
    def test_tuning_defaults_come_from_config_not_argparse(self, monkeypatch):
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
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
        assert config.claude_model == "claude-x"
        assert config.groq_model == "openai/gpt-oss-120b"
        config = build_config(parse_args(["--provider", "groq", "--model", "groq-x"]))
        assert config.groq_model == "groq-x"
        assert config.llm_provider is LLMProvider.GROQ


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
