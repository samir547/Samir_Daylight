# Sentiment Pipeline — Design Document

**Entry point:** `python -m daylight_sentiment.cli`
**Status:** Production
**Last updated:** 2026-09-17

## 1. Purpose

Pulls three review exports from SharePoint, classifies every review's sentiment
(and product sector / service aspect) with an LLM, translates non-English
reviews to English, and loads the result into a SQL Server database that
Power BI reads from — unattended, end to end, on a single invocation.

```
python -m daylight_sentiment.cli --provider claude
```

## 2. Package layout (since 2026-09-17)

The former 1,838-line `multi_source_sentiment_pipeline.py` is now a layered
package. The compatibility shim that briefly replaced it (re-exports + a
`DatabaseWriter` facade) was retired on 2026-09-17 once the standalone scripts
and tests imported from the package directly; `python -m daylight_sentiment.cli`
is the only entry point.

```
daylight_sentiment/
    config.py               LLMProvider enum + settings dataclasses (Claude/
                            SharePoint/DB/SourceFiles/Output/Tuning) +
                            Config.from_env().
                            Config() is pure defaults, zero env access;
                            from_env() is the ONE place .env/os.environ is read.
                            Flat legacy names (config.db_server, ...) still work
                            via an alias layer.
    logging_setup.py        configure_logging() -- the one logging config
    domain/                 pure logic, no I/O, depends on nothing in-tree
        taxonomy.py           SECTORS / ASPECTS / SENTIMENTS
        prompts.py            SENTIMENT_GUIDANCE, prompts, _build_context, ...
        action_items.py       review window, summary, reply validation for
                              the business to-do items (see §8a)
        action_items_prompt.py  the to-do items prompt
        normalise.py          _normalise, _sanitise_sector_label, _strip_fences,
                              _align_batch
    infra/                  external I/O; depends only on config + domain
        review_sources/       ReviewSource protocol + per-source loaders
                              (trustpilot.py, amazon_brightdata.py), coercion
                              helpers, resolve_source_file(), load_reviews()
        sharepoint_sync.py    SharePointSync, pattern-matched (see §7)
        db/                   connect(), schema MIGRATIONS registry,
                              ReviewRepository, RunRepository,
                              ActionItemRepository
        analyzers/            SentimentAnalyzer ABC (base.py),
                              errors.py (typed provider errors),
                              claude_analyzer.py, factory.py (provider registry)
        translation.py        run_translation_pass() + SettingsWriter (see §5a)
    application/            orchestration
        pipeline.py           MultiSourcePipeline
        aggregator.py         AnalysisAggregator
        output_generator.py   OutputGenerator
        checkpoint.py         load_checkpoint() + CheckpointWriter
        action_items.py       generate_action_items() (see §8a)
    cli.py                  parse_args + a thin main() wiring phase functions
scripts/                    operator tools that import the package (not library
                            code): translate_reviews, test_prompt_on_review,
                            detect_review_languages, load_asin_sku_map,
                            process_varun_amazon_batch. Run from the repo root
                            as `python -m scripts.<name>`.
tests/                      offline test suite (pytest; no API/DB access)
```

Dependency direction is enforced by the folder structure: `domain/` imports
nothing in-tree, `infra/` imports only `config`/`domain`, `application/`
imports both, `cli.py` imports everything. Standalone scripts should import
from the package directly; the four pre-refactor scripts still go through the
shim's re-exports and facades (`DataLoader`, `DatabaseWriter`), which is
supported indefinitely.

## 3. Code flow — what `cli.main()` does, in order

```
1. load_dotenv()                                   -> explicit, at entry (never at import)
2. parse_args() + build_config()                   -> Config.from_env() + CLI overrides
3. configure_logging()                             -> multi_source_sentiment.log + stdout

4. if args.action_items_only:
       action_items_only(config)                   -> run_action_items() on the
                                                      reviews already in the DB
       return 0, or 1 on any failure                <-- early exit (§8a)

   if args.load_db_only:
       load_database(config, existing JSON)
       delete_analysis_outputs(config)   [unless --keep-output-files]
       return                                      <-- early exit
       # NOTE: deliberately does NOT run the translation pass (documented gap;
       # run `python -m scripts.translate_reviews` manually if needed after this path)

5. sync_sharepoint(config)                         [if SP_* set, not --skip-sharepoint-sync]
       pattern-matched download of the 3 exports into Reviews/ (see §7)
       SharePointSignInRequiredError (no saved sign-in, no terminal)
       -> logged at ERROR, main() exits 2 before anything below runs
   if a sync ran and no source got a new file:
       log "No new review files waiting in SharePoint; nothing to do"
       return 0                                    <-- early exit
       # nothing analysed, written, loaded or moved, and no translation pass

6. all_reviews = load_reviews(build_default_sources(config.sources))
       each ReviewSource fetches + normalises; uid = f"{source}:{type}:{review_id}"

7. analyzer = build_analyzer(config)               -> analyzer registered for
                                                      config.llm_provider (Claude)

8. done = resume_state(config, args, all_reviews)
       checkpoint replay (successful rows only), restricted to loaded uids,
       then apply_rerun_flags() reopens product/service reviews if requested

9. results = MultiSourcePipeline(config, analyzer).process_all_reviews(...)
       type-homogeneous batches of 10, ThreadPoolExecutor(max_workers=8);
       one LLM call per review, each succeeding or failing on its own;
       every result appended to the checkpoint (CheckpointWriter, flushed
       per record) DURING the run. A FatalProviderError cancels the
       remaining batches and main() exits 1

10. write_outputs() -> Trustpilot_Result/*.json    (summary + per-review detail)

11. load_database()                                [if DB_* set, not --skip-db-load]
        ensure_schema()          -- idempotent MIGRATIONS registry
        ReviewRepository.upsert() -- MERGE per 50-row batch, keyed on uid
        RunRepository.upsert()    -- DELETE+INSERT keyed on run_timestamp
    delete_analysis_outputs()                      [unless --keep-output-files]
        # only after load_database() returned without raising
    move_synced_files_to_processed()               [if a sync ran]
        # In Progress -> Processed; a SharePointSignInRequiredError here
        # exits 2 (the reviews are already in the database)

12. run_translation(config, analyzer)              [if DB_* set, not --skip-translation]
        infra.translation.run_translation_pass(): offline language detection,
        then LLM translation of fr/de/it/es/nl/sv/pl/cs rows where
        review_text_en IS NULL; writes review_text_en + detected_language.
        A FatalProviderError stops the pass and main() exits 1

13. run_action_items(config, analyzer)             [if step 11 loaded, this run
                                                    analysed new reviews (not
                                                    replayed from the checkpoint),
                                                    not --skip-action-items]
        the top 3 business to-do items (§8a). Any failure logs one ERROR line
        and the exit code is unchanged; a FatalProviderError exits 1
```

**Key invariants:**
- Step 11's delete only runs after the DB load returns without raising —
  nothing is destroyed before it's confirmed durable somewhere else.
- Rows with `analysis IS None` (failed LLM calls) are never upserted and never
  trusted from the checkpoint — a failed rerun can't blank out good data.
- Translation state (`review_text_en IS NULL`) is independent of the sentiment
  checkpoint in both directions.
- A run with nothing new in SharePoint does no work at all. The cost: rows
  whose translation failed earlier (normally retried on every run) are not
  retried on such a run; they are picked up by the next run that has new
  files, or by `python -m scripts.translate_reviews`.

**Exit codes:** `0` finished, including the "nothing new in SharePoint" early
exit; `1` fatal LLM provider error (`FatalProviderError`), or
`--action-items-only` failed; `2` SharePoint sign-in needed
(`SharePointSignInRequiredError`).

## 4. Data sources & the `uid` key

| Source | ReviewSource | Format | Rows |
|---|---|---|---|
| Trustpilot product | `TrustpilotProductSource` | `.xlsx` | 872 |
| Trustpilot service | `TrustpilotServiceSource` | `.xlsx` | 1,395 |
| Amazon product | `AmazonBrightDataSource` | `.csv` | 666 (of 672 — 6 dropped for scrape errors) |

New sources implement the `ReviewSource` protocol (`name` +
`fetch() -> list[dict]`) and get added to `build_default_sources()` — not
bolted on as parallel scripts. (Whatever replaces Bright Data should land
here; ad hoc pulls like Varun's 2026-09-11 Amazon batch are the open design
question — one-off script vs. formalized `amazon_adhoc` source.)

The Amazon loader also captures `model_number` (uppercased; Daylight's own
item code) and `review_country` (reviewer-stated, not marketplace-queried).

`uid` is `{source}:{type}:{review_id}` — **this, not `review_id`, is the dedup
key everywhere** (checkpoint, resume filter, database `MERGE`). `review_id` is
only unique *within* a source.

## 5. LLM analysis

Every review returns `sentiment` (positive/negative/neutral), `confidence`
(0.0–1.0), up to 6 `key_themes`, plus either `primary_sector` (product) or
`primary_aspect` (service). `ASPECTS` is a closed vocabulary (anything else →
`general_experience`); `SECTORS` is a **seed list, not a hard cap** — a novel
but clearly-stated use case is kept as a sanitised snake_case label
(`_sanitise_sector_label`) rather than collapsed to `general`.

**`SENTIMENT_GUIDANCE` (added 2026-09-12):** every prompt instructs the model
to weigh the OVERALL experience — fixes false-neutrals on flat factual
complaints ("Not received") and false-negatives on mostly-positive reviews
with one minor complaint. The star rating is passed as context
(`_build_context`) but deliberately as a secondary cue, not an override — a
separate QA pass showed mismatched *ratings* are often the unreliable signal.

| | Claude (`--provider claude`, the only and default provider) |
|---|---|
| Model | `claude-sonnet-4-6` unless `CLAUDE_MODEL` is set; `--model` overrides both for one run |
| API key | `ANTHROPIC_API_KEY` |
| Cost/review | ~$0.003 |
| Calls | 1 per review (the base `analyze_batch()` loop) |

**Provider interface:** `SentimentAnalyzer` (`infra/analyzers/base.py`)
requires `complete_json(prompt, max_tokens=None) -> dict` (send one prompt,
return the parsed JSON object; `max_tokens=None` means the provider's default
for short classification output, 500 for Claude; a reply cut off at the
limit raises `ResponseTruncatedError`), `analyze_product_review()` and
`analyze_service_review()`;
`analyze_batch()` returns one entry per input review, in order: the
analysis dict, `None` (nothing to analyse), or the exception that failed
that review alone; a `FatalProviderError` is raised instead, so the run
stops. Its one-call-per-review default catches each review's non-fatal error
and returns it in that review's slot, so one bad reply (unparseable JSON,
truncation) does not cost the rest of the batch. A provider may override it
to send a whole batch per call (`_build_batch_prompt()` and `_align_batch()`
in `domain/` support that); if such an override raises a non-fatal error,
every review in that batch fails with that error. `ClaudeSentimentAnalyzer` parses
model JSON with `strict=False` after stripping code fences: raw control
characters inside string values (seen on some translation responses) don't
fail the call.

**Adding a provider:** add an `LLMProvider` member (its value is the
`--provider` choice and the name of its settings field on `Config`), a
settings dataclass + `Config` field loaded in `from_env()`, a
`SentimentAnalyzer` implementation including `complete_json`, and an entry in
`_ANALYZERS` in `infra/analyzers/factory.py`. `build_analyzer()` raises a
clear `ValueError` for a provider with no registered analyzer.

**Typed provider errors (`infra/analyzers/errors.py`):** a provider-neutral
hierarchy — `FatalProviderError` (every further call would fail the same way:
bad key, no credit, missing permission, unknown model), `RateLimited` and
`TransientProviderError` (still failing after the SDK's own retries),
`ResponseTruncatedError` (the reply hit the call's output-token limit,
`stop_reason == "max_tokens"`; not fatal), and the base `ProviderError` (a
failure specific to one request). Each provider maps
its SDK's exceptions onto it in one classifier; for Claude that is
`classify_anthropic_error()` (401/402/403/404 and a "credit balance is too
low" 400 are fatal). The Anthropic SDK keeps doing its own retries for
429/5xx/connection errors (`max_retries`, default 4). Callers branch by type:
the pipeline and the translation pass re-raise `FatalProviderError`, which
stops the run with a clear log message and exit status 1, and treat every
other error as a failure of that review or row only:
`MultiSourcePipeline.process_batch()` logs it once at WARNING with the uid
and stores `"<ExceptionType>: <message>"` in the review's `error`, which is
what the summary's `failure_reasons` counts. Reviews not analysed
before a fatal stop are not checkpointed, so the next run picks them up.

### 5a. Translation pass (`infra/translation.py`, added 2026-09-12)

Runs at the end of every regular pipeline run (skip with
`--skip-translation`): offline language detection (langdetect, zero API cost,
5-word minimum to avoid short-text false positives), then one LLM call per
confirmed fr/de/it/es/nl/sv/pl/cs review, writing `review_text_en` +
`detected_language` via a narrow two-column UPDATE that structurally cannot
touch sentiment data. Resumable via `review_text_en IS NULL` — no checkpoint
file. `python -m scripts.translate_reviews` is the standalone/backfill entry.

`run_translation_pass()` takes the analyzer a run already built and calls its
public `complete_json(prompt, max_tokens=...)`, so whichever provider the run
uses is reused with no second API client. The output limit comes from
`translation_max_tokens()`: `len(text) // 2 + 200` on the (already truncated)
source text, clamped to 500–4,096, so a full 6,000-character review gets
3,200 tokens. A reply that still hits the limit raises
`ResponseTruncatedError`; the row is logged as failed, `review_text_en` stays
NULL, and the pass moves on to the next row. The writer is duck-typed (needs `._connect()`).
`SettingsWriter(config.db)` is the stock writer. The module lived at the repo
root as `translation_lib.py` until 2026-09-17, kept dependency-free to avoid a
cycle with the old monolith (ISSUE-08); it now imports `REVIEWS_TABLE` from
`infra.db` instead of duplicating it.

## 6. Checkpointing & resume

`multi_source_checkpoint.jsonl` gets one JSON line per completed review,
flushed immediately (`CheckpointWriter`). `load_checkpoint()` keeps only rows
with a non-null `analysis` (failures retry) and the *last* record per uid
(rerun flags append superseding entries). Reviews succeed or fail
individually, so when one review in a batch fails, the others in the batch
are still checkpointed as done and only the failed one is analysed again
next run.

**Resume is the default**; `--fresh-start` is the only way to force a full
reprocess. `--rerun-product-reviews` / `--rerun-service-reviews`
(`apply_rerun_flags()`) reopen just one review type — use after a taxonomy or
prompt change; combine both to reopen everything without `--fresh-start`.

## 7. SharePoint sync (`infra/sharepoint_sync.py`)

Resolves the share link via Microsoft Graph's `/shares` endpoint, then
downloads the review exports into `Reviews/`, each saved under its real
SharePoint filename — a renamed export lands as a new file alongside
whatever differently-named stale file was already there, rather than
overwriting it.

**Pattern matching:** files are selected purely by `FilePattern` (name tokens
+ extension, e.g. *trustpilot*product*.xlsx); the newest match always wins,
on both sides — `select_items_by_patterns()` on SharePoint and
`resolve_source_file()` locally. Neither side has a preferred/expected
filename to check first: a renamed export — "(2)" suffix, fresh row count,
date stamp — is resolved exactly like any other name, because nothing is
ever compared against a specific one. A pattern with zero matches still
raises. Since a sync always downloads under the real name and resolution
always picks the newest file by mtime, a stale differently-named leftover in
`Reviews/` is harmless — the freshly synced file wins.

**Auth path:** silent sign-in from the token cache first, then ROPC (fails
on this account — MFA), then the MSAL device-code flow. The cache is
`.sp_token_cache.json` next to the package (owner-only, 0o600, on POSIX);
`scripts/check_sharepoint_connection.py` reads and writes the same file.
`SP_PASSWORD` must be set for `SharePointSettings.configured` to be true.

**Unattended guard:** the device-code flow is only started when someone can
complete it. `SharePointSync(interactive=None)` auto-detects: stdin must
exist, be open and be a terminal (on Windows, a real console — `NUL` also
claims `isatty()`). Otherwise `_get_token` raises
`SharePointSignInRequiredError` instead of starting the flow and polling for
~15 minutes, and `cli.main()` logs it and exits 2. The message names the
recovery command, `python -m daylight_sentiment.cli --sync-sharepoint-only`,
which uses this same sign-in code and cache and only downloads into
`Reviews/` (no analysis, database or move). Run it in a terminal, complete
the device-code sign-in, and later scheduled runs sign in silently again.
Steps for an operator are in README §7.5. The durable fix is an app
registration with application-level `Sites.Selected` (see ISSUE-21).

## 8. Database load (`infra/db/`)

Target: `daylight-powerbi-db-1.database.windows.net` / `DAYHANSA_SQL1`.
`connection.connect()` is the public connection factory (Packet Size=4096 and
the 120s statement timeout — see Network resilience). `ensure_schema()`
applies an ordered, idempotent, append-only `MIGRATIONS` registry (the
reviews and runs CREATEs, the guarded `model_number` / `review_country`
ALTERs, and the `sentiment_action_items` CREATE). New schema changes are
appended to that registry.

### `dbo.sentiment_analysis_reviews` (`ReviewRepository`)
Primary key: `uid`. `MERGE ... USING (VALUES ...)`, 50 rows/statement
(21 cols × 50 = 1,050 params, under the 2,100 limit). Re-loading updates rows
in place, never duplicates. The MERGE reads and writes **only**
`ReviewRepository.COLUMNS` (+ server-side `loaded_at`) — the structural
property that prevents the write-back NULLing bug class caught 2026-09-12 —
and it's pinned by tests.

Columns: `uid (PK), review_id, review_type, review_source, rating,
product_name, product_sku, product_asin, model_number, review_country, title,
review_text, reviewer_name, review_date, verified_purchase, sentiment,
confidence, primary_sector, primary_aspect, key_themes (JSON text),
analysis_error, loaded_at`, plus `review_text_en` / `detected_language`
(added by the translation pass, not by the MERGE).

### `dbo.sentiment_analysis_runs` (`RunRepository`)
One row per run, `DELETE`+`INSERT` keyed on `run_timestamp`.

### Network resilience
Unchanged from the 2026-08 fixes: `Packet Size=4096`, 50-row batches each
committed independently with reconnect + backoff (`max_batch_retries=6`).

### Downstream view
`SQL/views/vw_review_analysis.sql` resolves SKU/region (see its extensive
header), consolidates sectors via `dbo.sector_group_map`, and exposes
`review_text_en`, `detected_language`, and `review_text_display`
(`COALESCE(review_text_en, review_text)`) for Power BI.

## 8a. Action items (`application/action_items.py`)

The top 3 to-do items for the business, from the stored sentiment results,
read by the client's Financial Director in Power BI. Read-only: no status
tracking or write-back.

**Position.** Step 13 of §3: after the database load, the move to Processed
and the translation pass, so the reviews from this run are in the database
with their English text. It runs only when this run analysed and loaded new
reviews, so it is skipped by the "nothing new" early exit, `--skip-db-load`,
`--load-db-only` and a run with no `DB_*`. `--action-items-only` runs it on
its own.

**Flow** (`generate_action_items()`):

1. `ActionItemRepository.fetch_analysed_rows()` reads analysed rows
   (`sentiment IS NOT NULL`) from `dbo.vw_review_analysis`: `uid`,
   `review_type`, `review_source`, `sentiment`, `confidence`, `rating`,
   `sector_group`, `primary_aspect`, `product_region`, `key_themes`,
   `review_date_parsed`, plus `review_text_en` / `review_text` for negative
   reviews only. `reviewer_name` is never read.
2. `domain.action_items.select_window()`: rows with no `review_date_parsed`
   are left out and counted in the log. The window is the last
   `window_months` (default 12, `ACTION_ITEMS_WINDOW_MONTHS`,
   `--action-items-months`) ending on the newest review date, not today.
   Under `min_window_reviews` (100) it widens to every dated review; under
   `min_total_reviews` (50) even then, generation is skipped with a WARNING
   (not a failure). Settings: `ActionItemSettings` in `config.py`.
3. `build_summary()`: totals and sentiment split; by review type and source;
   product reviews by `sector_group` and service reviews by `primary_aspect`
   (count, negative count, negative share); product reviews by
   `product_region`; the 15 most frequent `key_themes` among negative reviews
   (JSON parsed defensively, lower-cased); monthly negative share; up to 10
   negative excerpts (most confident first, round-robin across areas,
   identical texts once, English text preferred, 300 characters, email
   addresses and phone numbers blanked).
4. One `analyzer.complete_json(prompt, max_tokens=1500)` call with the prompt
   from `domain/action_items_prompt.py`: plain British English for a
   non-technical reader, exactly 3 items ranked by business impact, titles
   starting with a verb, only numbers from the summary, cited evidence, say
   when data is thin, no generic advice.
5. `validate_action_items()`: exactly 3 items, ranks 1-3 once each, every
   field a non-empty string within its cap (`title` 200 and `related_area`
   100, the column sizes; the text fields 1,000-1,500), `related_area`
   "overall" or a group/aspect/region in the summary. Anything else raises
   `ActionItemsValidationError`; nothing is saved.
6. `ActionItemRepository.save()` applies `ensure_schema()` and inserts the 3
   rows in one transaction (rolled back on any error).

**Table `dbo.sentiment_action_items`** (`SQL/tables/sentiment_action_items.sql`,
and the last `MIGRATIONS` entry): `item_id INT IDENTITY PK`,
`generation_id UNIQUEIDENTIFIER` (the 3 rows of one generation),
`generated_at DATETIME2 DEFAULT SYSUTCDATETIME()`, `run_timestamp` (the
run's `sentiment_analysis_runs.run_timestamp`; NULL for
`--action-items-only`; not an enforced foreign key), `[rank] TINYINT`,
`title NVARCHAR(200)`, `description`, `rationale`, `evidence`
(`NVARCHAR(MAX)`), `related_area NVARCHAR(100)`, `window_start`,
`window_end` (`DATE`), `reviews_analysed INT`, `llm_provider`, `llm_model`;
`UNIQUE (generation_id, [rank])`. Insert-only: every generation is kept.

**View `dbo.vw_latest_action_items`** (`SQL/views/vw_latest_action_items.sql`):
the rows of the newest generation (latest `generated_at`, then highest
`item_id`), with `item_rank` for sorting and a `basis` column ("Based on N
reviews from <window_start> to <window_end>"). The pipeline's login
(`daylight_agents`) can create tables but not views, so an administrator
applies this file in SSMS.

**Flags:** `--skip-action-items`, `--action-items-months N`,
`--action-items-only`, `--action-items-dry-run` (generate and print, write
nothing; combinable with `--action-items-only`).

**Failure behaviour.** In a normal run any failure (provider error, reply
that fails validation, missing view, database error) logs one ERROR line,
`Action items were not generated: ...`, and the exit code stays what it
would otherwise be: the reviews are already saved. `--action-items-only`
exits 1 on the same failures. A `FatalProviderError` is reported as
everywhere else and exits 1.

## 9. Testing

`python -m pytest` — offline, no API/DB access, ~1.5s. Characterization tests
pin the domain functions, checkpoint semantics, MERGE column-safety, loader
mappings (synthetic files), pattern matching, provider-error classification
and the fatal-error stop, the provider registry, and the CLI phase functions. Written as the safety net for the 2026-09-17
refactor; keep them green.

## 10. CLI flags

| Flag | Effect |
|---|---|
| `--provider {claude}` | LLM backend; choices come from `LLMProvider` (default `claude`) |
| `--model` | Override the provider's model for this run (wins over `CLAUDE_MODEL`) |
| `--limit N` | Smoke-test on the first N reviews |
| `--workers`, `--batch-size` | Worker threads / reviews per unit of work (defaults 8 / 10, defined in Config only) |
| `--fresh-start` | Ignore the checkpoint; reprocess every review |
| `--rerun-product-reviews` | Reopen only product reviews (both sources) for reprocessing |
| `--rerun-service-reviews` | Reopen only service reviews; combine with the above to reopen all |
| `--skip-sharepoint-sync` | Use whatever's already in `Reviews/` |
| `--skip-db-load` | Analyze and write JSON, but don't load the DB (or delete JSON) |
| `--skip-translation` | Don't run the translation pass |
| `--load-db-only` | Skip sync + analysis; load existing JSON and exit (no translation) |
| `--sync-sharepoint-only` | Download new files into `Reviews/` and exit; also the sign-in recovery command |
| `--keep-output-files` | Load the DB as normal, but don't delete the JSON afterward |
| `--skip-action-items` | Don't generate the business to-do items (§8a) |
| `--action-items-months N` | Base the to-do items on the last N months (default 12) |
| `--action-items-only` | Skip sync, analysis and loading; generate the to-do items from the database and exit (1 on failure) |
| `--action-items-dry-run` | Generate and print the to-do items without saving them |

## 11. Known issues

- **`max_review_chars` (6,000)** truncates review text sent to the translation
  pass; sentiment calls send the full review text (the one-review-per-call
  path doesn't apply the limit).
- **Amazon CSV loader** silently drops scrape-error and duplicate rows —
  check `multi_source_sentiment.log` for `WARNING` lines if the count looks low.
- **SharePoint token expiry** — the saved sign-in eventually expires and a
  person has to redo the device-code sign-in. A scheduled run with no saved
  sign-in exits 2 at once with an ERROR line naming the recovery command
  (§7, README §7.5); runs fail that way until someone signs in again.
- **Unexplained deletion incident** — the checkpoint and both JSON outputs
  were once found deleted from `Trustpilot_Result/` mid-session, from outside
  the pipeline's own code path. Recovered from the Recycle Bin; cause never
  identified. Worth a second look if it recurs. (ISSUE-19; monitor only.)
- **Amazon multi-market region collapse (ISSUE-20)** — `vw_review_analysis`
  resolves one product code per ASIN via `MAX(product_code)`, so every review
  of a multi-market ASIN gets one region (alphabetically the US code wins)
  regardless of where it was posted. The view's comment says this is
  deliberate for Family/Category purposes, but `product_region` specifically
  is therefore not necessarily accurate for those ASINs. Needs a decision
  (prefer `review_country`?), not silently a fix.
- **SharePoint sign-in is delegated, not app-level (ISSUE-21)** — the pipeline
  signs in as a person (device-code flow, token cached in `.sp_token_cache.json`),
  so an unattended run works only while that cached token stays valid, and the
  pipeline's access depends on one individual's account. The durable design is
  an Entra app registration (single tenant) with the Microsoft Graph application
  permission `Sites.Selected`, granted `write` on one SharePoint site only
  (write is needed to move files from "In Progress" to "Processed"), using the
  client-credentials flow with a client secret or certificate. Two prerequisites
  are outside the pipeline's control: (1) the review exports currently sit behind
  a personal OneDrive sharing link, which `Sites.Selected` cannot cover — they
  would need to move into a SharePoint team site; (2) a Daylight tenant admin must
  register the app and grant consent. Parked until Daylight decides to take both
  on. The client secret expires (24 months maximum) and would need a renewal owner.
- **Action items wording varies between runs** — the to-do items call runs
  at the provider's default temperature (no fixed temperature is set), so
  regenerating from the same data can rank or word the items differently.
  Each generation is stored with its window and model, and Power BI shows
  only the latest.
- **Bright Data replacement undecided (ISSUE-03)** — Varun is pulling Amazon
  data manually in the interim; the replacement should become a
  `ReviewSource` implementation.
