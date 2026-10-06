# SQL

Source-of-truth SQL for the Daylight review pipeline's database layer
(`DAYHANSA_SQL1`). This folder is what gets committed to the repo — the
canonical definition of every object, not a log of the steps used to get
there.

## Layout and convention

| Folder | Contains | Naming | Safe to re-run? |
|---|---|---|---|
| `tables/` | Table definitions (`CREATE TABLE ... IF NOT EXISTS`-guarded) | One file per table, named after the table itself -- e.g. `sector_group_map.sql`, not `create_sector_group_map.sql` | Yes, for a table that doesn't exist yet. See "On table changes" below once it's live with data. |
| `seed/` | Reference/lookup data for tables in `tables/` | Same filename as the table it seeds | Yes -- written as `MERGE`, so re-running just re-asserts the current correct mapping |
| `views/` | View definitions | One file per view, named after the view -- e.g. `vw_review_analysis.sql` | Yes -- `CREATE OR ALTER` makes "current definition" and "latest change" the same thing |
| `migrations/` | One-time, already-applied structural changes to tables that were already live with data (e.g. an `ALTER TABLE ... ADD`) | Numbered, in the order they were applied | **No** -- these are a historical record, not something to run again. Never edit a migration after it's been applied; add a new one instead. |
| `diagnostics/` | One-off investigative queries (not schema-defining) | Named after what they investigate | N/A -- not part of the object model, kept for reference only |

## Why the split

Objects that can be made re-runnable by construction (views via
`CREATE OR ALTER`, seed data via `MERGE`) get one canonical file, edited in
place -- git history is the changelog, there's no separate "migration step"
because there's no meaningful difference between "current state" and
"latest applied change" for these.

Genuine structural changes to an **already-live** table are different --
`CREATE TABLE` isn't re-runnable against a table that already has data and a
different shape. Those go in `migrations/` as a numbered, one-time,
never-edited-after-the-fact record. As of this writing, `migrations/` is
empty in this folder, but the pattern already exists in practice: see
`multi_source_sentiment_pipeline.py`'s `_ensure_schema()`, which added
`model_number`/`review_country` to `dbo.sentiment_analysis_reviews` after it
was already live with 2,933 rows. That change should eventually get a
matching file here so the repo has one complete record of schema history,
rather than it being split across SQL and Python.

## Run order for a fresh database

1. `tables/*.sql` (any order -- no cross-table dependencies yet)
2. `seed/*.sql`
3. `views/*.sql`
4. `migrations/*.sql`, in numeric order, if starting from an older snapshot

## `_archive/`

Superseded files kept temporarily for reference during the reorganisation
into this structure. Not part of the object model. Safe to delete.
