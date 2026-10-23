"""Anthropic-backed analyzer."""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

import anthropic

from daylight_sentiment.config import Config
from daylight_sentiment.domain.normalise import _normalise, _strip_fences
from daylight_sentiment.domain.prompts import (
    PRODUCT_PROMPT,
    SERVICE_PROMPT,
    _build_context,
)
from daylight_sentiment.infra.analyzers.base import SentimentAnalyzer
from daylight_sentiment.infra.analyzers.errors import (
    FatalProviderError,
    ProviderError,
    RateLimited,
    ResponseTruncatedError,
    TransientProviderError,
)

# Output-token cap for the short JSON sentiment replies; callers that expect
# longer output (translation) pass their own.
DEFAULT_MAX_TOKENS = 500

# 401 invalid key, 402 billing, 403 permission, 404 unknown model: every
# request in the run would fail the same way.
_FATAL_STATUSES = {401, 402, 403, 404}


def classify_anthropic_error(exc: anthropic.APIError) -> ProviderError:
    """Map an Anthropic SDK exception onto the typed hierarchy. Called only
    once the SDK's own retries (429/5xx/connection) are exhausted."""
    status = getattr(exc, "status_code", None)
    message = str(exc)
    if status in _FATAL_STATUSES:
        return FatalProviderError(message)
    # An exhausted credit balance has been reported as a 400 rather than a 402.
    if status == 400 and "credit balance" in message.lower():
        return FatalProviderError(message)
    if status == 429:
        return RateLimited(message)
    if status is None or status >= 500:
        return TransientProviderError(message)
    return ProviderError(message)


class ClaudeSentimentAnalyzer(SentimentAnalyzer):
    """Anthropic-backed analyzer."""

    def __init__(self, config: Config):
        if not config.claude_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set (add it to .env)")
        self.config = config
        # The SDK already retries 429/5xx with exponential backoff.
        self.client = anthropic.Anthropic(
            api_key=config.claude_api_key,
            timeout=float(config.request_timeout),
            max_retries=config.max_retries,
        )

    def complete_json(self, prompt: str, max_tokens: Optional[int] = None) -> Dict[str, Any]:
        limit = DEFAULT_MAX_TOKENS if max_tokens is None else max_tokens
        try:
            resp = self.client.messages.create(
                model=self.config.claude_model,
                max_tokens=limit,
                system="You are a sentiment analysis expert. Respond with valid JSON only.",
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIError as exc:
            raise classify_anthropic_error(exc) from exc
        if resp.stop_reason == "max_tokens":
            raise ResponseTruncatedError(
                f"Model response hit the output-token limit (max_tokens={limit}) "
                f"and was cut off before the JSON was complete")
        text = "".join(b.text for b in resp.content if b.type == "text")
        # strict=False: model output (esp. translations) can carry raw control
        # characters inside JSON string values, which the default strict decoder
        # rejects with "Invalid control character".
        return json.loads(_strip_fences(text), strict=False)

    def analyze_product_review(self, text: str, product_name: Optional[str] = None,
                               rating: Optional[int] = None) -> Dict[str, Any]:
        context = _build_context(product_name, rating)
        raw = self.complete_json(PRODUCT_PROMPT.format(context=context, text=text))
        return _normalise(raw, "product")

    def analyze_service_review(self, text: str, rating: Optional[int] = None) -> Dict[str, Any]:
        context = _build_context(None, rating)
        raw = self.complete_json(SERVICE_PROMPT.format(context=context, text=text))
        return _normalise(raw, "service")
