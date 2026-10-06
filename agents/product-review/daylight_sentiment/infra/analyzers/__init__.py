"""LLM sentiment analyzers (ISSUE-09): the provider-agnostic base and rate
limiter live apart from the provider-specific implementations, so adding a
third provider touches nothing generic.

    base.py             SentimentAnalyzer ABC
    rate_limiter.py     TokenRateLimiter (Groq TPM pacing)
    errors.py           typed provider-error hierarchy (ISSUE-10)
    groq_analyzer.py    GroqSentimentAnalyzer
    claude_analyzer.py  ClaudeSentimentAnalyzer
    factory.py          build_analyzer()
"""
from daylight_sentiment.infra.analyzers.base import SentimentAnalyzer  # noqa: F401
from daylight_sentiment.infra.analyzers.errors import (  # noqa: F401
    DailyCapExceeded,
    FatalProviderError,
    ProviderError,
    RateLimited,
    TransientProviderError,
    classify_groq_error,
)
from daylight_sentiment.infra.analyzers.claude_analyzer import ClaudeSentimentAnalyzer  # noqa: F401
from daylight_sentiment.infra.analyzers.factory import build_analyzer  # noqa: F401
from daylight_sentiment.infra.analyzers.groq_analyzer import GroqSentimentAnalyzer  # noqa: F401
from daylight_sentiment.infra.analyzers.rate_limiter import TokenRateLimiter  # noqa: F401
