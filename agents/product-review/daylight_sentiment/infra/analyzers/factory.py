"""Analyzer construction from configuration."""
from __future__ import annotations

import logging

from daylight_sentiment.config import Config, LLMProvider
from daylight_sentiment.infra.analyzers.base import SentimentAnalyzer
from daylight_sentiment.infra.analyzers.claude_analyzer import ClaudeSentimentAnalyzer
from daylight_sentiment.infra.analyzers.groq_analyzer import GroqSentimentAnalyzer

logger = logging.getLogger("multi_source_sentiment")


def build_analyzer(config: Config) -> SentimentAnalyzer:
    if config.llm_provider is LLMProvider.GROQ:
        logger.info("Using Groq analyzer (%s)", config.groq_model)
        return GroqSentimentAnalyzer(config)
    logger.info("Using Claude analyzer (%s)", config.claude_model)
    return ClaudeSentimentAnalyzer(config)
