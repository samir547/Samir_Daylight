"""Pluggable review sources (ISSUE-02).

Each source implements the ReviewSource protocol (base.py): a ``name`` and a
``fetch() -> list[dict]`` returning reviews in the pipeline's normalised
shape. The orchestrator iterates ``build_default_sources()`` instead of
calling three hardcoded loader methods, so a new source (e.g. whatever
replaces Bright Data -- ISSUE-03) is a new class here, not a parallel script.

NOT yet included: amazon_adhoc.py (the Varun-batch pattern). Its only
consumer and test data are deliberately held back as a post-refactor
integration fixture, so formalizing it waits for that work.
"""
from daylight_sentiment.infra.review_sources.base import (  # noqa: F401
    ReviewSource,
    load_reviews,
    resolve_source_file,
)
from daylight_sentiment.infra.review_sources.amazon_brightdata import (  # noqa: F401
    AmazonBrightDataSource,
)
from daylight_sentiment.infra.review_sources.trustpilot import (  # noqa: F401
    TrustpilotProductSource,
    TrustpilotServiceSource,
)


def build_default_sources(sources_config):
    """The standard three production sources, in loading order."""
    return [
        TrustpilotProductSource(sources_config),
        TrustpilotServiceSource(sources_config),
        AmazonBrightDataSource(sources_config),
    ]
