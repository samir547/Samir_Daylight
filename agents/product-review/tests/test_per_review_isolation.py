"""A non-fatal error on one review fails only that review: the rest of its
batch keeps its analyses and is checkpointed, the failed review carries its
own error text into failure_reasons and is retried next run, and a
FatalProviderError still stops the run (fake analyzers -- no API calls)."""
import json
import logging
from typing import Any, Dict, List

import pytest

from daylight_sentiment.application.checkpoint import load_checkpoint
from daylight_sentiment.application.output_generator import OutputGenerator
from daylight_sentiment.application.pipeline import MultiSourcePipeline
from daylight_sentiment.config import Config
from daylight_sentiment.infra.analyzers import (
    FatalProviderError,
    ProviderError,
    ResponseTruncatedError,
    SentimentAnalyzer,
)

TRUNCATED = ResponseTruncatedError(
    "Model response hit the output-token limit (max_tokens=500)")


class FakeAnalyzer(SentimentAnalyzer):
    """Raises ``errors[text]`` for the matching review text; analyses the rest."""

    def __init__(self, errors: Dict[str, Exception] = None):
        self.errors = errors or {}
        self.calls: List[str] = []

    def complete_json(self, prompt, max_tokens=None):
        self.calls.append(prompt)
        if prompt in self.errors:
            raise self.errors[prompt]
        return {"sentiment": "positive"}

    def analyze_product_review(self, text, product_name=None, rating=None):
        return self.complete_json(text)

    def analyze_service_review(self, text, rating=None):
        return self.complete_json(text)


def _reviews(n: int) -> List[Dict[str, Any]]:
    return [{"uid": f"u{i}", "text": f"t{i}", "review_type": "product"} for i in range(n)]


def _pipeline(analyzer, batch_size: int = 10) -> MultiSourcePipeline:
    config = Config()
    config.max_workers = 1
    config.batch_size = batch_size
    return MultiSourcePipeline(config, analyzer)


class TestAnalyzeBatch:
    def test_one_failure_leaves_the_others_analysed(self):
        bad = json.JSONDecodeError("Expecting value", "", 0)
        results = FakeAnalyzer({"t1": bad}).analyze_batch(_reviews(3), "product")
        assert results[0] == {"sentiment": "positive"}
        assert results[1] is bad
        assert results[2] == {"sentiment": "positive"}

    def test_fatal_error_mid_batch_propagates(self):
        analyzer = FakeAnalyzer({"t1": FatalProviderError("invalid x-api-key")})
        with pytest.raises(FatalProviderError):
            analyzer.analyze_batch(_reviews(3), "product")
        assert analyzer.calls == ["t0", "t1"]


class TestProcessBatch:
    def test_failed_review_gets_its_own_error(self, caplog):
        pipeline = _pipeline(FakeAnalyzer({"t1": TRUNCATED}))
        with caplog.at_level(logging.WARNING, logger="multi_source_sentiment"):
            out = pipeline.process_batch(_reviews(3), "product")
        assert [r["analysis"] is not None for r in out] == [True, False, True]
        assert out[1]["error"] == f"ResponseTruncatedError: {TRUNCATED}"
        assert out[0]["error"] is None and out[2]["error"] is None
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "u1" in warnings[0].getMessage() and "output-token limit" in warnings[0].getMessage()
        assert warnings[0].exc_info is None

    def test_every_review_failing_does_not_raise(self):
        errors = {f"t{i}": ProviderError(f"bad request {i}") for i in range(3)}
        out = _pipeline(FakeAnalyzer(errors)).process_batch(_reviews(3), "product")
        assert all(r["analysis"] is None for r in out)
        assert [r["error"] for r in out] == [
            f"ProviderError: bad request {i}" for i in range(3)]

    def test_fatal_error_mid_batch_propagates(self):
        analyzer = FakeAnalyzer({"t1": FatalProviderError("credit balance is too low")})
        with pytest.raises(FatalProviderError):
            _pipeline(analyzer).process_batch(_reviews(3), "product")


class TestProcessAllReviews:
    def test_partial_failure_is_isolated_checkpointed_and_retried(self, tmp_path):
        ckpt = tmp_path / "ckpt.jsonl"
        reviews = _reviews(4)
        bad = json.JSONDecodeError("Expecting value", "", 0)
        results = _pipeline(FakeAnalyzer({"t2": bad})).process_all_reviews(
            reviews, ckpt, resume=False)

        by_uid = {r["uid"]: r for r in results}
        assert {u for u, r in by_uid.items() if r["analysis"] is not None} == {"u0", "u1", "u3"}
        assert by_uid["u2"]["analysis"] is None

        done = load_checkpoint(ckpt)
        assert set(done) == {"u0", "u1", "u3"}

        reasons = OutputGenerator._failure_reasons(
            [r for r in results if r["analysis"] is None])
        assert reasons == {"JSONDecodeError: Expecting value: line 1 column 1 (char 0)": 1}
        assert not any("no analysis" in reason for reason in reasons)

        # The next run analyses only the review that failed.
        rerun = FakeAnalyzer()
        results = _pipeline(rerun).process_all_reviews(reviews, ckpt, done_uids=done)
        assert rerun.calls == ["t2"]
        assert all(r["analysis"] is not None for r in results)
        assert set(load_checkpoint(ckpt)) == {"u0", "u1", "u2", "u3"}

    def test_truncated_review_is_isolated_within_its_batch(self, tmp_path):
        ckpt = tmp_path / "ckpt.jsonl"
        results = _pipeline(FakeAnalyzer({"t0": TRUNCATED})).process_all_reviews(
            _reviews(3), ckpt, resume=False)
        failed = [r for r in results if r["analysis"] is None]
        assert [r["uid"] for r in failed] == ["u0"]
        assert set(load_checkpoint(ckpt)) == {"u1", "u2"}
        assert "output-token limit" in next(iter(OutputGenerator._failure_reasons(failed)))

    def test_fatal_error_mid_batch_stops_the_run(self, tmp_path):
        ckpt = tmp_path / "ckpt.jsonl"
        analyzer = FakeAnalyzer({"t1": FatalProviderError("invalid x-api-key")})
        with pytest.raises(FatalProviderError):
            _pipeline(analyzer, batch_size=3).process_all_reviews(
                _reviews(9), ckpt, resume=False)
        # One worker: the fatal error ends the first batch and nothing else starts.
        assert analyzer.calls == ["t0", "t1"]
        assert ckpt.read_text(encoding="utf-8") == ""
