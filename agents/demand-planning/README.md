# Daylight Demand Planning — Prophet Forecast

A demand-planning forecasting system for **The Daylight Company** (UK lighting
manufacturer — lamps, magnifiers, therapy lights). It trains a
[Facebook Prophet](https://facebook.github.io/prophet/) model for each
**Item × BDM** (Business Development Manager) combination on historical sales,
predicts monthly demand for a held-out validation window and a forward forecast
window, and then **benchmarks the model against the BDMs' own manual forecasts**
(and a naive baseline) to answer one question: *does the AI beat human judgment?*

| | |
|---|---|
| **Active products** | ~250 |
| **Forecastable series** (Item × BDM with ≥ 24 months history) | ~310 |
| **History available** | 101 months (Jan 2018 – May 2026) |
| **Models** | Facebook Prophet (Phase 1).|
| **Output** | CSVs (predictions, accuracy, benchmark) + optional plots + optional write-back to Azure SQL |

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Setup — Virtual Environment](#2-setup--virtual-environment)
3. [Install Dependencies](#3-install-dependencies)
4. [Configuration](#4-configuration)
5. [Running the Script](#5-running-the-script)
6. [Output Files](#6-output-files)
7. [Understanding the Results](#7-understanding-the-results)
8. [Troubleshooting](#8-troubleshooting)
9. [Architecture Reference](#9-architecture-reference)
10. [Data Pipeline](#10-data-pipeline)
11. [Project Structure](#11-project-structure)
12. [.gitignore](#12-gitignore)

---

## 1. Prerequisites

- **Python 3.10 or higher**
- **ODBC Driver 17 for SQL Server** (required by `pyodbc`). Driver 18 also works
  — just match the version in your `.env`.
  - **Windows:** Download and install from Microsoft —
    <https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server>
  - **macOS:**
    ```bash
    brew install microsoft/mssql-release/msodbcsql17
    ```
  - **Linux (Ubuntu/Debian):**
    ```bash
    curl https://packages.microsoft.com/keys/microsoft.asc | sudo tee /etc/apt/trusted.gpg.d/microsoft.asc
    curl https://packages.microsoft.com/config/ubuntu/$(lsb_release -rs)/prod.list | sudo tee /etc/apt/sources.list.d/mssql-release.list
    sudo apt-get update
    sudo ACCEPT_EULA=Y apt-get install -y msodbcsql17 unixodbc-dev
    ```
- **Access to Azure SQL Server** `DAYHANSA_SQL1` with read permissions (and write
  permissions only if you intend to use `--write-db`).

---

## 2. Setup — Virtual Environment

Clone the repo, `cd` into the `prophet_forecast/` folder, then create and
activate a virtual environment.

**Windows (PowerShell):**
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```
> If activation is blocked, run once:
> `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`

**Windows (Command Prompt):**
```bat
python -m venv venv
venv\Scripts\activate.bat
```

**macOS / Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
```

**Verify it's active:** your shell prompt should now be prefixed with `(venv)`.
You can also confirm with `python -c "import sys; print(sys.prefix)"` — the path
should point inside the `venv` folder.

**Deactivate when done:**
```bash
deactivate
```

---

## 3. Install Dependencies

With the virtual environment active:

```bash
pip install -r requirements.txt
```

### Notes on installing Prophet

Prophet compiles a Stan model and needs a C++ toolchain. `cmdstanpy` is pulled in
automatically as a Prophet dependency.

- **Windows:** If `pip install prophet` fails, install the
  [Microsoft C++ Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)
  (select the "Desktop development with C++" workload), then retry. As a
  fallback you can install the older Stan backend first:
  ```bash
  pip install pystan==2.19.1.1
  pip install prophet
  ```
- **macOS:** You may need a compiler first:
  ```bash
  brew install gcc
  pip install prophet
  ```
- **Conda alternative (any OS):** the most reliable route if pip keeps failing:
  ```bash
  conda install -c conda-forge prophet
  ```

---

## 4. Configuration

### 4a. Database credentials (`.env` file)

Copy the template and fill in your credentials:

**Windows:**
```bat
copy .env.example .env
```
**macOS / Linux:**
```bash
cp .env.example .env
```

`.env.example` template:
```env
DB_SERVER=daylight-powerbi-db-1.database.windows.net
DB_NAME=DAYHANSA_SQL1
DB_USER=your_username_here
DB_PASSWORD=your_password_here
DB_DRIVER=ODBC Driver 17 for SQL Server
DB_PORT=1433
```

> ⚠️ **`.env` is git-ignored and must never be committed.** It holds live
> database credentials. Only `.env.example` (with placeholders) is tracked.

### 4b. `config.py` settings

All tunable parameters live in `config.py`. The defaults are correct for the
current POC — change them only deliberately.

**Date windows** (`YYYY-MM` strings)

| Setting | Default | Meaning |
|---|---|---|
| `TRAIN_START` / `TRAIN_END` | `2018-01` → `2025-10` | Training period |
| `TEST_START` / `TEST_END` | `2025-11` → `2026-04` | Held-out validation (model never sees this during fit) |
| `FORECAST_START` / `FORECAST_END` | `2026-05` → `2026-10` | Forward forecast delivered to BDMs |

**Forecasting grain** — `GROUP_COLS` defines what counts as one time series:

| Value | Effect |
|---|---|
| `["item_code", "bdm_name"]` | **Default / recommended** — one model per product × BDM. Required for the BDM benchmark. |
| `["item_code"]` | One model per product (all BDMs combined). Rolls up demand but **disables the BDM benchmark**, which joins on Item × BDM. |
| `[]` | Single total-demand model — testing only; not supported by the benchmark or plotting steps. |

**Prophet parameters** (`PROPHET_PARAMS`)

| Parameter | Default | Why |
|---|---|---|
| `seasonality_mode` | `multiplicative` | Seasonal swings scale with volume |
| `changepoint_prior_scale` | `0.05` | Conservative trend flexibility |
| `yearly_seasonality` | `True` | Strong annual patterns in the data |
| `weekly_seasonality` / `daily_seasonality` | `False` | Data is monthly — not applicable |
| `interval_width` | `0.95` | 95% confidence bands (`yhat_lower` / `yhat_upper`) |

See the [Prophet docs](https://facebook.github.io/prophet/docs/quick_start.html)
for deeper tuning guidance.

**Scope**

| Setting | Default | Meaning |
|---|---|---|
| `MIN_TRAIN_MONTHS` | `24` | Minimum months of history per series (two full yearly cycles, the minimum to detect yearly seasonality). Series below this are skipped. |
| `ACTIVE_SINCE` | `2025-06` | A product is "active" if it's on the BDM sheet **and** has training activity at/after this month. |
| `PILOT_RATING` | `A` | Rating the `--pilot` flag restricts to. |
| `BENCHMARK_START` / `BENCHMARK_END` | `2026-01` → `2026-04` | Overlap between the test window and the BDM forecast year, where Prophet and BDM are compared. |

---

## 5. Running the Script

Run from the `prophet_forecast/` folder with the virtual environment active.

**Basic run** — load from DB, train all ~310 series:
```bash
python main.py
```

**Pilot run** — A-rated products only (~50–80 series). **Recommended for your first run:**
```bash
python main.py --pilot
```

**Cached run** — skip the DB query and reuse previously downloaded data in `output/`:
```bash
python main.py --cached
```

**With component plots** — save trend/seasonality charts per series:
```bash
python main.py --plots
```

**Write results back to Azure SQL** (creates `dbo.forecast_output` /
`dbo.forecast_accuracy` if they don't exist, then appends):
```bash
python main.py --write-db
```

**Combine flags:**
```bash
python main.py --pilot --plots          # Pilot with plots
python main.py --cached --plots         # Cached data with plots
python main.py --cached --write-db      # Cached data, write to DB
```

### CLI flags reference

| Flag | Effect |
|---|---|
| `--pilot` | Restrict scope to A-rated items only |
| `--cached` | Load from cached CSVs in `output/` instead of the database |
| `--plots` | Save Prophet component plots to `output/plots/` |
| `--write-db` | Append results to `dbo.forecast_output` / `dbo.forecast_accuracy` |

### Expected runtime

| Run | Approx. time |
|---|---|
| Pilot (~50–80 series) | 1–2 minutes |
| Full run (~310 series) | 3–5 minutes |
| Add `--plots` | +2–3 minutes |

> The first DB run downloads and caches the raw data to `output/raw_data.csv`
> (plus `bdm_forecasts.csv` and `active_products.csv`). Subsequent `--cached`
> runs skip the database entirely.

---

## 6. Output Files

All written to `output/` (git-ignored).

| File | What it is |
|---|---|
| **`raw_data.csv`** | Cached copy of the training data pulled from the DB. Used by `--cached`. (Also caches `bdm_forecasts.csv` and `active_products.csv`.) |
| **`test_validation.csv`** | Prophet predictions vs actuals for the test window (Nov 2025 – Apr 2026). Columns: `ds, actual, yhat, yhat_lower, yhat_upper, item_code, bdm_name`. |
| **`forecast_may_oct_2026.csv`** | Forward 6-month predictions (May – Oct 2026). Columns: `ds, yhat, yhat_lower, yhat_upper, item_code, bdm_name`. |
| **`model_metrics.csv`** | Per-series accuracy summary. Columns: `item_code, bdm_name, n_train_months, n_test_months, mae, rmse, mape_pct, wape_pct, bias_pct`. |
| **`benchmark_comparison.csv`** | Prophet vs BDM vs Naive for Jan – Apr 2026. Columns: `item_code, bdm_name, bdm_code, region, rating, month, actual, prophet_forecast, bdm_forecast, naive_forecast, prophet_mape, bdm_mape, naive_mape, winner`. |
| **`plots/`** | Prophet component plots (trend + seasonality decomposition) per series — only written with `--plots`. |

---

## 7. Understanding the Results

**Key metrics**

- **MAPE** (Mean Absolute Percentage Error) — `|actual − forecast| / actual`,
  averaged across the test months. **Lower is better.** Rough guide:
  - `< 20%` — good for A-rated products
  - `< 30%` — acceptable for B/C-rated products
  - Rows where the actual is zero are excluded (MAPE is undefined there).
- **WAPE** (Weighted APE) — `Σ|actual − forecast| / Σ actual`. More robust than
  MAPE when some months have small or zero actuals.
- **Bias** — `Σ(forecast − actual) / Σ actual`. Positive = the model
  systematically **over**-forecasts; negative = **under**-forecasts.

**`winner` column** (in `benchmark_comparison.csv`) — for each series-month, the
method with the lowest MAPE among the three. `prophet` means the model beat the
BDM's manual forecast (and the naive baseline) for that series-month. The console
summary aggregates this into win-rates overall and by rating, region, and BDM.

**`yhat_lower` / `yhat_upper`** — the 95% confidence interval. If actuals
generally fall inside this band, the model's uncertainty is well-calibrated; if
actuals routinely fall outside it, the model is over-confident.

---

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| **`ODBC Driver 17 for SQL Server not found`** | Install the driver (see [Prerequisites](#1-prerequisites)). If you installed Driver **18**, set `DB_DRIVER=ODBC Driver 18 for SQL Server` in `.env`. |
| **Prophet installation fails on Windows** | Install [Microsoft C++ Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/) ("Desktop development with C++"), or use `conda install -c conda-forge prophet`. |
| **`DB_NAME is not set`** | You haven't created `.env`. Copy `.env.example` → `.env` and fill in the credentials. |
| **`Login failed for user`** | Wrong credentials in `.env`, or the Azure SQL firewall is blocking your IP. Add your IP under the server's *Networking* settings. |
| **`No models were fitted`** | Check `MIN_TRAIN_MONTHS` (should be `24`) and that `GROUP_COLS` includes `item_code`. With `--pilot`, confirm A-rated items actually have ≥ 24 months of history. |
| **Memory issues on large runs** | Run `--pilot` first, or narrow the date window / scope in `config.py`. |
| **Prophet convergence warnings** | Usually harmless — the model still produces valid predictions. They are suppressed by default. |

---

## 9. Architecture Reference

For the full system design — data model, training-data SQL, BDM allocation
approach, and the Phase 2 roadmap (Azure AutoML, Amazon integration, business-event
adjustments) — see:

**`daylight-demand-planning-agent-architecture-v4.html`**

---

## 10. Data Pipeline

The Python script does **not** rebuild series from raw invoices on every run. It
reads from a pre-built, validated staging table.

```
INVOICES_TEMP (329,810 invoice rows, Jan 2018 – May 2026)
        │  enriched with:
        │    • CUSTOMERS  → BDM attribution (~95% coverage)
        │    • Territory  → BDM codes
        │    • BDM        → authoritative region
        ▼
dbo.forecast_training_data  (49,269 rows; grain: Item × BDM × Region × Month)
        │  refreshed every 2 months in production (DROP + recreate)
        ▼
main.py  →  reads forecast_training_data + BDM forecasts + active-product list
```

- **Source of truth for training:** `dbo.forecast_training_data` (clean,
  pre-aggregated). The script reads this directly — **not** raw `INVOICES_TEMP`.
- **BDM benchmark forecasts:** `dbo.BDM` (manual monthly forecasts for 2026,
  unpivoted from wide Jan–Dec columns to one row per month).
- **Active-product allow-list:** items on the BDM sheet that still have recent
  trading activity (`year_month >= 2025-06`).

---

## 11. Project Structure

```
prophet_forecast/
├── .env                          # DB credentials (NOT committed)
├── .env.example                  # Template for .env
├── requirements.txt              # Python dependencies
├── config.py                     # Configuration (dates, params, scope)
├── main.py                       # Entry point / orchestration
├── data/
│   ├── queries.py                # SQL queries as named constants
│   └── loader.py                 # DB connection, loading, validation, write-back
├── preprocessing/
│   ├── scope.py                  # POC scope filtering (active / forecastable / pilot)
│   └── prepare.py                # Date conversion, aggregation, metadata map
├── models/
│   └── prophet_model.py          # Prophet training + prediction (per series)
├── evaluation/
│   ├── metrics.py                # MAPE, WAPE, Bias, MAE, RMSE
│   ├── benchmark.py              # Prophet vs BDM vs Naive comparison
│   └── plots.py                  # Component plots (reuses fitted models)
└── output/                       # Generated outputs (git-ignored)
    ├── raw_data.csv
    ├── test_validation.csv
    ├── forecast_may_oct_2026.csv
    ├── model_metrics.csv
    ├── benchmark_comparison.csv
    └── plots/
```

---

## 12. .gitignore

The following must stay out of version control:

```gitignore
.env
output/
venv/
__pycache__/
*.pyc
.DS_Store
```
