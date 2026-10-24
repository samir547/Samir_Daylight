"""SQL Server access, split by responsibility (ISSUE-05) with a real public
API (ISSUE-07):

    connection.py         connect() + the idempotent schema-migration registry
    review_repository.py  ReviewRepository -- review-row upserts and the
                          lookup of uids that already have an analysis
    run_repository.py     RunRepository -- run-summary upserts
    action_item_repository.py
                          ActionItemRepository -- reads analysed reviews from
                          dbo.vw_review_analysis, saves business to-do items

Standalone scripts should import from here instead of reaching into
"private" methods on the old DatabaseWriter (which now just delegates).
"""
from daylight_sentiment.infra.db.connection import (  # noqa: F401
    ACTION_ITEMS_TABLE,
    REVIEWS_TABLE,
    RUNS_TABLE,
    connect,
    ensure_schema,
)
from daylight_sentiment.infra.db.review_repository import (  # noqa: F401
    ExistingReviewsReadError,
    ReviewRepository,
)
from daylight_sentiment.infra.db.run_repository import RunRepository  # noqa: F401
from daylight_sentiment.infra.db.action_item_repository import ActionItemRepository  # noqa: F401
