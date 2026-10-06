# migrations/

One-time, already-applied structural changes to tables that were already
live with data at the time the change was made -- e.g. an `ALTER TABLE ...
ADD COLUMN`. Unlike everything in `../tables/`, `../seed/`, and `../views/`,
these are **not safe to re-run** and should never be edited after they've
been applied -- add a new numbered file for a further change instead of
modifying an old one.

## Currently empty

No files here yet. The one known candidate for this folder is the
`model_number`/`review_country` `ALTER TABLE` that already ran against
`dbo.sentiment_analysis_reviews` (see `_ensure_schema()` in
`multi_source_sentiment_pipeline.py`) -- that change happened via the Python
pipeline, not a SQL script, so there's no file to move here. Worth adding
`0001_add_model_number_review_country_to_sentiment_analysis_reviews.sql` as
a documented record of that change, even though it's already been applied,
so this folder reflects the database's complete structural history in one
place.

## Naming convention for when a real migration is added

`NNNN_short_description.sql`, zero-padded, strictly increasing, one change
per file. The number records order of application, not intent -- don't
renumber existing files to make room for one you forgot.
