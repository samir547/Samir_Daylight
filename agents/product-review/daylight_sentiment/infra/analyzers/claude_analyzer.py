"""Anthropic-backed analyzer."""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from daylight_sentiment.config import Config
from daylight_sentiment.domain.normalise import _normalise, _strip_fences
from daylight_sentiment.domain.prompts import (
    PRODUCT_PROMPT,
    SERVICE_PROMPT,
    _build_context,
)
from daylight_sentiment.infra.analyzers.base import SentimentAnalyzer


class ClaudeSentimentAnalyzer(SentimentAnalyzer):
    """Anthropic-backed analyzer (higher accuracy, higher cost)."""

    def __init__(self, config: Config):
        import anthropic
        if not config.claude_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set (add it to .env)")
        self.config = config
        # The SDK already retries 429/5xx with exponential backoff.
        self.client = anthropic.Anthropic(
            api_key=config.claude_api_key,
            timeout=float(config.request_timeout),
            max_retries=config.max_retries,
        )

    def _call(self, prompt: str) -> Dict[str, Any]:
        resp = self.client.messages.create(
            model=self.config.claude_model,
            max_tokens=500,
            system="You are a sentiment analysis expert. Respond with valid JSON only.",
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        # strict=False: model output (esp. translations) can carry raw control
        # characters inside JSON string values, which the default strict decoder
        # rejects with "Invalid control character".
        return json.loads(_strip_fences(text), strict=False)

    def analyze_product_review(self, text: str, product_name: Optional[str] = None,
                               rating: Optional[int] = None) -> Dict[str, Any]:
        context = _build_context(product_name, rating)
        raw = self._call(PRODUCT_PROMPT.format(context=context, text=text))
        return _normalise(raw, "product")

    def analyze_service_review(self, text: str, rating: Optional[int] = None) -> Dict[str, Any]:
        context = _build_context(None, rating)
        raw = self._call(SERVICE_PROMPT.format(context=context, text=text))
        return _normalise(raw, "service")
