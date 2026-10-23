"""LLM sentiment analyzers (ISSUE-09): the provider-agnostic interface and
error hierarchy live apart from the provider-specific implementations, so
adding another provider touches nothing generic.

    base.py             SentimentAnalyzer ABC
    errors.py           typed provider-error hierarchy (ISSUE-10)
    claude_analyzer.py  ClaudeSentimentAnalyzer
    factory.py          build_analyzer() and the provider registry
"""
from daylight_sentiment.infra.analyzers.base import SentimentAnalyzer  # noqa: F401
from daylight_sentiment.infra.analyzers.errors import (  # noqa: F401
    FatalProviderError,
    ProviderError,
    RateLimited,
    ResponseTruncatedError,
    TransientProviderError,
)
from daylight_sentiment.infra.analyzers.claude_analyzer import ClaudeSentimentAnalyzer  # noqa: F401
from daylight_sentiment.infra.analyzers.factory import build_analyzer  # noqa: F401
