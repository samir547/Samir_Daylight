"""The top 3 to-do items for the business, generated from the stored
sentiment results: fetch the analysed reviews, pick the review window, build
the summary, make one model call, validate the reply (retrying once, with
the reasons, if it fails), save the 3 items.

The model call goes through the provider-neutral
SentimentAnalyzer.complete_json(); nothing here depends on which provider
produced the analyzer.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from daylight_sentiment.config import ActionItemSettings
from daylight_sentiment.domain.action_items import (
    ActionItemsValidationError,
    ReviewWindow,
    build_summary,
    select_window,
    summary_areas,
    validate_action_items,
)
from daylight_sentiment.domain.action_items_prompt import (
    build_action_items_prompt,
    build_retry_prompt,
)
from daylight_sentiment.infra.analyzers.base import SentimentAnalyzer

logger = logging.getLogger("multi_source_sentiment")

# Output-token cap for the one call: 3 items of a few sentences each, with
# room to spare.
ACTION_ITEMS_MAX_TOKENS = 1500


@dataclass
class ActionItemsOutcome:
    window: ReviewWindow
    items: List[Dict[str, Any]] = field(default_factory=list)
    generation_id: Optional[str] = None

    @property
    def skipped(self) -> bool:
        return self.window.skipped


def generate_action_items(analyzer: SentimentAnalyzer, repository: Any,
                          settings: ActionItemSettings, llm_provider: str,
                          llm_model: str, run_timestamp: Optional[str] = None,
                          dry_run: bool = False) -> ActionItemsOutcome:
    """Generate (and, unless ``dry_run``, save) one set of to-do items.

    Returns an outcome with no items when there are too few reviews to base
    them on. A reply that fails validation is retried ONCE, with the
    reasons; a provider error, or a retry that fails validation too
    (ActionItemsValidationError), propagates and nothing is saved.
    """
    rows = repository.fetch_analysed_rows()
    window = select_window(rows, settings.window_months, settings.min_window_reviews,
                           settings.min_total_reviews)
    if window.capped_from:
        logger.warning("Action items: the newest review date (%s) is after today, so the "
                       "window ends today (%s) instead; %d review(s) dated after today "
                       "are kept in it", window.capped_from.isoformat(),
                       window.end.isoformat(), window.future_dated)
    if window.undated_excluded:
        logger.info("Action items: %d analysed review(s) have no parsed review date "
                    "and are left out", window.undated_excluded)
    if window.skipped:
        logger.warning("Action items: not generated -- %s", window.skip_reason)
        return ActionItemsOutcome(window)
    if window.widened:
        logger.info("Action items: fewer than %d reviews in the last %d months; "
                    "using all %d dated reviews instead",
                    settings.min_window_reviews, settings.window_months, window.review_count)
    logger.info("Action items: %s", window.basis())

    summary = build_summary(window, settings.top_themes, settings.max_excerpts,
                            settings.excerpt_chars)
    prompt = build_action_items_prompt(summary, summary_areas(summary))
    reply = analyzer.complete_json(prompt, max_tokens=ACTION_ITEMS_MAX_TOKENS)
    try:
        items = validate_action_items(reply, summary)
    except ActionItemsValidationError as exc:
        logger.warning("Action items: the reply failed validation, retrying once: %s", exc)
        reply = analyzer.complete_json(build_retry_prompt(prompt, str(exc)),
                                       max_tokens=ACTION_ITEMS_MAX_TOKENS)
        try:
            items = validate_action_items(reply, summary)
        except ActionItemsValidationError as retry_exc:
            logger.warning("Action items: the retry failed validation too: %s", retry_exc)
            raise

    outcome = ActionItemsOutcome(window, items)
    if dry_run:
        logger.info("Action items: dry run, nothing written to the database")
        return outcome
    outcome.generation_id = repository.save(
        items, window.start, window.end, window.review_count,
        llm_provider, llm_model, run_timestamp)
    return outcome
