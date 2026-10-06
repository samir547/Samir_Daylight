"""Typed provider-error hierarchy (ISSUE-10).

Before this, the retry loop's only mechanism for telling "wait it out" from
"fail now" was string-matching the provider's error message. The strings are
still parsed -- Groq puts retry hints in message bodies -- but the parsing
happens in ONE place (classify_groq_error), which emits a typed error the
retry logic branches on by isinstance. If Groq rewords a message, the fix is
one classifier, not a hunt through control flow.
"""
from __future__ import annotations

import re
from typing import Optional


class ProviderError(Exception):
    """Base for classified provider failures.

    ``retry_after``: the provider's suggested wait in seconds, if it gave one.
    ``charged``: whether the failed call still consumed generation tokens
    server-side (True for a 400 json_validate_failed, where a full generation
    was burned) -- decides settle vs refund on the rate-limiter reservation.
    """

    def __init__(self, message: str, retry_after: Optional[float] = None,
                 charged: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.charged = charged


class DailyCapExceeded(ProviderError):
    """Tokens-per-day cap: not a transient error -- the quota refills on a
    rolling window, so the right response is patience, not failure."""


class RateLimited(ProviderError):
    """Per-minute (TPM) rejection: back off briefly and retry."""


class TransientProviderError(ProviderError):
    """5xx / connection-level failure: retry with backoff."""


class FatalProviderError(ProviderError):
    """4xx that retrying can't fix: bad model, bad key, malformed request."""


def _retry_after_hint(exc: Exception) -> float:
    """Pull a Retry-After hint off a provider exception, if it carries one."""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if headers:
        try:
            value = float(headers.get("retry-after") or 0)
            if value:
                return value
        except (TypeError, ValueError):
            pass
    # Groq omits the header on some rejections but states the wait in the body,
    # in h/m/s form: "Please try again in 6m56.448s" / "in 4h26m24s" / "in 2.91s".
    match = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?([\d.]+)s", str(exc))
    if match:
        hours, minutes, seconds = match.groups()
        return (int(hours or 0) * 3600 + int(minutes or 0) * 60
                + float(seconds) + 0.25)
    return 0.0


def _is_daily_cap(exc: Exception) -> bool:
    """True for a tokens-per-day rejection, as opposed to a per-minute one."""
    text = str(exc).lower()
    return "tokens per day" in text or "(tpd)" in text


def classify_groq_error(exc: Exception) -> ProviderError:
    """Map a raw Groq/SDK exception onto the typed hierarchy.

    The message-string heuristics from the monolith live here and only here.
    """
    status = getattr(exc, "status_code", None)
    retry_after = _retry_after_hint(exc) or None
    message = str(exc)
    if _is_daily_cap(exc):
        return DailyCapExceeded(message, retry_after=retry_after)
    if status == 429:
        return RateLimited(message, retry_after=retry_after)
    if status is not None and status < 500:
        # 400 json_validate_failed still burned a full generation server-side.
        return FatalProviderError(message, retry_after=retry_after,
                                  charged=(status == 400))
    return TransientProviderError(message, retry_after=retry_after)
