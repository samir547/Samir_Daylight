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

## Objects

| File | Object | Notes |
|---|---|---|
| `tables/sector_group_map.sql` | `dbo.sector_group_map` | Raw sector label -> reporting group; seeded by `seed/sector_group_map.sql` |
| `tables/sentiment_action_items.sql` | `dbo.sentiment_action_items` | The business to-do items: 3 rows per generation, one generation per review window ("Last 3 months", "Last 6 months", "Last 12 months") per run, history kept. Also created by the pipeline itself, with `evidence_strength`, `window_label` and `window_months` added by their own guarded `ALTER`s (the action-items entries at the end of `MIGRATIONS`, `daylight_sentiment/infra/db/connection.py`) |
| `views/vw_review_analysis.sql` | `dbo.vw_review_analysis` | Reviews with resolved SKU, region, parsed review date, sector group and English display text |
| `views/vw_latest_action_items.sql` | `dbo.vw_latest_action_items` | The newest generation of to-do items for each `window_label`, for Power BI, with `evidence_strength` and a plain-text `basis` line that notes when the newest review is more than 45 days old |

Views are applied by an administrator in SSMS: the pipeline's login
(`daylight_agents`) can create and alter tables but not views.

### Action items in Power BI

The to-do items are generated at run time for a few fixed review windows
(by default the last 3, 6 and 12 months, each ending on the newest review
date and no later than the run date) and stored; the report only reads them.
So the report's date slicer does not drive them. Set the page up with a
single-select slicer on `window_label` (default "Last 6 months") and sort the
items visual by `item_rank`. Rows saved before `window_label` existed appear
under "Custom window". When `window_end` is more than 45 days before the date
the view is queried (`stale_after_days` in the view), `basis` adds that the
items may be out of date.

## Run order for a fresh database

1. `tables/*.sql` (any order -- no cross-table dependencies yet)
2. `seed/*.sql`
3. `views/*.sql`
4. `migrations/*.sql`, in numeric order, if starting from an older snapshot

## `_archive/`

Superseded files kept temporarily for reference during the reorganisation
into this structure. Not part of the object model. Safe to delete.
