# migrations/

Genuine one-time, non-re-runnable changes (e.g. the `model`/`run_date` column additions to `forecast_output`/`forecast_accuracy`, or a future `DROP TABLE` before a schema change — see `queries.py`'s comment: *"Drop dbo.forecast_output / dbo.forecast_accuracy once before the first item-grain --write-db run so the new schema takes effect."*). Numbered/dated filenames, applied once, never re-run. Empty for now — nothing to backfill here retroactively; starts tracking from the next real schema change forward.
