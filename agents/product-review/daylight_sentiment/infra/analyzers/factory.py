"""Analyzer construction from configuration."""
from __future__ import annotations

import logging
from typing import Callable, Dict

from daylight_sentiment.config import Config, LLMProvider
from daylight_sentiment.infra.analyzers.base import SentimentAnalyzer
from daylight_sentiment.infra.analyzers.claude_analyzer import ClaudeSentimentAnalyzer

logger = logging.getLogger("multi_source_sentiment")

# One entry per LLMProvider member. Adding a provider means adding its
# analyzer class here.
_ANALYZERS: Dict[LLMProvider, Callable[[Config], SentimentAnalyzer]] = {
    LLMProvider.CLAUDE: ClaudeSentimentAnalyzer,
}


def build_analyzer(config: Config) -> SentimentAnalyzer:
    try:
        analyzer_cls = _ANALYZERS[config.llm_provider]
    except KeyError:
        known = ", ".join(sorted(p.value for p in _ANALYZERS))
        raise ValueError(
            f"No analyzer registered for LLM provider {config.llm_provider!r} "
            f"(registered: {known})") from None
    logger.info("Using %s for provider %s",
                getattr(analyzer_cls, "__name__", analyzer_cls), config.llm_provider.value)
    return analyzer_cls(config)
