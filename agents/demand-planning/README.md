# Demand Planning Agent — Monthly Item Forecast

Forecasts monthly unit demand for **The Daylight Company** (UK lighting
manufacturer: lamps, magnifiers, therapy lights) at **item-code** grain, six
months ahead, and writes the result to Azure SQL for Power BI.

It is a hybrid forecaster: each item is classified by its demand pattern and
routed to the model that suits it (Facebook Prophet, or one of two trailing
averages). Items with too little history or no usable signal get **no forecast**
by design, and the reason is recorded.

> **Read this first if you are taking the project over:**
> [`HOW_IT_WORKS.md`](HOW_IT_WORKS.md) explains the design, why each model was
> chosen, what is good and bad about the data, and the **open issues**.
> This README covers setup, running, operating and the outputs.

---

## At a glance

| | |
|---|---|
| **Scope** | Non-Amazon sales (direct / wholesale / distributor channels). **Amazon is excluded** — see [Scope and caveats](#scope-and-caveats). |
| **Grain / horizon** | One forecast per `item_code` per month, 6 months ahead (BDM is *not* a modelling dimension) |
| **History used** | Jan 2018 onward, monthly |
| **Models** | Prophet (Smooth / Erratic demand with ≥ 24 months history) · Naive-3mo and Naive-12mo trailing averages (Lumpy / Intermittent demand) · *Blocked* (no forecast) |
| **Latest run** (anchor month 2026-09) | 202 items forecast (Prophet 154 · Naive-3mo 23 · Naive-12mo 25); 64 of 266 routed items blocked (24%) |
| **Source of truth** | `dbo.forecast_training_data` (rebuilt from `dbo.INVOICES_TEMP` by this repo) |
| **Destination** | `dbo.forecast_output`, `dbo.forecast_accuracy`, view `dbo.vw_forecast_output_latest` → Power BI |
| **Runs** | Monthly, unattended, via `run_forecast_cycle.sh` (Linux VM + cron) |
| **Stack** | Python 3.10+, Prophet, pandas, SQLAlchemy + pyodbc, Azure SQL |

### Accuracy in one paragraph

Scored on a held-out six-month window against a "same month last year" baseline
(like-for-like, 895 item-months): Prophet WAPE **50.9%** vs **62.2%** baseline
(n = 799); Naive-12mo 57.3% vs 59.9%; Naive-3mo 76.5% vs 106.3% (n = 28, low
confidence). The forecast leans about **10% low** overall, as does the baseline.
WAPE is the metric to use — MAPE explodes on low-volume items. Full results and
caveats: [`HOW_IT_WORKS.md`](HOW_IT_WORKS.md#7-evaluation).

### Scope and caveats

- **Amazon sales are excluded.** Rows in `INVOICES_TEMP` for Amazon are *sell-in*
  (bulk replenishment orders to Amazon's warehouses), not consumer demand, and
  they corrupt seasonality learning. The correct Amazon demand signal is Vendor
  Central sell-out, handled by a separate workstream (`agents/amazon-*`). The
  Power BI series from this agent should be labelled **"excl. Amazon"**.
- **The BDM benchmark is currently not like-for-like.** The BDM manual forecast
  sheet includes Amazon volume (≈ 24% of the Apr–Sep 2026 BDM plan); this
  pipeline's actuals and forecasts do not. Do not read the BDM win-rates in
  `benchmark_comparison.csv` as a verdict. The prior-year comparison *is*
  like-for-like. See [open issues](HOW_IT_WORKS.md#9-open-issues-and-challenges).
- **~24% of routed items get no forecast** (retired, too little history, too few
  sales events, or demand that has moved to Amazon). They are listed with the
  reason, not silently dropped.

---

## Documentation map

| Document | Audience | Contents |
|---|---|---|
| `README.md` (this file) | Operators, new developers | Setup, running, monthly operation, configuration, outputs, troubleshooting |
| [`HOW_IT_WORKS.md`](HOW_IT_WORKS.md) | Engineers, analysts, reviewers | Pipeline step by step, model-selection rationale and evidence, data quality, evaluation, **open issues** |
| `database/` | DBAs | Table build SQL, schema, output view, diagnostics |
| Module docstrings | Developers | Every `preprocessing/`, `models/` and `analysis/` file documents its own reasoning and limits at the top — read them before changing behaviour |

---

## 1. Prerequisites

- **Python 3.10+**
- **ODBC Driver 18 for SQL Server** (Driver 17 also works — set `DB_DRIVER`).
  - Ubuntu/Debian: `sudo ACCEPT_EULA=Y apt-get install -y msodbcsql18 unixodbc-dev`
    (after adding Microsoft's package repository — see
    <https://learn.microsoft.com/en-us/sql/connect/odbc/linux-mac/installing-the-microsoft-odbc-driver-for-sql-server>)
  - Windows: <https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server>
  - macOS: `brew install microsoft/mssql-release/msodbcsql18`
- **Azure SQL access** to `DAYHANSA_SQL1` on `daylight-powerbi-db-1.database.windows.net`.
  Read access for forecasting; **write/DDL access** for `--write-db` and for the
  training-table rebuild. The machine's outbound IP must be allow-listed on the
  Azure SQL firewall.
- A C++ toolchain if `pip install prophet` has to compile (Windows: Microsoft C++
  Build Tools; macOS: `brew install gcc`; or `conda install -c conda-forge prophet`).

## 2. Setup

```bash
cd agents/demand-planning
python3 -m venv venv
source venv/bin/activate            # Windows PowerShell: .\venv\Scripts\Activate.ps1
pip install -r requirements.txt

cp .env.example .env                # Windows: copy .env.example .env
# edit .env — see "Configuration" below
```

> **Git Bash on Windows:** the activation script must be *sourced*:
> `source venv/Scripts/activate`.

## 3. Configuration

### 3a. Credentials — environment only

Credentials come from `.env` (git-ignored) or the process environment. **There
are no defaults for `DB_NAME`, `DB_USER` or `DB_PASSWORD`**: a missing value
fails fast with a clear error rather than connecting with a baked-in login.
Offline runs (`--cached`) never touch the database.

| Variable | Required | Default | Notes |
|---|---|---|---|
| `DB_SERVER` | no | `daylight-powerbi-db-1.database.windows.net` | |
| `DB_NAME` | **yes** | — | `DAYHANSA_SQL1` |
| `DB_USER` | **yes** | — | |
| `DB_PASSWORD` | **yes** | — | Never commit. Rotate if ever exposed. |
| `DB_DRIVER` | no | `ODBC Driver 18 for SQL Server` | |
| `DB_PORT` | no | `1433` | |
| `FORECAST_OUTPUT_DIR` | no | `./output` | Where CSVs are written. The cron script sets a per-run folder. |

### 3b. `config.py` — the settings that matter

| Setting | Value | Meaning |
|---|---|---|
| `TEST_END` | hand-set fallback anchor | The *fallback* window for runs **without** `--auto-window`. All other window constants are offsets from it. `--auto-window` overrides it in memory from the data. |
| `TRAIN_END` / `TEST_START` / `FORECAST_START` / `FORECAST_END` | `TEST_END` −6 / −5 / +1 / +6 | Derived — never edit directly |
| `TRAIN_START` | `2018-01` | |
| `GROUP_COLS` | `["item_code"]` | Modelling grain |
| `MIN_TRAIN_MONTHS` | `24` | Calendar months of history Prophet needs (two yearly cycles) |
| `CROSTON_MIN_OCCURRENCES` | `10` | Minimum sales months for the naive routes. **Provisional / unvalidated** — see open issues |
| `ACTIVE_SINCE` | hand-bumped (`2025-06` at last edit) | Active = on the BDM sheet **and** traded at/after this month. **Bump by hand monthly** (keep ≈ 14 months before `TEST_END`) or long-dead items stay in scope |
| `PROPHET_PARAMS` | `yearly_seasonality=3`, multiplicative, `changepoint_prior_scale=0.05`, `seasonality_prior_scale=10`, `interval_width=0.95` | Each deviation from Prophet defaults is justified with evidence in the file's comments |
| `CHANGEPOINT_PRIOR_KNOWN_CHANGEOVER` | `0.25` | Looser trend prior for pooled families with a confirmed changeover date |
| `SB_ADI_THRESHOLD` / `SB_CV2_THRESHOLD` | `1.32` / `0.49` | Standard Syntetos-Boylan cut-offs |
| `NAIVE_WINDOW_MONTHS` / `NAIVE_BAND_RATIOS` | 3 and 12 / empirical p10–p90 ratios | Naive routes |
| `AMAZON_CHANNELS` | `["Amazon"]` | Channel values excluded from training |
| `CHANNEL_MISMATCH_*` | ≥ 90% Amazon share, < 60 non-Amazon units/yr, ≥ 300 total units/yr | Items whose demand has migrated to Amazon get no forecast |
| `BENCHMARK_START` / `BENCHMARK_END` | static default; rolled by `--auto-window` | Window over which the BDM comparison is scored |

## 4. Running

### 4a. Monthly production cycle (what cron calls)

```bash
./run_forecast_cycle.sh
```

It does exactly two things, in order, and stops on the first failure:

1. `python database/build_training_data.py` — drops and rebuilds
   `dbo.forecast_training_data` from `dbo.INVOICES_TEMP`.
2. `python main.py --auto-window --require-roll --write-db` — rolls the
   train/test/forecast windows onto the latest *complete* month, fits and routes
   every item, and appends the result to Azure SQL.

Each run keeps its CSVs in `output/runs/<timestamp>/` and its console log in
`logs/cycle_<date>.log`.

| Exit code | Meaning | Action |
|---|---|---|
| `0` | Cycle complete | — |
| `1` | A step failed (DB error, exception) | Read the log; nothing partial is written (the DB write is one transaction) |
| `2` | Bad flag combination | `--write-db` is refused together with `--item`, `--cached` or `--pilot` so a partial run can never be published |
| `3` | No newer complete month than `config.TEST_END` | Normal if the source table has not been refreshed yet; nothing forecast, nothing written |

Example crontab (06:30 on the 5th, after the upstream invoice refresh finishes —
that refresh is **not** owned by this repo):

```cron
MAILTO=you@example.com
30 6 5 * *  /opt/demand-planning/run_forecast_cycle.sh
```

**Monthly checklist (manual steps that remain):**

1. Confirm `INVOICES_TEMP` has been refreshed for the closed month.
2. Bump `ACTIVE_SINCE` in `config.py` (≈ 14 months before the new anchor).
3. After the run: check the exit code, then run
   `database/diagnostics/forecast_output_checks.sql` in SSMS.
4. Optionally bump `TEST_END` to the new anchor so manual runs default to it.

### 4b. Ad-hoc / development runs

```bash
python main.py                          # fallback window from config.py, loads from the DB
python main.py --auto-window            # roll the window to the latest complete month
python main.py --cached                 # reuse CSVs in output/ (no DB access)
python main.py --pilot                  # A-rated items only (fast validation)
python main.py --plots                  # also save Prophet component plots
python main.py --cached --item A25090 --plots   # one item, fast debugging (repeatable)
python main.py --auto-window --write-db # publish (normally done by the cron script)
python -m analysis.window_proposal      # show the window --auto-window would choose
```

| Flag | Effect |
|---|---|
| `--auto-window` | Derive TRAIN/TEST/FORECAST (and benchmark window) from the newest complete month |
| `--require-roll` | With `--auto-window`: exit 3 instead of re-forecasting if the window did not move |
| `--write-db` | Append to `dbo.forecast_output` / `dbo.forecast_accuracy` (one transaction, schema-checked first) |
| `--cached` | Skip the database; use cached CSVs |
| `--pilot` | A-rated items only |
| `--item CODE` | Fit only these items (repeatable) |
| `--plots` | Save component plots to `output/plots/` |

### 4c. First DB publish / schema changes

`--write-db` creates the output tables if missing, then checks that existing
tables have exactly the expected columns. If an older version of
`dbo.forecast_output` or `dbo.forecast_accuracy` exists with different columns,
the check fails with the offending column names — **drop the old tables once**
and rerun (they are recreated). Application code never auto-runs DDL for views:
apply `database/views/vw_forecast_output_latest.sql` yourself.

## 5. Outputs

### 5a. Files (per run folder)

| File | Contents |
|---|---|
| `forecast_<start>_<end>.csv` | The forward forecast: `ds, yhat, yhat_lower, yhat_upper, item_code, model, family_key, split_method, split_ratio, ratio_basis, family`. File name derives from the window, so it cannot go stale. |
| `test_validation.csv` | Same shape plus `actual` for the held-out test window |
| `model_metrics.csv` | Per-item MAE / RMSE / MAPE / WAPE / bias, `n_train_months`, `n_test_months`, `changepoint_segment`. **Prophet items only** |
| `benchmark_comparison.csv` | Model vs BDM vs prior-year per item-month, with `winner`. *BDM side includes Amazon — see caveats* |
| `model_routing.csv` | Every item's demand category, route and block reason. **Not written by `main.py`** — produce it with `python -m analysis.model_routing_report` |
| `successor_split_ratios.csv` | How pooled family forecasts were split back to item codes (test ratio and forward ratio) |
| `excluded_channel_mismatch.csv` | Items dropped because their demand moved to Amazon, with the figures |
| `excluded_amazon_rows.csv` | Amazon rows removed from training (audit) |
| `region_ambiguity_log.csv` | (item, BDM) pairs that mapped to several regions and were collapsed (written by `database/build_training_data.py`) |
| `unmatched_family_log.csv` | In-scope items with no Product Family |
| `raw_data.csv`, `bdm_forecasts.csv`, `active_products.csv`, `master_product.csv`, `successor_map.csv` | Cached inputs used by `--cached` |

### 5b. Database objects

| Object | Notes |
|---|---|
| `dbo.INVOICES_TEMP` | Source invoice lines. **`Date` is a string** (parsed in the build) |
| `dbo.forecast_training_data` | Item × BDM × Region × Month, rebuilt by `build_training_data.py` (DROP + SELECT INTO) |
| `dbo.BDM` | Manual BDM forecasts (wide Jan–Dec → one row per month); also the active-product list and region authority |
| `dbo.CUSTOMERS`, `dbo.Territory` | BDM attribution and channel |
| `dbo.product_successor_map`, `dbo.product_successor_review` | Retired → successor code pairings and confirmed changeover dates |
| `dbo.[MASTER RODUCT TABLE]` | Product Family (**the table name has a typo in the live database; code uses it as is**) |
| `dbo.forecast_output` | **Append-only**; columns `item_code, family, year_month, yhat, yhat_lower, yhat_upper, model, run_kind, run_date` |
| `dbo.forecast_accuracy` | **Append-only**; `item_code, family, model, mae, rmse, mape_pct, n_train_months, n_test_months, run_date` |
| `dbo.vw_forecast_output_latest` | Rows of the newest `run_date` for the three delivered models. **This is what Power BI reads** ("Forecast Qty (Prophet)", Global – Demand Planning page) |

Because `forecast_output` is append-only and the view takes `MAX(run_date)`, **a
bad batch becomes "the current forecast" immediately.** To roll back, delete
that `run_date`'s rows from `forecast_output` and `forecast_accuracy`.

## 6. Reading the results

- **WAPE** — `Σ|actual − forecast| / Σ actual`. Volume-weighted; use this.
- **MAPE** — mean percentage error; unreliable for small or intermittent
  quantities (an error against an actual of 2 can be thousands of percent).
  Quote the median, never the mean.
- **Bias** — `Σ(forecast − actual) / Σ actual`. Negative = under-forecasting.
- **`model`** column — which forecaster produced the row. Naive-3mo rows are
  low-confidence (small sample, strong under-forecast bias in testing).
- **`yhat_lower` / `yhat_upper`** — 95% interval for Prophet. For the naive
  routes it is a fixed empirical band (p10/p90 ratios), *not* a model-derived
  prediction interval.
- **`n_test_months`** — a MAPE built from 1–3 months is weak evidence.
- **`ratio_basis`** — `forward` for pooled-family items split back to item codes
  using the freshest sales mix; `na` otherwise.

## 7. Repository layout

```
agents/demand-planning/
├── main.py                       # entry point / orchestration
├── config.py                     # all tunables, each with its justification
├── run_forecast_cycle.sh         # the monthly cron entry point
├── requirements.txt, .env.example, .gitignore, .gitattributes
├── data/
│   ├── queries.py                # SQL as named constants
│   └── loader.py                 # connection, loading, schema-checked transactional write
├── preprocessing/
│   ├── prepare.py                # aggregation to item × month
│   ├── scope.py                  # active / forecastable / Amazon / channel-mismatch filters
│   ├── family_pool.py            # successor-family pooling and split-back
│   ├── family.py                 # Product Family reporting tag (unrelated to successor families)
│   ├── demand_classification.py  # Syntetos-Boylan classification
│   ├── model_routing.py          # the routing ladder (Prophet / Naive / Blocked)
│   └── feature_extraction.py
├── models/
│   ├── prophet_model.py          # per-series Prophet fit and prediction
│   └── naive_model.py            # trailing-average routes
├── evaluation/
│   ├── metrics.py                # MAE / RMSE / MAPE / WAPE / bias
│   ├── benchmark.py              # model vs BDM vs prior-year
│   └── plots.py
├── analysis/                     # window selection, completeness, diagnostics, experiments
├── database/
│   ├── forecast_training_data.sql, build_training_data.py
│   ├── schema.sql, views/, diagnostics/, migrations/, seed/, tables/
└── HOW_IT_WORKS.md
```

`analysis/` scripts are read-only diagnostics and experiments (they change no
config and write no database rows). `month_completeness.py` and
`window_proposal.py` are the exceptions in that **production calls them**.
Several experiment scripts read cached CSVs from `output/` and document their
conclusions in their own docstrings. There is no automated unit-test suite for
this agent; correctness has been established by the experiments, run-to-run
reconciliation checks and manual review (see open issues).

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `DB_NAME / DB_USER / DB_PASSWORD is not set` | `.env` missing or incomplete; copy `.env.example` |
| `Can't open lib 'ODBC Driver 18…'` | Install the driver, or set `DB_DRIVER` to the version you have |
| `Login failed` / connection timeout | Wrong credentials, or this machine's IP is not on the Azure SQL firewall |
| `ModuleNotFoundError: sqlalchemy` | Wrong Python — activate the venv first (`source venv/bin/activate`) |
| Exit code 3 | No new complete month. Check that `INVOICES_TEMP` was refreshed and that the build step ran |
| Exit code 2 | `--write-db` used with `--item/--cached/--pilot`, or `--require-roll` without `--auto-window` |
| Schema check fails on `forecast_output` | Old table layout — drop it once (see 4c) |
| `model_metrics.csv not found` warning from model routing | Diagnostic only. Occurs because each cycle writes to a fresh run folder; it only affects the "currently fitted" cross-check column |
| Prophet install fails | Install a C++ toolchain, or use conda-forge |
| Convergence warnings | Usually harmless; suppressed by default |
| A forecast looks near zero for an item that is clearly selling | Run `python -m analysis.model_routing_report` and check `successor_split_ratios.csv`: it is usually a pooled successor family whose split gave the item ~0 share (see open issues) |

## 9. Security and handover checklist

- Credentials are environment-only; `.env` is git-ignored. **Rotate the database
  password before go-live** if it has ever been in a repository, ticket or chat.
- Use a dedicated SQL login with only the rights needed (read on source tables;
  create/insert on the two output tables; drop/create on `forecast_training_data`).
- The VM's outbound IP must be allow-listed on the Azure SQL firewall.
- Confirm who owns the upstream `INVOICES_TEMP` refresh — this agent depends on
  it and cannot trigger it.
- Review [open issues](HOW_IT_WORKS.md#9-open-issues-and-challenges) before
  presenting numbers to stakeholders.
