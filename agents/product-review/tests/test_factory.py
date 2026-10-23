"""build_analyzer(): the provider registry is the extension point for adding
another LLM provider (no real API clients are constructed)."""
from enum import Enum

import pytest

from daylight_sentiment.config import Config, LLMProvider
from daylight_sentiment.infra.analyzers import ClaudeSentimentAnalyzer, SentimentAnalyzer
from daylight_sentiment.infra.analyzers import factory
from daylight_sentiment.infra.analyzers.factory import build_analyzer


class _OtherProvider(Enum):
    FAKE = "fake"


class FakeProviderAnalyzer(SentimentAnalyzer):
    def __init__(self, config):
        self.config = config

    def complete_json(self, prompt):
        return {"echo": prompt}

    def analyze_product_review(self, text, product_name=None, rating=None):
        return self.complete_json(text)

    def analyze_service_review(self, text, rating=None):
        return self.complete_json(text)


def test_every_provider_has_an_analyzer():
    assert set(factory._ANALYZERS) == set(LLMProvider)


def test_claude_is_built_by_default():
    config = Config()
    config.claude_api_key = "test-key"  # the client is constructed but never called
    assert isinstance(build_analyzer(config), ClaudeSentimentAnalyzer)


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError, match="No analyzer registered.*registered: claude"):
        build_analyzer(Config(llm_provider=_OtherProvider.FAKE))


def test_a_registered_provider_is_built(monkeypatch):
    monkeypatch.setitem(factory._ANALYZERS, _OtherProvider.FAKE, FakeProviderAnalyzer)
    config = Config(llm_provider=_OtherProvider.FAKE)
    analyzer = build_analyzer(config)
    assert isinstance(analyzer, FakeProviderAnalyzer)
    assert analyzer.config is config
    assert analyzer.analyze_batch(
        [{"text": "hi", "review_type": "service"}], "service") == [{"echo": "hi"}]


def test_complete_json_is_required_by_the_interface():
    class Incomplete(SentimentAnalyzer):
        def analyze_product_review(self, text, product_name=None, rating=None):
            return {}

        def analyze_service_review(self, text, rating=None):
            return {}

    with pytest.raises(TypeError, match="complete_json"):
        Incomplete()
