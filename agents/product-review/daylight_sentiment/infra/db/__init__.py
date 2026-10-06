"""SQL Server access, split by responsibility (ISSUE-05) with a real public
API (ISSUE-07):

    connection.py         connect() + the idempotent schema-migration registry
    review_repository.py  ReviewRepository -- review-row upserts
    run_repository.py     RunRepository -- run-summary upserts

Standalone scripts should import from here instead of reaching into
"private" methods on the old DatabaseWriter (which now just delegates).
"""
from daylight_sentiment.infra.db.connection import (  # noqa: F401
    REVIEWS_TABLE,
    RUNS_TABLE,
    connect,
    ensure_schema,
)
from daylight_sentiment.infra.db.review_repository import ReviewRepository  # noqa: F401
from daylight_sentiment.infra.db.run_repository import RunRepository  # noqa: F401
