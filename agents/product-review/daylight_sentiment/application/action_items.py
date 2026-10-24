"""The top 3 to-do items for the business, generated from the stored
sentiment results: fetch the analysed reviews, then for each review window
("Last 3 months", "Last 6 months", "Last 12 months" by default) build that
window's summary, make one model call, validate the reply (retrying once,
with the reasons, if it fails) and save that window's 3 items.

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
    ActionItemsValidationError,
    ReviewWindow,
    build_summary,
    select_windows,
    summary_areas,
    validate_action_items,
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
    """What happened to one review window: skipped (too few reviews),
    failed (``error`` set, nothing saved) or generated (``items``)."""
    window: ReviewWindow
    items: List[Dict[str, Any]] = field(default_factory=list)
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
    prompt = build_action_items_prompt(summary, summary_areas(summary))
    reply = analyzer.complete_json(prompt, max_tokens=ACTION_ITEMS_MAX_TOKENS)
    try:
        return validate_action_items(reply, summary)
    except ActionItemsValidationError as exc:
        logger.warning("Action items for %s: the reply failed validation, retrying once: %s",
                       window.label, exc)
        reply = analyzer.complete_json(build_retry_prompt(prompt, str(exc)),
                                       max_tokens=ACTION_ITEMS_MAX_TOKENS)
        try:
            return validate_action_items(reply, summary)
        except ActionItemsValidationError as retry_exc:
            logger.warning("Action items for %s: the retry failed validation too: %s",
                           window.label, retry_exc)
            raise


def generate_action_items(analyzer: SentimentAnalyzer, repository: Any,
                          settings: ActionItemSettings, llm_provider: str,
                          llm_model: str, run_timestamp: Optional[str] = None,
                          dry_run: bool = False,
                          today: Optional[date] = None) -> ActionItemsOutcome:
    """Generate (and, unless ``dry_run``, save) one set of to-do items per
    review window in ``settings.windows_months``.

    A window with too few reviews is skipped with a WARNING. A window that
    fails (a reply that fails validation twice, a provider or database
    error) is recorded in its WindowOutcome with nothing saved, and the
    other windows still go ahead. A FatalProviderError stops every window,
    since each call would fail the same way. Fetching the reviews is done
    once; if it fails the error propagates.
    """
    rows = repository.fetch_analysed_rows()
    windows = select_windows(rows, settings.windows_months, today=today)
    first = windows[0] if windows else None
    if first is not None and first.capped_from:
        logger.warning("Action items: the newest review date (%s) is after today, so the "
                       "windows end today (%s) instead; %d review(s) dated after today "
                       "are kept in them", first.capped_from.isoformat(),
                       first.end.isoformat(), first.future_dated)
    if first is not None and first.undated_excluded:
        logger.info("Action items: %d analysed review(s) have no parsed review date "
                    "and are left out", first.undated_excluded)

    outcome = ActionItemsOutcome()
    for window in windows:
        result = WindowOutcome(window)
        outcome.windows.append(result)
        if window.skipped:
            logger.warning("Action items for %s: skipped -- %s", window.label,
                           window.skip_reason)
            continue
        logger.info("Action items for %s: %s", window.label, window.basis())
        try:
            result.items = generate_window_items(analyzer, window, settings)
            if dry_run:
                logger.info("Action items for %s: dry run, nothing written to the "
                            "database", window.label)
                continue
            result.generation_id = repository.save(
                result.items, window.label, window.months, window.start, window.end,
                window.review_count, llm_provider, llm_model, run_timestamp)
        except FatalProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 -- one window's failure spares the rest
            result.items = []
            result.error = exc
    return outcome
