"""The top 3 to-do items for the business, generated from the stored
sentiment results: fetch the analysed reviews once, then for each review
scope (all reviews, product reviews, Amazon, Trustpilot, Trustpilot product,
Trustpilot service by default) and each review window ("Last 3 months",
"Last 6 months", "Last 12 months" by default) build that scope and window's
summary, make one model call, validate the reply (retrying once, with the
reasons, if it fails) and save its 3 items. A scope and window with too few
reviews gets one placeholder row instead, saying so.

The model call goes through the provider-neutral
SentimentAnalyzer.complete_json(); nothing here depends on which provider
produced the analyzer.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

from daylight_sentiment.config import ActionItemSettings
from daylight_sentiment.domain.action_items import (
    SCOPES_BY_KEY,
    ActionItemsValidationError,
    ReviewWindow,
    build_summary,
    placeholder_item,
    scope_rows,
    select_windows,
    summary_areas,
    validate_action_items,
    with_period,
)
from daylight_sentiment.domain.action_items_prompt import (
    build_action_items_prompt,
    build_retry_prompt,
)
from daylight_sentiment.infra.analyzers.base import SentimentAnalyzer
from daylight_sentiment.infra.analyzers.errors import FatalProviderError

logger = logging.getLogger("multi_source_sentiment")

# Output-token cap for each call: 3 items of a few sentences each, with room
# to spare.
ACTION_ITEMS_MAX_TOKENS = 1500


@dataclass
class WindowOutcome:
    """What happened to one review window of one scope (``window.scope``):
    skipped for too few reviews (``placeholder`` is the row that says so),
    failed (``error`` set, nothing saved) or generated (``items``)."""
    window: ReviewWindow
    items: List[Dict[str, Any]] = field(default_factory=list)
    placeholder: Optional[Dict[str, Any]] = None
    generation_id: Optional[str] = None
    error: Optional[Exception] = None

    @property
    def skipped(self) -> bool:
        return self.window.skipped

    @property
    def failed(self) -> bool:
        return self.error is not None


@dataclass
class ActionItemsOutcome:
    """One WindowOutcome per scope and window, scope by scope."""
    windows: List[WindowOutcome] = field(default_factory=list)

    @property
    def failed(self) -> List[WindowOutcome]:
        return [w for w in self.windows if w.failed]

    @property
    def generated(self) -> List[WindowOutcome]:
        return [w for w in self.windows if w.items]


def generate_window_items(analyzer: SentimentAnalyzer, window: ReviewWindow,
                          settings: ActionItemSettings) -> List[Dict[str, Any]]:
    """One window's items: its own summary, one call, validation against
    that summary, and ONE retry with the reasons if the reply fails. A
    provider error, or a retry that fails validation too
    (ActionItemsValidationError), propagates."""
    summary = build_summary(window, settings.top_themes, settings.max_excerpts,
                            settings.excerpt_chars)
    prompt = build_action_items_prompt(summary, summary_areas(summary), window.scope)
    reply = analyzer.complete_json(prompt, max_tokens=ACTION_ITEMS_MAX_TOKENS)
    try:
        return validate_action_items(reply, summary)
    except ActionItemsValidationError as exc:
        logger.warning("Action items for %s: the reply failed validation, retrying once: %s",
                       window.name, exc)
        reply = analyzer.complete_json(build_retry_prompt(prompt, str(exc)),
                                       max_tokens=ACTION_ITEMS_MAX_TOKENS)
        try:
            return validate_action_items(reply, summary)
        except ActionItemsValidationError as retry_exc:
            logger.warning("Action items for %s: the retry failed validation too: %s",
                           window.name, retry_exc)
            raise


def _save(repository: Any, items: List[Dict[str, Any]], window: ReviewWindow,
          llm_provider: Optional[str], llm_model: Optional[str],
          run_timestamp: Optional[str], is_placeholder: bool) -> str:
    return repository.save(
        items, window.label, window.months, window.start, window.end,
        window.review_count, llm_provider, llm_model, run_timestamp,
        source_scope=window.scope.source_scope,
        review_type_scope=window.scope.review_type_scope,
        is_placeholder=is_placeholder)


def generate_action_items(analyzer: SentimentAnalyzer, repository: Any,
                          settings: ActionItemSettings, llm_provider: str,
                          llm_model: str, run_timestamp: Optional[str] = None,
                          dry_run: bool = False,
                          today: Optional[date] = None) -> ActionItemsOutcome:
    """Generate (and, unless ``dry_run``, save) one set of to-do items per
    review scope in ``settings.scopes`` and review window in
    ``settings.windows_months``.

    The reviews are fetched once; if that fails the error propagates. Each
    scope's rows are then picked out of them, and its windows end on that
    scope's own newest review date. The "all" scope needs
    ``settings.min_reviews`` reviews in a window, every other scope
    ``settings.min_reviews_scoped``.

    A window with too few reviews is skipped with a WARNING and ONE
    placeholder row is saved for it (not in a dry run), so the report shows
    why there are no items. It is saved under the window's own dates; a
    scope with no dated reviews at all has none, so its placeholders use
    the windows ending on the newest review date of all the data (or
    today, if there is none).

    A scope and window that fails (a reply that fails validation twice, a
    provider or database error) is recorded in its WindowOutcome with
    nothing saved, and the others still go ahead. A FatalProviderError stops
    everything, since each call would fail the same way.
    """
    today = today or date.today()
    rows = repository.fetch_analysed_rows()

    # All the data, whatever the scopes: for the two log lines below and
    # for the end date a scope with no dated reviews falls back on.
    overall = select_windows(rows, settings.windows_months, today=today,
                             min_reviews=settings.min_reviews)
    first = overall[0] if overall else None
    if first is not None and first.capped_from:
        logger.warning("Action items: the newest review date (%s) is after today, so the "
                       "windows end today (%s) instead in every scope that has such a "
                       "review; %d review(s) dated after today are kept in them",
                       first.capped_from.isoformat(), first.end.isoformat(),
                       first.future_dated)
    if first is not None and first.undated_excluded:
        logger.info("Action items: %d analysed review(s) have no parsed review date "
                    "and are left out", first.undated_excluded)
    fallback_end = first.end if first is not None and first.end is not None else today

    outcome = ActionItemsOutcome()
    for key in settings.scopes:
        scope = SCOPES_BY_KEY[key]
        windows = select_windows(scope_rows(rows, scope), settings.windows_months,
                                 today=today, min_reviews=settings.min_reviews_for(key),
                                 scope=scope)
        for window in windows:
            if window.skipped:
                window = with_period(window, fallback_end)
            result = WindowOutcome(window)
            outcome.windows.append(result)
            try:
                if window.skipped:
                    logger.warning("Action items for %s: skipped -- %s", window.name,
                                   window.skip_reason)
                    result.placeholder = placeholder_item(window)
                    if not dry_run:
                        # No model wrote this row, so it names none.
                        result.generation_id = _save(
                            repository, [result.placeholder], window, None, None,
                            run_timestamp, is_placeholder=True)
                    continue
                logger.info("Action items for %s: %s", window.name, window.basis())
                result.items = generate_window_items(analyzer, window, settings)
                if dry_run:
                    logger.info("Action items for %s: dry run, nothing written to the "
                                "database", window.name)
                    continue
                result.generation_id = _save(
                    repository, result.items, window, llm_provider, llm_model,
                    run_timestamp, is_placeholder=False)
            except FatalProviderError:
                raise
            except Exception as exc:  # noqa: BLE001 -- one failure spares the rest
                result.items = []
                result.error = exc
    return outcome
