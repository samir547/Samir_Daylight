"""Per-call output-token limits: translation sizes its limit to the text it
sends, and a reply cut off at the limit raises ResponseTruncatedError, which
fails only that review / row (clients are stubbed -- no API or DB calls)."""
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from daylight_sentiment.application.pipeline import MultiSourcePipeline
from daylight_sentiment.config import Config
from daylight_sentiment.infra import translation
from daylight_sentiment.infra.analyzers import (
    ClaudeSentimentAnalyzer,
    FatalProviderError,
    ResponseTruncatedError,
    SentimentAnalyzer,
)
from daylight_sentiment.infra.analyzers.claude_analyzer import DEFAULT_MAX_TOKENS


class TestTranslationMaxTokens:
    def test_floor_for_short_text(self):
        assert translation.translation_max_tokens("") == translation.TRANSLATION_MIN_TOKENS
        assert translation.translation_max_tokens("Très bien") == 500

    def test_scales_with_text_length(self):
        assert translation.translation_max_tokens("x" * 2000) == 2000 // 2 + 200

    def test_full_length_review_fits_under_ceiling(self):
        assert translation.translation_max_tokens("x" * 6000) == 3200

    def test_ceiling(self):
        assert translation.translation_max_tokens("x" * 50_000) == translation.TRANSLATION_MAX_TOKENS


def _claude(stop_reason: str = "end_turn", text: str = '{"ok": true}'):
    analyzer = ClaudeSentimentAnalyzer.__new__(ClaudeSentimentAnalyzer)
    analyzer.config = SimpleNamespace(claude_model="stub")
    calls: List[Dict[str, Any]] = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(stop_reason=stop_reason,
                               content=[SimpleNamespace(type="text", text=text)])

    analyzer.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    return analyzer, calls


class TestClaudeCompleteJson:
    def test_default_limit(self):
        analyzer, calls = _claude()
        assert analyzer.complete_json("p") == {"ok": True}
        assert calls[0]["max_tokens"] == DEFAULT_MAX_TOKENS == 500

    def test_explicit_limit_reaches_the_sdk(self):
        analyzer, calls = _claude()
        analyzer.complete_json("p", max_tokens=3200)
        assert calls[0]["max_tokens"] == 3200

    def test_sentiment_calls_keep_the_default(self):
        text = ('{"sentiment": "positive", "confidence": 0.9, '
                '"primary_sector": "craft", "key_themes": []}')
        analyzer, calls = _claude(text=text)
        analyzer.analyze_product_review("lovely kit")
        analyzer.analyze_service_review("fast delivery")
        assert [c["max_tokens"] for c in calls] == [500, 500]

    def test_truncated_reply_raises_specific_error(self):
        analyzer, _ = _claude(stop_reason="max_tokens", text='{"translation": "half a sen')
        with pytest.raises(ResponseTruncatedError, match=r"max_tokens=1234"):
            analyzer.complete_json("p", max_tokens=1234)

    def test_truncation_is_not_fatal(self):
        assert not issubclass(ResponseTruncatedError, FatalProviderError)


class FakeAnalyzer(SentimentAnalyzer):
    """Raises ResponseTruncatedError for prompts containing 'long'."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def complete_json(self, prompt, max_tokens=None):
        self.calls.append({"prompt": prompt, "max_tokens": max_tokens})
        if "long" in prompt:
            raise ResponseTruncatedError(f"hit the limit (max_tokens={max_tokens})")
        return {"translation": "hello", "sentiment": "positive"}

    def analyze_product_review(self, text, product_name=None, rating=None):
        return self.complete_json(text)

    def analyze_service_review(self, text, rating=None):
        return self.complete_json(text)


def test_translate_one_passes_computed_limit():
    analyzer = FakeAnalyzer()
    text = "x" * 9000
    translation.translate_one(analyzer, text, "French", max_chars=6000)
    # Sized from the truncated text actually sent, not the original.
    assert analyzer.calls[0]["max_tokens"] == translation.translation_max_tokens("x" * 6000)


class _FakeConn:
    def __init__(self, updates):
        self.updates = updates

    def cursor(self):
        return SimpleNamespace(execute=lambda sql, *params: self.updates.append(params))

    def commit(self):
        pass

    def close(self):
        pass


def test_truncated_translation_fails_only_that_row(monkeypatch):
    rows = [{"uid": "a", "review_text": "long review"},
            {"uid": "b", "review_text": "short review"}]
    updates: List[tuple] = []
    monkeypatch.setattr(translation, "ensure_translation_columns", lambda conn: None)
    monkeypatch.setattr(translation, "fetch_untranslated", lambda conn: rows)
    monkeypatch.setattr(translation, "detect_language", lambda text: "fr")
    writer = SimpleNamespace(_connect=lambda: _FakeConn(updates))

    stats = translation.run_translation_pass(FakeAnalyzer(), writer)

    assert stats == {"matched": 2, "translated": 1, "failed": 1}
    assert [p[-1] for p in updates] == ["b"]  # only the second row was written


def test_truncated_review_fails_without_stopping_the_pipeline():
    config = Config()
    config.max_workers = 1
    config.batch_size = 1
    reviews = [{"uid": "a", "text": "long review", "review_type": "service"},
               {"uid": "b", "text": "short review", "review_type": "service"}]
    results = {r["uid"]: r for r in
               MultiSourcePipeline(config, FakeAnalyzer()).process_all_reviews(
                   reviews, resume=False)}
    assert results["a"]["analysis"] is None and results["a"]["error"]
    assert results["b"]["analysis"] is not None
