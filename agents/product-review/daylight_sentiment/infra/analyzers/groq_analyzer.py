"""Groq-backed analyzer. The Groq-specific error-message parsing lives in
errors.py (ISSUE-09/10); the retry loop here branches on the typed hierarchy
instead of re-parsing strings inline."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from daylight_sentiment.config import Config
from daylight_sentiment.domain.normalise import _align_batch, _normalise
from daylight_sentiment.domain.prompts import (
    PRODUCT_PROMPT,
    SERVICE_PROMPT,
    _build_batch_prompt,
    _build_context,
)
from daylight_sentiment.infra.analyzers.base import SentimentAnalyzer
from daylight_sentiment.infra.analyzers.errors import (
    DailyCapExceeded,
    FatalProviderError,
    RateLimited,
    classify_groq_error,
)
# Legacy names -- prefer classify_groq_error() / the typed hierarchy.
from daylight_sentiment.infra.analyzers.errors import (  # noqa: F401
    _is_daily_cap,
    _retry_after_hint as _retry_after,
)
from daylight_sentiment.infra.analyzers.rate_limiter import TokenRateLimiter

logger = logging.getLogger("multi_source_sentiment")


class GroqSentimentAnalyzer(SentimentAnalyzer):
    """Groq-backed analyzer. Uses native JSON mode rather than fence-stripping."""

    def __init__(self, config: Config):
        from groq import Groq
        if not config.groq_api_key:
            raise RuntimeError("GROQ_API_KEY is not set (add it to .env)")
        self.config = config
        # The SDK's own retries bypass our limiter, so disable them and pace here.
        self.client = Groq(api_key=config.groq_api_key,
                           timeout=config.request_timeout, max_retries=0)
        self.limiter = TokenRateLimiter(config.groq_tpm_limit)

    def _call(self, prompt: str, n_items: int = 1) -> Dict[str, Any]:
        last: Optional[Exception] = None
        # ~4 chars/token for the prompt, plus ~55 output tokens per item and a
        # flat allowance for reasoning tokens.
        est = len(prompt) // 4 + n_items * 55 + 150
        max_out = min(400 + n_items * 90, 8000)
        attempt = 0
        tpd_waits = 0
        while attempt < self.config.max_retries:
            self.limiter.acquire(est)
            try:
                resp = self.client.chat.completions.create(
                    model=self.config.groq_model,
                    messages=[
                        {"role": "system", "content":
                         "You are a sentiment analysis expert. Always respond with valid JSON only."},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0,
                    max_tokens=max_out,
                    reasoning_effort=self.config.groq_reasoning_effort,
                    response_format={"type": "json_object"},
                )
                usage = getattr(resp, "usage", None)
                self.limiter.settle(est, getattr(usage, "total_tokens", est) or est)
                # strict=False: model output (esp. translations) can carry raw
                # control characters inside JSON string values, which the default
                # strict decoder rejects with "Invalid control character".
                return json.loads(resp.choices[0].message.content, strict=False)
            except Exception as exc:  # noqa: BLE001 - provider raises many shapes
                last = exc
                err = classify_groq_error(exc)
                # A rejected or failed-to-connect request generated no tokens, so
                # give the reservation back; otherwise the bucket drifts below the
                # real quota and throughput spirals down. (err.charged marks the
                # exception -- a 400 json_validate_failed -- where a generation
                # WAS burned server-side, so the reservation stands.)
                if err.charged:
                    self.limiter.settle(est, est)
                else:
                    self.limiter.refund(est)
                # A tokens-per-day rejection is not a transient error -- the quota
                # refills on a rolling window. Wait it out without consuming a
                # retry attempt, otherwise the batch is failed for a cap that
                # simply needed patience.
                if isinstance(err, DailyCapExceeded) and tpd_waits < self.config.max_tpd_waits:
                    tpd_waits += 1
                    delay = min(err.retry_after or 300.0, self.config.max_tpd_wait)
                    logger.info("Daily token cap reached; sleeping %.0fs "
                                "(wait %d/%d) then retrying",
                                delay, tpd_waits, self.config.max_tpd_waits)
                    time.sleep(delay)
                    continue

                if isinstance(err, FatalProviderError):
                    raise  # unrecoverable: bad model, bad key, malformed request

                attempt += 1
                delay = min(err.retry_after or (2 ** attempt), 120.0)
                if isinstance(err, (RateLimited, DailyCapExceeded)):
                    logger.info("Groq 429 (per-minute); backing off %.1fs", delay)
                else:
                    logger.warning("Groq call failed (attempt %d/%d): %s; sleeping %.1fs",
                                   attempt, self.config.max_retries, exc, delay)
                if attempt < self.config.max_retries:
                    time.sleep(delay)
        raise last  # type: ignore[misc]

    def analyze_product_review(self, text: str, product_name: Optional[str] = None,
                               rating: Optional[int] = None) -> Dict[str, Any]:
        context = _build_context(product_name, rating)
        raw = self._call(PRODUCT_PROMPT.format(context=context, text=text))
        return _normalise(raw, "product")

    def analyze_service_review(self, text: str, rating: Optional[int] = None) -> Dict[str, Any]:
        context = _build_context(None, rating)
        raw = self._call(SERVICE_PROMPT.format(context=context, text=text))
        return _normalise(raw, "service")

    def analyze_batch(self, reviews: List[Dict[str, Any]], review_type: str
                      ) -> List[Optional[Dict[str, Any]]]:
        if len(reviews) == 1:
            return [self._analyze_one(reviews[0], review_type)]

        prompt = _build_batch_prompt(reviews, review_type, self.config.max_review_chars)
        try:
            raw = self._call(prompt, n_items=len(reviews))
            out = _align_batch(raw, reviews, review_type)
        except Exception as exc:  # noqa: BLE001 - fall back to per-review calls
            logger.warning("Batch of %d failed (%s); retrying individually",
                           len(reviews), exc)
            out = [None] * len(reviews)

        # Retry only the slots the model dropped, one review at a time.
        missing = [i for i, v in enumerate(out) if v is None]
        if missing:
            logger.info("Re-running %d/%d dropped reviews individually",
                        len(missing), len(reviews))
            for i in missing:
                try:
                    out[i] = self._analyze_one(reviews[i], review_type)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Single retry failed for %s: %s",
                                 reviews[i].get("uid"), exc)
        return out
