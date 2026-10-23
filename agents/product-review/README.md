# Daylight Sentiment Pipeline

Multi-source review sentiment pipeline for Daylight Company. Pulls Trustpilot
and Amazon review exports via automated SharePoint sync, classifies every
review's sentiment with an LLM (Claude), translates non-English
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
keep the LLM provider behind one interface, so another provider can be added
without touching the pipeline (see §5).

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
 ┌─────────────────┐   ClaudeSentimentAnalyzer
 │  5. LLM CALL      │  one call per review (complete_json), parses the
 │                    │  JSON response; a fatal API error stops the run
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
makes resuming safe against a mid-run crash or a run stopped by a fatal
provider error (§4.5).

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

### 4.2 Prompt construction
Each review type has its own prompt (`PRODUCT_PROMPT` / `SERVICE_PROMPT` in
`domain/prompts.py`), filled with the review text and any context (product
name, star rating). The Claude analyzer sends one review per call.

### 4.3 Batches
The pipeline groups pending reviews into type-homogeneous batches of
`batch_size` (default 10) and hands each batch to a worker thread through
`SentimentAnalyzer.analyze_batch()`. The base implementation, which the
Claude analyzer uses, analyses the batch one review at a time, and each
review succeeds or fails on its own: `analyze_batch()` returns one entry per
review — the analysis, `None` if there was nothing to analyse, or the
exception that failed that review — and raises only a `FatalProviderError`.
`domain/prompts.py` and `domain/normalise.py` also hold a multi-review prompt
(`PRODUCT_BATCH_PROMPT` / `SERVICE_BATCH_PROMPT`, built by
`_build_batch_prompt()`) and `_align_batch()`, which maps a batched response
back onto the input reviews by their echoed `id`. A provider that overrides
`analyze_batch()` to send a whole batch in one call can use them.

### 4.4 Retries
The Anthropic SDK retries rate limits (429), server errors (5xx) and
connection errors itself, with exponential backoff, up to `max_retries`
(default 4) with a `request_timeout` of 30s per attempt. The pipeline does not
add a retry loop on top.

### 4.5 Provider errors
Once the SDK gives up, `classify_anthropic_error()`
(`infra/analyzers/claude_analyzer.py`) maps the exception onto the typed
hierarchy in `infra/analyzers/errors.py`:
- **`FatalProviderError`** — invalid API key (401), billing / credit problem
  (402, or a 400 saying the credit balance is too low), missing permission
  (403) or unknown model (404, check `CLAUDE_MODEL`). Every other call would
  fail the same way, so the run stops: the remaining batches are cancelled,
  the CLI logs the reason and exits with status 1. Reviews that were not
  analysed are not written to the checkpoint, so the next run picks them up.
  The translation pass stops the same way.
- **`ResponseTruncatedError`** — the reply hit the call's output-token
  limit (`stop_reason == "max_tokens"`), so its JSON is incomplete. The error
  names the limit used.
- **`RateLimited`, `TransientProviderError`, `ResponseTruncatedError`,
  `ProviderError`**, and any other error such as an unparseable JSON reply —
  only the affected review (or, in the translation pass, the affected row)
  is marked failed; the other reviews in its batch keep their analyses. The
  failure is logged once at WARNING with the review's uid, and its `error`
  reads `"<ExceptionType>: <message>"` (for example
  `ResponseTruncatedError: Model response hit the output-token limit ...`),
  which is what `failure_reasons` counts. It is retried on the next run.

**Output-token limits.** `complete_json(prompt, max_tokens=None)` uses 500
output tokens by default, which fits the short sentiment JSON. The
translation pass sizes the limit to the text it sends
(`translation_max_tokens()` in `infra/translation.py`): about one token per
two characters plus 200, at least 500 and at most 4,096, so a full
6,000-character review gets 3,200.

### 4.6 Checkpointing & resume
Every completed result (success or failure) is appended to
`multi_source_checkpoint.jsonl` as one JSON object per line, flushed
immediately. Every run resumes from this file by default — `_load_checkpoint()`
reads it and keeps only rows with a non-null `analysis`; anything that
previously errored is retried, not skipped. Because reviews fail
individually, a run in which one review of a batch failed checkpoints the
rest of that batch as done, and the next run analyses only the failed
review. Pass `--fresh-start` to ignore it
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

### Provider and model

| | Claude (`--provider claude`, the default) |
|---|---|
| Model | `claude-sonnet-4-6` by default; set `CLAUDE_MODEL` in `.env` to change it, or pass `--model` for a single run (the flag wins) |
| API key | `ANTHROPIC_API_KEY` |
| Response parsing | fence-stripped (`_strip_fences`) then `json.loads(strict=False)` |
| Approx. cost | ~$0.003/review |
| Calls | one per review (sentiment) and one per non-English review (translation) |

### Adding another LLM provider

Every provider sits behind the `SentimentAnalyzer` interface
(`infra/analyzers/base.py`), so the pipeline, prompts, output schema and
checkpoint format stay the same whichever provider runs. To add one:

1. Add a member to `LLMProvider` in `config.py`, e.g. `OTHER = "other"`. Its
   value is the `--provider` choice and also the name of its settings field
   on `Config`.
2. Add a settings dataclass (`api_key`, `model`, a `from_env()` reading its
   environment variables) and a `Config` field with that name, loaded in
   `Config.from_env()`.
3. Write `infra/analyzers/other_analyzer.py` implementing `SentimentAnalyzer`:
   `complete_json(prompt, max_tokens=None)` (send one prompt, return the
   parsed JSON object; `None` means the provider's default for short output,
   the translation pass passes a larger limit; raise
   `ResponseTruncatedError` if the reply is cut off at the limit),
   `analyze_product_review()` and
   `analyze_service_review()` (shared prompts from `domain/prompts.py`, output
   through `_normalise()`). Optionally override `analyze_batch()` to send a
   whole batch per call (§4.3), keeping its contract: one entry per review
   (analysis, `None`, or that review's exception), raising only
   `FatalProviderError`. Map the SDK's exceptions onto
   `infra/analyzers/errors.py` in one classifier, raising
   `FatalProviderError` for anything that would fail every call.
4. Register the class in `_ANALYZERS` in `infra/analyzers/factory.py`.
   `--provider` picks up the new member automatically.
5. Pin the SDK in `requirements.txt`, regenerate `constraints.txt` (see its
   header), and add its variables to `.env.example`.

## 6. Output files

- **`multi_source_sentiment_analysis.json`** — the summary: metadata (provider, model, elapsed time), overall sentiment split, per-source/per-type aggregates, cross-source comparison. This is what you'd hand to Samir.
- **`multi_source_sentiment_per_review.json`** — every review with its full analysis attached, sorted by `uid`. This is what you'd use to spot-check specific reviews or build a dashboard.
- **`multi_source_checkpoint.jsonl`** — append-only run log, one JSON object per line per completed review. Not meant for direct consumption; it's resume state.

## 7. Build, run & test

### 7.1 Build / install

Supported Python version: **3.12** (the system Python on the Ubuntu 24.04
production VM; the pinned files are tested on Python 3.12.3).

```bash
cd agents/product-review        # from the repository root
python3 -m venv .venv
source .venv/bin/activate       # Linux (Windows: .venv\Scripts\activate)
pip install -r requirements.txt -c constraints.txt
```

Dependencies are pinned to exact tested versions:

- `requirements.txt` — the direct runtime dependencies.
- `requirements-dev.txt` — the runtime dependencies plus `pytest`, for
  running the test suite (§7.3).
- `constraints.txt` — every package, including sub-dependencies, at the
  version tested. Always pass it with `-c` so a rebuild installs the same set.

**System prerequisite.** `pyodbc` needs Microsoft's "ODBC Driver 18 for SQL
Server" installed on the machine; it is an operating-system package, not a
Python package, so pip does not install it. On Linux, check it is installed
with `odbcinst -q -d`, which should list `[ODBC Driver 18 for SQL Server]`. To
install it, follow Microsoft's official instructions:
[Install the Microsoft ODBC driver for SQL Server (Linux)](https://learn.microsoft.com/en-us/sql/connect/odbc/linux-mac/installing-the-microsoft-odbc-driver-for-sql-server).

Create `.env` by copying `.env.example` and filling in real values (the
Anthropic API key, SharePoint and database settings; optionally
`CLAUDE_MODEL`). `.env` is git-ignored: never commit it
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

When SharePoint is configured and none of the three "In Progress" folders
has a new file, the run logs `No new review files waiting in SharePoint;
nothing to do` and exits with code 0 straight after the sync: no analysis,
no output files, no database rows and no translation pass. This makes it
safe to schedule the pipeline more often than new exports arrive.

```bash
# Smoke test — 20 reviews
python -m daylight_sentiment.cli --limit 20

# Same, on a different Claude model for this run only
python -m daylight_sentiment.cli --limit 20 --model <model-id>

# Full run — pulls from SharePoint, analyses (resuming automatically),
# loads to DB, deletes the JSON outputs once loaded
python -m daylight_sentiment.cli --provider claude

# Force a full reprocess, ignoring the checkpoint
python -m daylight_sentiment.cli --provider claude --fresh-start

# Skip analysis entirely -- just load whatever's currently in
# Trustpilot_Result/*.json into the database (and then delete them)
python -m daylight_sentiment.cli --load-db-only

# Tune concurrency / batch size if needed
python -m daylight_sentiment.cli --provider claude --workers 4 --batch-size 5
```

Flags: `--provider {claude}` (default `claude`), `--model <override>`
(overrides `CLAUDE_MODEL`), `--limit N`, `--workers N` (default 8),
`--batch-size N` (reviews per unit of work handed to a worker, default 10),
`--fresh-start` (ignore the checkpoint), `--skip-sharepoint-sync`,
`--skip-db-load`, `--skip-translation`, `--load-db-only`,
`--sync-sharepoint-only` (download the new files into `Reviews/` and stop),
`--keep-output-files` (don't delete the JSON outputs after a successful DB
load).

Exit codes: `0` finished (including "nothing new in SharePoint"), `1` the
Claude API rejected the run (key, billing or model — see §4.5), `2`
SharePoint sign-in is needed (see §7.5).

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
and SharePoint share access, and saves the sign-in to the same
`.sp_token_cache.json` the pipeline reads). Neither imports from `daylight_sentiment`;
they're meant for checking connectivity independently of the pipeline, e.g.
from a new machine during the VM migration.

### 7.3 Test / validate

A pytest suite lives in `tests/`. Install the test dependencies and run it
from this folder (`pytest.ini` is already configured):

```bash
pip install -r requirements-dev.txt -c constraints.txt
python -m pytest
```

Beyond the unit tests, validate a run by inspecting
its own output:

1. **Smoke test first, always.** Run with `--limit 20` on whichever model
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
   investigating — it usually points at a systematic problem (a prompt,
   an input format or the provider) rather than individual reviews.

3. **Cross-check model agreement on a shared sample** before changing
   `CLAUDE_MODEL`. Run the same `--limit N --fresh-start` slice with the
   current and the candidate model (`--model`), copying `Trustpilot_Result/`
   aside between runs, and compare `sentiment` and
   `primary_sector`/`primary_aspect` agreement. Large disagreement on the
   same reviews is a signal to look at the prompt, not just pick whichever
   model is cheaper.

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
  (gitignored; owner-only on Linux) so this isn't needed every run.
  `SP_PASSWORD` must still be set: without it the pipeline treats SharePoint
  as not configured. When the saved sign-in is missing or expired and the
  run has no terminal (a scheduled job), the device-code step is not
  started: the run logs an error and exits with code 2 straight away, before
  anything is analysed, loaded or moved — see §7.5.
  `--skip-sharepoint-sync` skips this and uses whatever's already in
  `Reviews/`.

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

### 7.5 SharePoint sign-in expired - what to do

The pipeline signs in to SharePoint as a person, and that saved sign-in
eventually runs out (for example after a long gap between runs, a password
change or an admin sign-out). A scheduled run can't ask anyone to sign in,
so it stops instead.

**How you can tell.** The scheduled run ends with exit code `2`, and
`multi_source_sentiment.log` has an `ERROR` line starting
`SharePoint sign-in has expired or has not been set up on this machine`.
Nothing has been analysed, loaded or moved; the files are still waiting in
SharePoint.

**What to do.**

1. Open a terminal on the machine that runs the pipeline and go to the
   `agents/product-review` folder (activate `.venv` if the setup uses one).
2. Run:

   ```bash
   python -m daylight_sentiment.cli --sync-sharepoint-only
   ```

   This only signs in and downloads the waiting files into `Reviews/`. It
   doesn't analyse anything, write to the database or move files in
   SharePoint.
3. The terminal shows a box headed `SHAREPOINT SIGN-IN REQUIRED` with a web
   address (usually `https://microsoft.com/devicelogin`) and a short code.
   Open the address in any browser, type the code, and sign in with the same
   SharePoint account as before (the `SP_USERNAME` in `.env`), including the
   MFA prompt. The code is valid for about 15 minutes.
4. Go back to the terminal. It carries on by itself once you've signed in.

**How to confirm it worked.** The command finishes without an error, and the
last log line starts `SharePoint sync:` and lists how many sources had a new
file. The next scheduled run then signs in silently. To process the files
straight away instead of waiting, run the pipeline once by hand
(`python -m daylight_sentiment.cli`).

## 8. Known things to watch

- `max_review_chars` (6,000) truncates review text sent to the translation
  pass. Sentiment calls currently send the full review text (the
  one-review-per-call path does not apply the limit).
- The Amazon CSV loader silently drops rows with a populated `error` column
  and duplicate `review_id`s — both are logged (`logger.warning`) but not
  surfaced in the final summary JSON, so check `multi_source_sentiment.log`
  if the Amazon review count looks lower than expected.
