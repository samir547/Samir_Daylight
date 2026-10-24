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
        the top 3 business to-do items for each review window (§8a). Each
        failed window logs one ERROR line and the exit code is unchanged; a
        FatalProviderError exits 1
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
read by the client's Financial Director in Power BI. One set is generated
for each review window (default: the last 3, 6 and 12 months). Read-only:
no status tracking or write-back.

**Position.** Step 13 of §3: after the database load, the move to Processed
and the translation pass, so the reviews from this run are in the database
with their English text. It runs only when this run analysed and loaded new
reviews, so it is skipped by the "nothing new" early exit, `--skip-db-load`,
`--load-db-only` and a run with no `DB_*`; a run with nothing new leaves the
stored items as they are. `--action-items-only` runs it on its own.

**Windows.** `ActionItemSettings.windows_months` in `config.py` (default
`(3, 6, 12)`; `ACTION_ITEMS_WINDOWS_MONTHS`, comma-separated, or
`--action-items-months 3,6,12`; a single number gives one window; values
must be whole numbers from 1 to 255, the `TINYINT` column's range, and are
sorted with repeats removed). Each window is labelled from its length by
`window_label()` ("Last 3 months"). The windows are fixed, named sets rather
than date ranges chosen in the report: the items are written by the model at
run time, so a Power BI date slicer can't change them, and there is no
real-time generation.

**Flow** (`generate_action_items()`):

1. `ActionItemRepository.fetch_analysed_rows()` reads analysed rows
   (`sentiment IS NOT NULL`) from `dbo.vw_review_analysis` once for all
   windows: `uid`, `review_type`, `review_source`, `sentiment`, `confidence`,
   `rating`, `sector_group`, `primary_aspect`, `product_region`,
   `resolved_sku`, `product_name`, `key_themes`, `review_date_parsed`, plus
   `review_text_en` / `review_text` for negative reviews only.
   `reviewer_name` is never read. If this fails, nothing is generated.
2. `domain.action_items.select_windows()`: rows with no `review_date_parsed`
   are left out and counted in the log. Every window ends on the earlier of
   the newest review date and today, and starts N months before that
   (`months_before()`, plus one day). When the cap at today applies (a review
   dated in the future, usually a misparsed date) one WARNING names the date;
   reviews dated after today stay in the windows. A window with fewer than
   `MIN_WINDOW_REVIEWS` (100) analysed reviews is returned skipped and logged
   as a WARNING, never widened, so its label always describes it; a skipped
   window is not a failure. If every window is skipped nothing is saved.
3. For each window that isn't skipped, `generate_window_items()`:
   `build_summary()` of that window's rows, one
   `analyzer.complete_json(prompt, max_tokens=1500)`, validation against that
   window's summary, and one retry (below). Then
   `ActionItemRepository.save()` with the window's label and length. Any
   exception in one window (a reply that fails validation twice, a provider
   error, a database error) is recorded on that window's `WindowOutcome`
   and the remaining windows still run. A `FatalProviderError` stops all of
   them, since every call would fail the same way. Up to 3 calls per run,
   plus one retry per window that needs it.

**Summary** (`build_summary()`). Every grouped section gives `reviews`,
`negatives`, `negative_share_pct` and `small_sample` (fewer than
`SMALL_SAMPLE_REVIEWS` = 30 reviews; the threshold is in the summary as
`small_sample_threshold_reviews`). Sections: `window` (label, dates,
reviews); the sentiment split and `negative_reviews_total`;
`by_review_type_and_source`; `product_by_sector_group` and
`product_by_sector_group_and_source`; `service_by_aspect` and
`service_by_aspect_and_source`, under the readable names in `ASPECT_NAMES`
(one mapping for the summary, `related_area` and Power BI; a label outside it
is humanised); `product_by_region` (all sources together) and, separately,
`product_by_region_and_source`; `products_most_negative`;
`top_negative_themes`; `monthly_negative`; `trend`; `negative_excerpts` (up
to 10, most confident first, round-robin across areas, identical texts once,
English text preferred, single-spaced, cut to 300 characters after the last
whole word, email addresses and phone numbers blanked).

- **Products.** `vw_review_analysis` has no product family or catalogue
  name; it exposes `resolved_sku` (the one key resolved for both sources)
  and the raw `product_name` each review was filed under. Products are
  grouped by `resolved_sku` and shown under their most frequent
  `product_name` (cut to 80 characters at a word boundary; the SKU when
  there is none), with the region and `reviews_by_source`. Only products
  with at least `MIN_PRODUCT_NEGATIVES` (3) negatives are listed, at most
  `TOP_PRODUCTS` (10). Regional variants of one product have different SKUs
  and are listed separately.
- **Themes.** `key_themes` is free text. Each theme is lower-cased and
  trimmed (`parse_themes()`), then counted under `theme_key()`: words
  singularised, a short explicit stem table (`defective` -> `defect`,
  `broken`/`broke` -> `break`), words sorted. "product defect", "defective
  product" and "product defects" are one theme, shown under its most
  frequent spelling; a review counts once per merged theme. Themes that
  merely overlap in meaning ("product failure", "short lifespan") stay
  separate, which is why the prompt forbids adding counts together.
- **Trend** (`build_trend()`). `recent_part_start()` splits the window into
  its last third of days (at least one day) and the earlier two thirds;
  reviews dated after `window_end` count as recent. `recent_part` and
  `earlier_part` give each part's dates and review count. For
  `by_review_type_and_source`, `product_by_sector_group`,
  `service_by_aspect` and `product_by_region_and_source` -- the same groups
  in the same order as the main sections -- each entry gives `recent` and
  `earlier` tallies with their own `small_sample` flags. For the first
  `TREND_THEMES` (10) entries of `top_negative_themes`, each part gives
  `reviews` (all reviews in that part), `negatives_mentioning` and
  `share_of_reviews_pct` (a share of all the part's reviews, so the parts
  compare whatever their size, and never a share of the negative reviews).

**Prompt** (`domain/action_items_prompt.py`): plain British English for a
non-technical reader, exactly 3 items ranked by business impact, titles
starting with a verb, only numbers from the summary (and no derived
figures), cited evidence, no generic advice. Its rules:

- *Like with like* (rule 5): regions, sources, sector groups, service
  aspects and products are compared only within the same source (the
  `..._and_source` tables and `reviews_by_source`), or the item says that
  the source mix differs and treats the gap as background; a gap between
  groups with different source mixes is never the main reason for an item.
  Amazon and Trustpilot are different populations (Trustpilot reviews are
  mostly company-invited and skew positive), so a difference between them
  is context, never evidence of a problem by itself.
- *Small samples* (rule 6): an item based mainly on a small-sample group is
  framed as monitor or investigate, never overhaul or fix.
- No claim that a difference is statistically significant or "not random";
  theme counts are never added or turned into a share of negative reviews;
  products are named where the evidence supports it.
- *`related_area`* (rule 12): "overall" for a cross-cutting issue; a
  catch-all group only as rule 14 allows.
- *Evidence strength follows the evidence* (rule 13): "early signal" when
  the key figures behind an item are mostly `small_sample`, even when
  `related_area` is "overall", and always when `related_area` itself is a
  small-sample group; "strong" only for large groups with the same pattern
  in more than one group or in both parts of the trend; otherwise
  "moderate".
- *Catch-all groups are not findings* (rule 14): "General / No Use Stated",
  "Unknown", "No Region Data" and other unspecified groups are not an item
  by themselves, and no item's only action is to investigate or monitor one;
  they may be used only when the cause inside them (products, themes or
  aspects) is identified from the summary.
- *Trend* (rule 15): a clearly lower recent negative share (with a recent
  part that isn't `small_sample`) means "may already be improving", ranked
  lower or said plainly; a higher one means "getting worse"; never "fixed",
  "solved" or "resolved".

**Validation** (`validate_action_items(reply, summary)`): every text field
is single-spaced first (`single_spaced()`: any run of whitespace, line
breaks included, becomes one space -- a line break inside a JSON string
survives `json.loads(strict=False)`, and a display that drops it would join
the words). Then: exactly 3 items, ranks 1-3 once each, every field a
non-empty string within its cap (`title` 200, `related_area` 100 and
`evidence_strength` 20, the column sizes; the text fields 1,000-1,500);
`related_area` "overall" or a sector group, aspect (readable name) or region
in the summary, matched case-insensitively; `evidence_strength` one of
`strong`, `moderate`, `early signal` (stored lower-case), and `early signal`
when `related_area` is flagged `small_sample`. That last check is the only
part of rule 13 the code enforces: for an "overall" item it can't tell which
groups the item rests on, so there the rule rests on the prompt. Every number
in `description`, `rationale` and `evidence` must be one of that window's
summary numbers at 1 decimal place (every section, the trend and the
by-source tables included); a percentage must match one of the summary's
percentages, or that percentage rounded half up to a whole number. Dates
(ISO, numeric, with a month name), years (1900-2099) and periods
("12 months", "3 weeks") are removed first, and digits inside codes such as
`D35500` are not numbers. All problems found are reported together in one
`ActionItemsValidationError`.

**Wording and reliability checks.** For an `early signal` item, the
title's first word (`title_verb()`, case-insensitive) must not be one of
`STRONG_ACTION_VERBS` (fix, overhaul, resolve, eliminate, stop,
eradicate); the error asks for one of `EARLY_SIGNAL_VERBS` (Monitor,
Investigate, Review). `title`, `description`, `rationale` and `evidence`
must contain none of `BANNED_WORDS` ("significant", "significantly",
"statistically", "meaningful", "not random", "random variation"), matched
as whole words in any case by `banned_wording()`, so "insignificant" and
"meaningfully" pass while "non-significant" does not; the error names the
words found. Each trend row carries `few_mentions` when its `negatives`
(for a theme, `negatives_mentioning`) across both parts total less than
`MIN_TREND_COUNT` (10). Rules 13 and 15 forbid trend claims and
"moderate"/"strong" evidence on rows flagged `small_sample` or
`few_mentions`, and treat a theme or product with fewer than 10 negatives
as an "early signal"; those rules rest on the prompt. The flag is a
boolean, so `summary_numbers()` skips it and the accepted numbers are
unchanged. Rule 16 asks for each window's items to stand on that window
alone, without repeating an issue only because another window has it.

**Retry.** If validation fails, one retry: the same prompt plus "Your
previous reply failed because: <the errors>". Each failure is logged as a
WARNING naming the window, with the reason only. If the retry fails
validation too, that window fails and nothing is saved for it. A provider
error is not retried here.

**Saving.** `ActionItemRepository.save()` applies `ensure_schema()` and
inserts the window's 3 rows in one transaction (rolled back on any error),
under a new `generation_id`, with `window_label` and `window_months`.

**Table `dbo.sentiment_action_items`** (`SQL/tables/sentiment_action_items.sql`,
and the `MIGRATIONS` entries `create_action_items_table`,
`add_action_items_evidence_strength`, `add_action_items_window_label` and
`add_action_items_window_months`): `item_id INT IDENTITY PK`,
`generation_id UNIQUEIDENTIFIER` (the 3 rows of one window's generation),
`generated_at DATETIME2 DEFAULT SYSUTCDATETIME()`, `run_timestamp` (the
run's `sentiment_analysis_runs.run_timestamp`; NULL for
`--action-items-only`; not an enforced foreign key), `[rank] TINYINT`,
`title NVARCHAR(200)`, `description`, `rationale`, `evidence`
(`NVARCHAR(MAX)`), `related_area NVARCHAR(100)`,
`evidence_strength NVARCHAR(20) NULL`, `window_label NVARCHAR(30) NULL`,
`window_months TINYINT NULL`, `window_start`, `window_end` (`DATE`),
`reviews_analysed INT`, `llm_provider`, `llm_model`;
`UNIQUE (generation_id, [rank])`. Insert-only: every generation is kept.
The columns added after the table was first created each have their own
guarded `ALTER TABLE ... ADD` (`IF NOT EXISTS` on `sys.columns`), appended
after the create entry, which is left as it was first applied. The canonical
SQL file has the final shape and the same guarded `ALTER`s.

**View `dbo.vw_latest_action_items`** (`SQL/views/vw_latest_action_items.sql`):
for each `window_label` (NULL treated as one 'Custom window' group), the rows
of its newest generation (latest `generated_at`, then highest `item_id`, via
`ROW_NUMBER()`), with `window_label`, `window_months`, `item_rank` for
sorting, `related_area` ("Overall"), `evidence_strength` capitalised ("Early
signal"), the window dates and count, `generated_at`, and a `basis` column
("Based on N reviews from <window_start> to <window_end>", followed by ". Newest
review is from <window_end>, so this may be out of date." when `window_end` is
more than `stale_after_days` = 45 days before the query date). The pipeline's
login (`daylight_agents`) can create tables but not views, so an
administrator applies this file in SSMS. In Power BI: a single-select slicer
on `window_label` defaulting to "Last 6 months", the visual sorted by
`item_rank`.

**Flags:** `--skip-action-items`, `--action-items-months 3,6,12`,
`--action-items-only`, `--action-items-dry-run` (generate and print every
window's items, write nothing; combinable with `--action-items-only`).

**Failure behaviour.** In a normal run, each failed window logs one ERROR
line, `Action items were not generated for Last N months: ...`, and the
exit code stays what it would otherwise be: the reviews are already saved.
A failure before any window runs (the view is missing, the database is
unreachable) logs one ERROR line, `Action items were not generated: ...`.
`--action-items-only` exits 1 if any window failed or the reviews couldn't be
read, and 0 when every window was generated or skipped. A
`FatalProviderError` is reported as everywhere else and exits 1.

**Sizing.** When fewer than about 35 reviews a month arrive, the 3-month
window falls under the 100-review minimum and is skipped. If monthly review
volume grows well beyond about 150, shorter windows (for example
`ACTION_ITEMS_WINDOWS_MONTHS=1,3,6`) give fresher advice.

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
| `--action-items-months 3,6,12` | The review windows, in months, to generate to-do items for (default `3,6,12`) |
| `--action-items-only` | Skip sync, analysis and loading; generate every window's to-do items from the database and exit (1 if any window failed) |
| `--action-items-dry-run` | Generate and print every window's to-do items without saving them |

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
  only the latest for each window.
- **Bright Data replacement undecided (ISSUE-03)** — Varun is pulling Amazon
  data manually in the interim; the replacement should become a
  `ReviewSource` implementation.
