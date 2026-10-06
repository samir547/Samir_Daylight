# Daylight Sentiment Pipeline

Multi-source review sentiment pipeline for Daylight Company. Pulls Trustpilot
and Amazon review exports via automated SharePoint sync, classifies every
review's sentiment with an LLM (Groq or Claude), translates non-English
reviews, and loads the results into the Azure SQL Server database that the
Power BI dashboards read from — unattended, end to end, on a single
invocation. Structured as the `daylight_sentiment` package; see `DESIGN.md`
for the full architecture and current known issues.

---

## 1. Overview

The pipeline pulls three review exports (two Trustpilot, one Amazon) from
SharePoint into `Reviews/`, normalizes them into one schema, sends each
review to an LLM for classification, and produces two output files: an
aggregated summary and a per-review detail dump. It's designed to run
unattended over ~2,900 reviews, survive interruptions via checkpointing, and
switch between two LLM providers without any code changes.

## 2. How it works — pipeline stages

```
 ┌─────────────────┐   sync_sharepoint() (if SP_* set, not --skip-sharepoint-sync)
 │  1. SHAREPOINT    │  pattern-matched download of the 3 review exports
 │     SYNC          │  into Reviews/*.xlsx, *.csv
 └─────────────────┘
        │
        ▼
 ┌─────────────────┐   load_reviews()
 │  2. LOAD          │  reads all 3 sources, normalizes to one schema,
 │                    │  assigns a unique uid per review
 └─────────────────┘
        │
        ▼
 ┌─────────────────┐   _load_checkpoint() (always, unless --fresh-start)
 │  3. CHECKPOINT    │  filters out uids already successfully analyzed
 │     FILTER        │  in a prior run
 └─────────────────┘
        │
        ▼
 ┌─────────────────┐   MultiSourcePipeline.process_all_reviews()
 │  4. BATCH &       │  groups pending reviews into type-homogeneous
 │     DISPATCH      │  batches (product / service), fans out across
 │                    │  a thread pool
 └─────────────────┘
        │
        ▼
 ┌─────────────────┐   GroqSentimentAnalyzer / ClaudeSentimentAnalyzer
 │  5. LLM CALL      │  sends the batch prompt, parses JSON response,
 │                    │  retries dropped/failed items individually
 └─────────────────┘
        │
        ▼
 ┌─────────────────┐   MultiSourcePipeline._record()
 │  6. CHECKPOINT    │  appends each completed result to
 │     WRITE          │  multi_source_checkpoint.jsonl as it finishes
 └─────────────────┘
        │
        ▼
 ┌─────────────────┐   AnalysisAggregator + OutputGenerator
 │  7. AGGREGATE &   │  computes sentiment distributions, top themes,
 │     WRITE OUTPUT  │  cross-source comparisons; writes final JSON
 └─────────────────┘
        │
        ▼
 Trustpilot_Result/multi_source_sentiment_analysis.json   (summary)
 Trustpilot_Result/multi_source_sentiment_per_review.json (per-review detail)
        │
        ▼
 ┌─────────────────┐   load_database() (if DB_* set, not --skip-db-load)
 │  8. DATABASE      │  upserts both JSON outputs into SQL Server, then
 │     LOAD           │  deletes them once the load is confirmed durable
 └─────────────────┘
        │
        ▼
 ┌─────────────────┐   run_translation_pass() (if DB_* set, not --skip-translation)
 │  9. TRANSLATION   │  offline language detection, then one LLM call per
 │     PASS          │  non-English review; writes review_text_en directly
 │                    │  to the DB row -- a separate concern from sentiment,
 │                    │  reusing the same analyzer, not the same JSON files
 └─────────────────┘
```

Stage 1 is skipped (using whatever's already in `Reviews/`) when `SP_*` env
vars aren't set or `--skip-sharepoint-sync` is passed — see §7.4. Stages 8
and 9 are each independently skippable (`--skip-db-load`, `--skip-translation`)
and both depend on `DB_*` being set at all — see §7.4 and §8.

Step 5 happens continuously during step 4 (not after) — every individual
result is flushed to the checkpoint file as soon as it's done, which is what
makes resuming safe against a mid-run crash or a Groq daily-cap stall.

## 3. Data sources

Three files, three different schemas, normalized into one shape before
anything reaches the LLM. Each arrives via the SharePoint sync (stage 1 in
§2) into `Reviews/`. There is no expected or preferred filename for any of
them — each source is matched purely by `FilePattern` (name tokens +
extension), and `resolve_source_file()` (`infra/review_sources/base.py`)
always picks the newest file in `Reviews/` matching that pattern. A renamed
export — "(2)" suffix, fresh row count, date stamp — is resolved exactly
like any other name; only a pattern with zero matches fails the run.

### 3.1 Trustpilot product reviews — matched by `*trustpilot*product*.xlsx`
Excel, e.g. `Reviews/trustpilot-product-reviews 873.xlsx` (872 rows in the
current export). `load_trustpilot_product()` (`infra/review_sources/trustpilot.py`)
reads columns: `review_id`, `stars`, `product_name`, `product_sku`, `content`,
`consumer_name`, `created_at`.

### 3.2 Trustpilot service reviews — matched by `*trustpilot*service*.xlsx`
Excel, e.g. `Reviews/TrustPilot Service Reviews.xlsx` (1,395 rows in the
current export). `load_trustpilot_service()` (`infra/review_sources/trustpilot.py`)
reads: `Review Id`, `Review Stars`, `Review Title`, `Review Content`,
`Review Username`, `Review Created (UTC)`.
Title and body are concatenated (`"{title}\n\n{body}"`) before being sent to
the LLM, since service review titles often carry the real signal
(e.g. "Great After-Sales Service!").

### 3.3 Amazon product reviews — matched by `*amazon*product*.csv`
CSV, e.g. `Reviews/amazon_product_reviews_all.csv` (672 rows in the current
export). `load_amazon_product()` (`infra/review_sources/amazon_brightdata.py`)
reads: `review_id`, `rating`, `product_name`, `asin`, `review_header`,
`review_text`, `author_name`, `review_posted_date`, `is_verified`.
Two cleanup steps happen here that don't happen for the Trustpilot sources:
- Rows with a non-null `error` column (scrape failures) are dropped and logged.
- Duplicate `review_id`s are dropped, keeping the first occurrence.

### Unified schema
After loading, every review is a dict with at minimum:
`review_id, review_type (product|service), review_source (trustpilot|amazon), rating, text, reviewer_name, review_date, uid`.
`uid` is built as `{source}:{type}:{review_id}` (falling back to `row{index}`
if `review_id` is blank) — this is the checkpoint key and the reason resume
works safely across sources.

## 4. Core logic implemented

### 4.1 Value coercion
`_clean_str`, `_safe_int`, `_safe_bool` handle the fact that pandas represents
missing cells as `NaN`/`NaT` in both numeric and text columns. Anything that
stringifies to `"nan"`/`"nat"`/`"none"` is coerced to an empty string /
`None` rather than leaking into a prompt or a rating average.

### 4.2 Prompt construction — single vs. batch
Two prompt shapes exist for each review type (`PRODUCT_PROMPT` /
`SERVICE_PROMPT` for one review, `PRODUCT_BATCH_PROMPT` /
`SERVICE_BATCH_PROMPT` for many). Batches are built by `_render_batch()`,
which numbers each review (`--- Review 1 ---`, `--- Review 2 ---`, ...) and
truncates each body to `max_review_chars` (default 6,000). The model is asked
to echo the review's numeric `id` in its response so the answer can be mapped
back to the right review.

### 4.3 Batching & response alignment
`_align_batch()` maps a batched LLM response back onto the input list by the
echoed `id`. If the model drops an id, duplicates one, or returns malformed
ids, leftover items are filled into remaining empty slots positionally rather
than discarded. Any review still unmapped after that is retried as an
**individual** (non-batched) call — this is why a batch failure doesn't cost
you the whole batch, only the reviews the model actually mishandled.

### 4.4 Rate limiting — `TokenRateLimiter`
A token-bucket limiter paces Groq calls under a tokens-per-minute ceiling
(`groq_tpm_limit`, default 8,000). It reserves an estimated token cost before
each call and reconciles against the real usage the API reports afterward
(`settle()`), refunding the reservation if a call fails before generating any
tokens (`refund()`). This exists because firing threads at Groq without
pacing just produces 429 storms — the limiter is what lets `max_workers=8` run
without constantly tripping the per-minute cap. Claude doesn't use this
limiter; the Anthropic SDK's own retry handles 429/5xx.

### 4.5 Retry & error handling
- **Per-minute (429):** exponential backoff, capped at 120s, up to `max_retries` (default 4).
- **Per-day (Groq TPD cap):** detected via `_is_daily_cap()` (matches "tokens per day" / "(tpd)" in the error text). This is treated as *not* a failure — the pipeline sleeps (up to 15 min per wait, up to 400 waits) and retries, rather than burning a retry attempt or failing the batch. This is the mechanism that lets a run survive hitting Groq's daily cap unattended, at the cost of the run pausing for however long the cap takes to reset.
- **4xx other than 429/400:** raised immediately (unrecoverable — bad key, bad model, malformed request).
- **Retry-After parsing:** `_retry_after()` reads the header if present, or parses Groq's plain-text wait time from the error body (`"try again in 6m56.448s"` style).

### 4.6 Checkpointing & resume
Every completed result (success or failure) is appended to
`multi_source_checkpoint.jsonl` as one JSON object per line, flushed
immediately. Every run resumes from this file by default — `_load_checkpoint()`
reads it and keeps only rows with a non-null `analysis`; anything that
previously errored is retried, not skipped. Pass `--fresh-start` to ignore it
and reprocess everything. The checkpoint is also filtered to only uids present
in the *current* load of `Reviews/` (`wanted = {r["uid"] for r in all_reviews}`),
so a stale checkpoint entry for data that's since been removed from the source
files won't leak into results. Note that the checkpoint is the only local
record of completed analysis once a run finishes — the two JSON outputs get
deleted after a successful database load (see §7.4).

### 4.7 Aggregation & cross-source comparison
`AnalysisAggregator` groups results by `(review_source, review_type)` and
computes, per group: sentiment counts/distribution, sector or aspect
distribution, average rating, average confidence, and the top 15 themes by
frequency. `cross_source_comparison()` then computes two specific diffs:
Trustpilot vs. Amazon on product reviews (sentiment %, rating, sector-share
deltas), and Trustpilot product vs. Trustpilot service (the only source with
both review types).

## 5. LLM analysis — what's actually being classified

Every review gets four fields back from the model:

| Field | Applies to | Values |
|---|---|---|
| `sentiment` | all | `positive` / `negative` / `neutral` |
| `primary_sector` | product reviews | `needlework`, `art`, `craft`, `beauty`, `reading`, `technical`, `general` — what the customer uses the product **for** |
| `primary_aspect` | service reviews | `shipping_quality`, `customer_service`, `product_quality`, `fulfillment_speed`, `communication`, `returns_refunds`, `warranty`, `general_experience` |
| `confidence` | all | float 0.0–1.0, self-reported by the model |
| `key_themes` | all | up to 6 free-text short phrases |

`_normalise()` enforces this shape on every response regardless of provider:
any sentiment/sector/aspect value outside the fixed vocabulary is coerced to
the catch-all (`neutral` / `general` / `general_experience`), confidence is
clamped to [0, 1], and themes are lowercased and capped at 6. This is what
keeps the aggregate distributions meaningful even if the model occasionally
invents a label.

### Providers

| | Groq (`--provider groq`, default) | Claude (`--provider claude`) |
|---|---|---|
| Model | `openai/gpt-oss-120b` | `claude-sonnet-4-6` |
| Response parsing | native `response_format=json_object` | fence-stripped (`_strip_fences`) then `json.loads` |
| Approx. cost | ~$0.0004/review | ~$0.003/review |
| Daily cap | ~200k tokens/day (free tier) | none |
| Batching | native (`analyze_batch` sends the whole batch in one call) | falls back to the `SentimentAnalyzer` base class's one-call-per-review loop — Claude analyzer doesn't override `analyze_batch` |
| Best for | quick tests, small batches | the full remaining backfill |

Both providers share the exact same prompts, output schema, and checkpoint
format — switching is the `--provider` flag only, nothing else changes.

## 6. Output files

- **`multi_source_sentiment_analysis.json`** — the summary: metadata (provider, model, elapsed time), overall sentiment split, per-source/per-type aggregates, cross-source comparison. This is what you'd hand to Samir.
- **`multi_source_sentiment_per_review.json`** — every review with its full analysis attached, sorted by `uid`. This is what you'd use to spot-check specific reviews or build a dashboard.
- **`multi_source_checkpoint.jsonl`** — append-only run log, one JSON object per line per completed review. Not meant for direct consumption; it's resume state.

## 7. Build, run & test

### 7.1 Build / install

```bash
cd agents\product-review        # from the repository root
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

Create `.env` by copying `.env.example` and filling in real values (LLM API
keys, SharePoint and database settings). `.env` is git-ignored: never commit it
or paste its contents into chat. The shared development values are handed out
privately.

### 7.2 Run

A plain run does all five steps in sequence: pull the 3 review exports from
SharePoint into `Reviews/` (`SP_*` in `.env`), analyse every review (always
resuming from the checkpoint — nothing is reprocessed unless you force it),
load the two JSON outputs into SQL Server (`DB_*` in `.env`), delete those
two JSON files now that they're durable in the database, then translate any
non-English reviews already in the database straight to `review_text_en`
(skip with `--skip-translation`; see §7.4). The checkpoint
(`multi_source_checkpoint.jsonl`) is never deleted by this — it's resume
state, not an output.

```bash
# Smoke test — 20 reviews, either provider
python -m daylight_sentiment.cli --provider claude --limit 20
python -m daylight_sentiment.cli --provider groq --limit 20

# Full run — pulls from SharePoint, analyses (resuming automatically),
# loads to DB, deletes the JSON outputs once loaded
python -m daylight_sentiment.cli --provider claude

# Force a full reprocess, ignoring the checkpoint
python -m daylight_sentiment.cli --provider claude --fresh-start

# Skip analysis entirely -- just load whatever's currently in
# Trustpilot_Result/*.json into the database (and then delete them)
python -m daylight_sentiment.cli --load-db-only

# Tune concurrency / batching if needed
python -m daylight_sentiment.cli --provider claude --workers 4 --batch-size 5
```

Flags: `--provider {groq,claude}`, `--model <override>`, `--limit N`,
`--workers N` (default 8), `--batch-size N` (default 10, `1` disables
batching), `--fresh-start` (ignore the checkpoint), `--skip-sharepoint-sync`,
`--skip-db-load`, `--skip-translation`, `--load-db-only`, `--keep-output-files`
(don't delete the JSON outputs after a successful DB load).

Operator tools live in `scripts/` and are run as modules from the repo root
(the same form as the pipeline itself), e.g.:

```bash
python -m scripts.translate_reviews --dry-run          # translation backfill / audit
python -m scripts.test_prompt_on_review --sku DN1560   # re-run the prompt on one review
python -m scripts.detect_review_languages              # language reconnaissance
python -m scripts.load_asin_sku_map --dry-run          # ASIN -> SKU reference table
python -m scripts.process_varun_amazon_batch --dry-run # one-off Amazon JSON batch
```

Each script's docstring has the full usage; `--help` works on all of them.

Two standalone, read-only diagnostics also live in `scripts/` —
`python -m scripts.check_database_connection` (verifies `DAYHANSA_SQL1`
connectivity and lists table row counts) and
`python -m scripts.check_sharepoint_connection` (verifies the MSAL sign-in
and SharePoint share access). Neither imports from `daylight_sentiment`;
they're meant for checking connectivity independently of the pipeline, e.g.
from a new machine during the VM migration.

### 7.3 Test / validate

A pytest suite lives in `tests/` (run `pytest` from this folder; `pytest.ini`
is already configured). Beyond the unit tests, validate a run by inspecting
its own output:

1. **Smoke test first, always.** Run with `--limit 20` on whichever provider
   you're about to trust for a full run, and manually read a handful of
   entries in `multi_source_sentiment_per_review.json` against the source
   review text — check the sentiment and sector/aspect calls look sane before
   spending on the full 2,939.

2. **Check `failure_reasons` in the summary output**, not just the top-line
   `failed_reviews` count:
   ```bash
   python -c "import json; d=json.load(open('Trustpilot_Result/multi_source_sentiment_analysis.json')); print(json.dumps(d['summary']['failure_reasons'], indent=2))"
   ```
   A handful of "Empty review text" is expected (blank rows in the source
   files). Anything else repeating more than a few times is worth
   investigating — it usually means a batch is consistently getting dropped
   ids from one provider.

3. **Cross-check provider agreement on a shared sample.** Run the same
   `--limit N` slice through both providers to different output files (copy
   `Trustpilot_Result/` aside between runs, or diff the per-review JSON) and
   compare `sentiment` and `primary_sector`/`primary_aspect` agreement. Large
   disagreement on the same reviews is a signal to look at the prompt, not
   just pick whichever provider is cheaper.

4. **Resume correctness.** Interrupt a run partway (Ctrl+C) and rerun it —
   resuming is automatic now, so confirm the log line
   `Resuming: N already analysed, M to go` matches what you expect, and that
   `multi_source_checkpoint.jsonl` doesn't
   have duplicate `uid`s afterward:
   ```bash
   python -c "import json,collections; uids=[json.loads(l)['uid'] for l in open('Trustpilot_Result/multi_source_checkpoint.jsonl',encoding='utf-8')]; dupes=[u for u,c in collections.Counter(uids).items() if c>1]; print(f'{len(uids)} rows, {len(dupes)} duplicate uids')"
   ```

5. **Rating sanity check.** `avg_rating` in the aggregated output should be
   roughly consistent with the raw star ratings in the source files — if it's
   wildly off, that source's column mapping (§3, `infra/review_sources/`)
   has likely drifted from the actual export format (this has already
   happened once, which is why sector/aspect and sentiment are validated
   against `SECTORS`/`ASPECTS`/`SENTIMENTS` rather than trusted blindly).

### 7.4 SharePoint source & database sink

The pipeline's full sequence is: pull from SharePoint → analyse → load to SQL
Server → delete the JSON outputs. Each half is independently optional —
configured via env vars, skipped cleanly if they're absent:

- **SharePoint** (`SP_SHARE_URL`, `SP_USERNAME`, `SP_PASSWORD` in `.env`) —
  `SharePointSync` downloads the 3 review exports into `Reviews/` before
  loading. Auth is MSAL against Microsoft Graph's `/shares` endpoint;
  plain username/password (ROPC) is tried first but this account has MFA
  enforced, so it always falls back to an interactive device-code sign-in
  (prints a `microsoft.com/device` URL + code — a human has to complete it
  in a browser). A successful sign-in is cached to `.sp_token_cache.json`
  (gitignored) so this isn't needed every run. `--skip-sharepoint-sync`
  skips this and uses whatever's already in `Reviews/`.

- **Database** (`DB_SERVER`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_DRIVER`,
  `DB_PORT` in `.env`) — `DatabaseWriter` creates two tables if they don't
  exist (`dbo.sentiment_analysis_reviews`, `dbo.sentiment_analysis_runs`) and
  upserts into them. Reviews are keyed on `uid`, not `review_id` — `review_id`
  is only unique *within* a source (Trustpilot and Amazon can hand out the
  same id), so `uid` is the only column that's actually safe to dedup on.
  Loading the same or an overlapping output file twice never creates
  duplicate rows. `--skip-db-load` skips this; `--load-db-only` does *just*
  this (loads whatever's currently in `Trustpilot_Result/*.json` and exits,
  skipping SharePoint sync and analysis).

  The network path to this SQL Server has been unreliable under sustained
  transfer (`Communication link failure` after a few minutes) — the loader
  works around this by committing in small batches (50 rows) with
  reconnect-and-retry per batch, and by capping `Packet Size=4096` in the
  connection string. If loads start failing again, that's the first thing to
  suspect, not the query logic.

- **Cleanup** — once a database load succeeds, the two JSON files
  (`multi_source_sentiment_analysis.json`, `multi_source_sentiment_per_review.json`)
  are deleted; the database is then their only durable copy. This only
  happens after `load_files()` returns without raising, so a failed load
  never deletes local data. `--keep-output-files` disables this. The
  checkpoint is never touched by this step.

## 8. Known things to watch

- Groq's ~200k token/day cap means a same-day full backfill of the remaining
  ~1,790 reviews should use `--provider claude`, not Groq — see the cost/cap
  table above.
- `max_review_chars` (6,000) truncates unusually long reviews before they
  reach the model — if a specific review's analysis looks off, check whether
  it was truncated.
- The Amazon CSV loader silently drops rows with a populated `error` column
  and duplicate `review_id`s — both are logged (`logger.warning`) but not
  surfaced in the final summary JSON, so check `multi_source_sentiment.log`
  if the Amazon review count looks lower than expected.
