"""ISSUE-10: typed provider-error classification and the Groq retry loop
branching on it (stubbed clients, no real API)."""
from types import SimpleNamespace

import pytest

from daylight_sentiment.infra.analyzers import TokenRateLimiter
from daylight_sentiment.infra.analyzers.errors import (
    DailyCapExceeded,
    FatalProviderError,
    RateLimited,
    TransientProviderError,
    classify_groq_error,
)
from daylight_sentiment.infra.analyzers.groq_analyzer import GroqSentimentAnalyzer


class FakeApiError(Exception):
    def __init__(self, message, status_code=None, headers=None):
        super().__init__(message)
        self.status_code = status_code
        if headers is not None:
            self.response = SimpleNamespace(headers=headers)


class TestClassification:
    def test_daily_cap_from_message_text(self):
        err = classify_groq_error(FakeApiError(
            "Rate limit reached ... tokens per day (TPD) ... "
            "Please try again in 4h26m24s", status_code=429))
        assert isinstance(err, DailyCapExceeded)
        assert err.retry_after == pytest.approx(4 * 3600 + 26 * 60 + 24 + 0.25)

    def test_per_minute_429_with_body_hint(self):
        err = classify_groq_error(FakeApiError(
            "Rate limit ... Please try again in 2.91s", status_code=429))
        assert isinstance(err, RateLimited)
        assert err.retry_after == pytest.approx(2.91 + 0.25)

    def test_retry_after_header_preferred(self):
        err = classify_groq_error(FakeApiError(
            "429", status_code=429, headers={"retry-after": "7"}))
        assert err.retry_after == 7.0

    def test_400_is_fatal_and_charged(self):
        err = classify_groq_error(FakeApiError("json_validate_failed", status_code=400))
        assert isinstance(err, FatalProviderError)
        assert err.charged is True

    def test_401_is_fatal_not_charged(self):
        err = classify_groq_error(FakeApiError("invalid api key", status_code=401))
        assert isinstance(err, FatalProviderError)
        assert err.charged is False

    def test_5xx_and_connection_errors_transient(self):
        assert isinstance(classify_groq_error(FakeApiError("boom", status_code=503)),
                          TransientProviderError)
        assert isinstance(classify_groq_error(ConnectionError("reset")),
                          TransientProviderError)


def _analyzer(create):
    analyzer = GroqSentimentAnalyzer.__new__(GroqSentimentAnalyzer)
    analyzer.config = SimpleNamespace(
        groq_model="stub", groq_reasoning_effort="low", max_retries=3,
        max_tpd_wait=0.01, max_tpd_waits=2)
    analyzer.limiter = TokenRateLimiter(10_000_000)
    analyzer.client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    return analyzer


def _ok_response(content='{"ok": true}'):
    return SimpleNamespace(
        usage=SimpleNamespace(total_tokens=100),
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class TestRetryLoop:
    def test_fatal_raises_original_exception_immediately(self):
        calls = []

        def create(**kwargs):
            calls.append(1)
            raise FakeApiError("invalid api key", status_code=401)

        with pytest.raises(FakeApiError):
            _analyzer(create)._call("p")
        assert len(calls) == 1  # no retries on fatal

    def test_transient_retries_then_succeeds(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)
        calls = []

        def create(**kwargs):
            calls.append(1)
            if len(calls) < 3:
                raise FakeApiError("bad gateway", status_code=502)
            return _ok_response()

        assert _analyzer(create)._call("p") == {"ok": True}
        assert len(calls) == 3

    def test_daily_cap_waits_without_consuming_retries(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
        calls = []

        def create(**kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise FakeApiError("... tokens per day (TPD) ... try again in 1.0s",
                                   status_code=429)
            return _ok_response()

        assert _analyzer(create)._call("p") == {"ok": True}
        # Waited (capped at max_tpd_wait=0.01) and retried without burning an attempt.
        assert sleeps == [0.01]
