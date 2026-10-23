"""Typed provider-error hierarchy (ISSUE-10).

Callers decide what to do with a failed LLM call by isinstance on these
classes, never by string-matching a provider's error message. Each provider
module owns one classifier that maps its SDK's exceptions onto this
hierarchy (for Claude, classify_anthropic_error in claude_analyzer.py), so
if a provider rewords a message or changes a status code, the fix is that
one classifier, not a hunt through control flow.

    FatalProviderError      every further call would fail the same way (bad
                            API key, no credit, unknown model): stop the run
    RateLimited             rate limit still hit after the SDK's own retries
    TransientProviderError  5xx / connection failure after the SDK's retries
    ProviderError           any other failure specific to this one request
"""
from __future__ import annotations


class ProviderError(Exception):
    """Base for classified provider failures. Raised as-is for a failure that
    affects only the request that caused it (e.g. a malformed request)."""


class RateLimited(ProviderError):
    """Rate limit hit and not cleared by the provider SDK's own retries."""


class TransientProviderError(ProviderError):
    """5xx / connection-level failure not cleared by the SDK's own retries."""


class FatalProviderError(ProviderError):
    """Retrying can't fix it and every other request would fail the same way:
    invalid API key, exhausted credit / billing problem, missing permission,
    unknown model. The pipeline and the translation pass stop on this rather
    than marking every remaining review failed."""
