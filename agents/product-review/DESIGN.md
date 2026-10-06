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
    config.py               Settings dataclasses (Groq/Claude/SharePoint/DB/
                            SourceFiles/Output/Tuning) + Config.from_env().
                            Config() is pure defaults, zero env access;
                            from_env() is the ONE place .env/os.environ is read.
                            Flat legacy names (config.db_server, ...) still work
                            via an alias layer.
    logging_setup.py        configure_logging() -- the one logging config
    domain/                 pure logic, no I/O, depends on nothing in-tree
        taxonomy.py           SECTORS / ASPECTS / SENTIMENTS
        prompts.py            SENTIMENT_GUIDANCE, prompts, _build_context, ...
        normalise.py          _normalise, _sanitise_sector_label, _strip_fences,
                              _align_batch
    infra/                  external I/O; depends only on config + domain
        review_sources/       ReviewSource protocol + per-source loaders
                              (trustpilot.py, amazon_brightdata.py), coercion
                              helpers, resolve_source_file(), load_reviews()
        sharepoint_sync.py    SharePointSync, pattern-matched (see §7)
        db/                   connect(), schema MIGRATIONS registry,
                              ReviewRepository, RunRepository
        analyzers/            SentimentAnalyzer ABC, TokenRateLimiter,
                              errors.py (typed provider errors),
                              groq_analyzer.py, claude_analyzer.py, factory.py
        translation.py        run_translation_pass() + SettingsWriter (see §5a)
    application/            orchestration
        pipeline.py           MultiSourcePipeline
        aggregator.py         AnalysisAggregator
        output_generator.py   OutputGenerator
        checkpoint.py         load_checkpoint() + CheckpointWriter
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

4. if args.load_db_only:
       load_database(config, existing JSON)
       delete_analysis_outputs(config)   [unless --keep-output-files]
       return                                      <-- early exit
       # NOTE: deliberately does NOT run the translation pass (documented gap;
       # run `python -m scripts.translate_reviews` manually if needed after this path)

5. sync_sharepoint(config)                         [if SP_* set, not --skip-sharepoint-sync]
       pattern-matched download of the 3 exports into Reviews/ (see §7)

6. all_reviews = load_reviews(build_default_sources(config.sources))
       each ReviewSource fetches + normalises; uid = f"{source}:{type}:{review_id}"

7. analyzer = build_analyzer(config)               -> Groq | Claude analyzer

8. done = resume_state(config, args, all_reviews)
       checkpoint replay (successful rows only), restricted to loaded uids,
       then apply_rerun_flags() reopens product/service reviews if requested

9. results = MultiSourcePipeline(config, analyzer).process_all_reviews(...)
       type-homogeneous batches of 10, ThreadPoolExecutor(max_workers=8);
       dropped reviews retried individually; every result appended to the
       checkpoint (CheckpointWriter, flushed per record) DURING the run

10. write_outputs() -> Trustpilot_Result/*.json    (summary + per-review detail)

11. load_database()                                [if DB_* set, not --skip-db-load]
        ensure_schema()          -- idempotent MIGRATIONS registry
        ReviewRepository.upsert() -- MERGE per 50-row batch, keyed on uid
        RunRepository.upsert()    -- DELETE+INSERT keyed on run_timestamp
    delete_analysis_outputs()                      [unless --keep-output-files]
        # only after load_database() returned without raising

12. run_translation(config, analyzer)              [if DB_* set, not --skip-translation]
        infra.translation.run_translation_pass(): offline language detection,
        then LLM translation of fr/de/it/es/nl/sv/pl/cs rows where
        review_text_en IS NULL; writes review_text_en + detected_language
```

**Key invariants:**
- Step 11's delete only runs after the DB load returns without raising —
  nothing is destroyed before it's confirmed durable somewhere else.
- Rows with `analysis IS None` (failed LLM calls) are never upserted and never
  trusted from the checkpoint — a failed rerun can't blank out good data.
- Translation state (`review_text_en IS NULL`) is independent of the sentiment
  checkpoint in both directions.

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

| | Groq (`--provider groq`, default) | Claude (`--provider claude`) |
|---|---|---|
| Model | `openai/gpt-oss-120b` | `claude-sonnet-4-6` |
| Cost/review | ~$0.0004 | ~$0.003 |
| Daily cap | ~200k tokens (free tier) | none |
| Batching | native, 1 call per batch of 10 | falls back to 1 call per review |

`_align_batch()` maps a batched response back onto inputs by an echoed `id`;
anything the model drops, duplicates, or mangles is retried individually.

Both analyzers parse model JSON with `strict=False` (2026-09-17): raw control
characters inside string values — seen on some translation responses — no
longer fail the call.

**Typed provider errors (`infra/analyzers/errors.py`, 2026-09-17):**
`classify_groq_error()` is the one place Groq's error strings/status codes are
parsed, emitting `DailyCapExceeded` / `RateLimited` / `TransientProviderError`
/ `FatalProviderError`. The retry loop branches on type: daily-cap waits don't
consume retry attempts; fatal 4xx re-raises immediately; the rate-limiter
reservation is settled (not refunded) only when the call was actually charged
(400 json_validate_failed).

### 5a. Translation pass (`infra/translation.py`, added 2026-09-12)

Runs at the end of every regular pipeline run (skip with
`--skip-translation`): offline language detection (langdetect, zero API cost,
5-word minimum to avoid short-text false positives), then one LLM call per
confirmed fr/de/it/es/nl/sv/pl/cs review, writing `review_text_en` +
`detected_language` via a narrow two-column UPDATE that structurally cannot
touch sentiment data. Resumable via `review_text_en IS NULL` — no checkpoint
file. `python -m scripts.translate_reviews` is the standalone/backfill entry.

`run_translation_pass()` is duck-typed on purpose: callers pass in an analyzer
(needs `._call(prompt) -> dict`) and a writer (needs `._connect()`), so whichever
provider a run already built is reused with no second API client.
`SettingsWriter(config.db)` is the stock writer. The module lived at the repo
root as `translation_lib.py` until 2026-09-17, kept dependency-free to avoid a
cycle with the old monolith (ISSUE-08); it now imports `REVIEWS_TABLE` from
`infra.db` instead of duplicating it.

## 6. Checkpointing & resume

`multi_source_checkpoint.jsonl` gets one JSON line per completed review,
flushed immediately (`CheckpointWriter`). `load_checkpoint()` keeps only rows
with a non-null `analysis` (failures retry) and the *last* record per uid
(rerun flags append superseding entries).

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

**Auth path (unchanged):** ROPC first (fails on this account — MFA), then
MSAL device-code flow, token cached to `.sp_token_cache.json`. A truly
unattended run only works until the cached token expires; durable fix is an
IT app registration with application-level `Sites.Read.All`.

## 8. Database load (`infra/db/`)

Target: `daylight-powerbi-db-1.database.windows.net` / `DAYHANSA_SQL1`.
`connection.connect()` is the public connection factory (Packet Size=4096 and
the 120s statement timeout — see Network resilience). `ensure_schema()`
applies an ordered, idempotent, append-only `MIGRATIONS` registry (both
CREATEs + the guarded `model_number` / `review_country` ALTERs). New schema
changes are appended to that registry.

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

## 9. Testing

`python -m pytest` — offline, no API/DB access, ~1.5s. Characterization tests
pin the domain functions, checkpoint semantics, MERGE column-safety, loader
mappings (synthetic files), pattern matching, provider-error classification,
and the CLI phase functions. Written as the safety net for the 2026-09-17
refactor; keep them green.

## 10. CLI flags

| Flag | Effect |
|---|---|
| `--provider {groq,claude}` | LLM backend (default `groq`, or `$LLM_PROVIDER`) |
| `--model` | Override the provider's default model |
| `--limit N` | Smoke-test on the first N reviews |
| `--workers`, `--batch-size` | Tuning (defaults 8 / 10, now defined in Config only) |
| `--fresh-start` | Ignore the checkpoint; reprocess every review |
| `--rerun-product-reviews` | Reopen only product reviews (both sources) for reprocessing |
| `--rerun-service-reviews` | Reopen only service reviews; combine with the above to reopen all |
| `--skip-sharepoint-sync` | Use whatever's already in `Reviews/` |
| `--skip-db-load` | Analyze and write JSON, but don't load the DB (or delete JSON) |
| `--skip-translation` | Don't run the translation pass |
| `--load-db-only` | Skip sync + analysis; load existing JSON and exit (no translation) |
| `--keep-output-files` | Load the DB as normal, but don't delete the JSON afterward |

## 11. Known issues

- **Groq's ~200k token/day cap** — use `--provider claude` for a same-day full backfill.
- **`max_review_chars` (6,000)** truncates long reviews before the model sees them.
- **Amazon CSV loader** silently drops scrape-error and duplicate rows —
  check `multi_source_sentiment.log` for `WARNING` lines if the count looks low.
- **SharePoint token expiry** — periodic device-code re-auth needed; not
  transparent to a headless/scheduled run.
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
- **Bright Data replacement undecided (ISSUE-03)** — Varun is pulling Amazon
  data manually in the interim; the replacement should become a
  `ReviewSource` implementation.
