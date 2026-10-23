"""ISSUE-10: Claude SDK errors mapped onto the typed hierarchy, and a fatal
provider error (bad key, no credit, unknown model) stopping the pipeline and
the translation pass instead of failing every review one by one.

Clients are stubbed -- no real API or database calls are made."""
from types import SimpleNamespace
from typing import Any, Dict, List

import anthropic
import httpx
import pytest

from daylight_sentiment.application.pipeline import MultiSourcePipeline
from daylight_sentiment.config import Config
from daylight_sentiment.infra import translation
from daylight_sentiment.infra.analyzers import (
    ClaudeSentimentAnalyzer,
    FatalProviderError,
    ProviderError,
    RateLimited,
    SentimentAnalyzer,
    TransientProviderError,
)
from daylight_sentiment.infra.analyzers.claude_analyzer import classify_anthropic_error

_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

_STATUS_ERRORS = {
    400: anthropic.BadRequestError,
    401: anthropic.AuthenticationError,
    402: anthropic.APIStatusError,
    403: anthropic.PermissionDeniedError,
    404: anthropic.NotFoundError,
    429: anthropic.RateLimitError,
    500: anthropic.InternalServerError,
}


def _api_error(status: int, message: str = "error") -> anthropic.APIStatusError:
    return _STATUS_ERRORS[status](
        message, response=httpx.Response(status, request=_REQUEST), body=None)


class TestClassifyAnthropicError:
    @pytest.mark.parametrize("status", [401, 402, 403, 404])
    def test_run_wide_statuses_are_fatal(self, status):
        assert isinstance(classify_anthropic_error(_api_error(status)), FatalProviderError)

    def test_low_credit_balance_400_is_fatal(self):
        exc = _api_error(400, "Your credit balance is too low to access the Anthropic API.")
        assert isinstance(classify_anthropic_error(exc), FatalProviderError)

    def test_other_400_affects_only_that_request(self):
        err = classify_anthropic_error(_api_error(400, "prompt is too long"))
        assert type(err) is ProviderError

    def test_429_and_5xx_after_sdk_retries(self):
        assert isinstance(classify_anthropic_error(_api_error(429)), RateLimited)
        assert isinstance(classify_anthropic_error(_api_error(500)), TransientProviderError)

    def test_connection_error_is_transient(self):
        exc = anthropic.APIConnectionError(request=_REQUEST)
        assert isinstance(classify_anthropic_error(exc), TransientProviderError)


def _claude_raising(exc: Exception) -> ClaudeSentimentAnalyzer:
    analyzer = ClaudeSentimentAnalyzer.__new__(ClaudeSentimentAnalyzer)
    analyzer.config = SimpleNamespace(claude_model="stub")

    def create(**kwargs):
        raise exc

    analyzer.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    return analyzer


def test_complete_json_raises_typed_error_chained_to_sdk_error():
    sdk_error = _api_error(401, "invalid x-api-key")
    with pytest.raises(FatalProviderError) as info:
        _claude_raising(sdk_error).complete_json("p")
    assert info.value.__cause__ is sdk_error


class FakeAnalyzer(SentimentAnalyzer):
    """Records every prompt; raises ``error`` on each call when set."""

    def __init__(self, error: Exception = None):
        self.error = error
        self.calls: List[str] = []

    def complete_json(self, prompt: str, max_tokens: int = None) -> Dict[str, Any]:
        self.calls.append(prompt)
        if self.error:
            raise self.error
        return {"translation": "hello"}

    def analyze_product_review(self, text, product_name=None, rating=None):
        self.complete_json(text)
        return {"sentiment": "positive"}

    def analyze_service_review(self, text, rating=None):
        self.complete_json(text)
        return {"sentiment": "positive"}


def _reviews(n: int) -> List[Dict[str, Any]]:
    return [{"uid": f"u{i}", "text": f"t{i}", "review_type": "product"} for i in range(n)]


class TestPipelineStopsOnFatal:
    def _pipeline(self, analyzer):
        config = Config()
        config.max_workers = 1
        config.batch_size = 2
        return MultiSourcePipeline(config, analyzer)

    def test_fatal_error_stops_the_run(self, tmp_path):
        analyzer = FakeAnalyzer(FatalProviderError("invalid x-api-key"))
        with pytest.raises(FatalProviderError):
            self._pipeline(analyzer).process_all_reviews(
                _reviews(10), tmp_path / "ckpt.jsonl", resume=False)
        # One worker, five batches: the first call fails and nothing else starts.
        assert len(analyzer.calls) == 1
        # Nothing was checkpointed, so every review is retried next run.
        assert (tmp_path / "ckpt.jsonl").read_text(encoding="utf-8") == ""

    def test_non_fatal_error_only_fails_its_batch(self):
        analyzer = FakeAnalyzer(ProviderError("prompt is too long"))
        results = self._pipeline(analyzer).process_all_reviews(_reviews(4), resume=False)
        assert len(results) == 4
        assert all(r["analysis"] is None for r in results)


class _FakeConn:
    def cursor(self):
        return SimpleNamespace(execute=lambda *a: None)

    def commit(self):
        pass

    def close(self):
        pass


class TestTranslation:
    def test_translate_one_uses_complete_json(self):
        analyzer = FakeAnalyzer()
        assert translation.translate_one(analyzer, "bonjour", "French") == "hello"
        assert len(analyzer.calls) == 1 and "bonjour" in analyzer.calls[0]

    def test_fatal_error_stops_the_translation_pass(self, monkeypatch):
        rows = [{"uid": f"u{i}", "review_text": "bonjour"} for i in range(3)]
        monkeypatch.setattr(translation, "ensure_translation_columns", lambda conn: None)
        monkeypatch.setattr(translation, "fetch_untranslated", lambda conn: rows)
        monkeypatch.setattr(translation, "detect_language", lambda text: "fr")
        writer = SimpleNamespace(_connect=_FakeConn)
        analyzer = FakeAnalyzer(FatalProviderError("credit balance is too low"))
        with pytest.raises(FatalProviderError):
            translation.run_translation_pass(analyzer, writer)
        assert len(analyzer.calls) == 1
