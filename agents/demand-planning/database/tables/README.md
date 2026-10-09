# tables/

One `.sql` file per table this pipeline owns, matching what's actually deployed.

**Not yet populated as of 2026-09.** `forecast_output` and `forecast_accuracy`'s real `CREATE TABLE` definitions currently live inline in `data/queries.py` (`CREATE_FORECAST_OUTPUT` / `CREATE_FORECAST_ACCURACY`), which is what `main.py --write-db` actually executes. Extracting those into standalone files here — with `queries.py` reading them rather than embedding the DDL as Python strings — would make this folder the real source of truth instead of documentation-after-the-fact. Worth doing, not done as part of this weekend's work to avoid touching the write path right before a demo.
