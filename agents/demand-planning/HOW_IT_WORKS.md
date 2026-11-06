# How the Demand Planning Agent Works

Design, rationale, data and open issues for the monthly item-level forecast.
Written so that a team that has never seen this project can operate it, defend
its numbers, and know where it is weak. Operating instructions are in
[`README.md`](README.md).

Figures labelled **"latest run"** come from the development run anchored on
**2026-09** (train to 2026-03, test 2026-04 → 2026-09, forecast 2026-10 → 2027-03).
They will change every month; re-derive rather than trusting them
(`python -m analysis.window_proposal`, and the run's CSVs). The monthly cycle now
runs in production mode, which trains to the anchor (§5.1).

**Contents**

1. [The problem and the shape of the answer](#1-the-problem-and-the-shape-of-the-answer)
2. [Architecture and data flow](#2-architecture-and-data-flow)
3. [Data sources](#3-data-sources)
4. [The data: what is good, what is bad, how it is handled](#4-the-data-what-is-good-what-is-bad-how-it-is-handled)
5. [The pipeline, step by step](#5-the-pipeline-step-by-step)
6. [Why these models: the evidence](#6-why-these-models-the-evidence)
7. [Evaluation](#7-evaluation)
8. [Operating model and failure modes](#8-operating-model-and-failure-modes)
9. [Open issues and challenges](#9-open-issues-and-challenges)
10. [Glossary](#10-glossary)

---

## 1. The problem and the shape of the answer

Daylight sells roughly 250 active products (item codes) through regional teams
(UK, EU, USA, Australia). Each Business Development Manager (BDM) produces a
manual yearly forecast per product. The goal is an **automated, repeatable
monthly forecast** that supplies a statistical view of demand alongside the BDM
view — not to replace the BDMs. The client's stated priority is **volume-weighted
accuracy (WAPE)**, not winning item-by-item against the BDMs.

What the system produces: for each forecastable item, six monthly quantities
(`yhat`) with a lower/upper band, tagged with the model that produced them and
the item's Product Family, appended to Azure SQL for Power BI.

What it deliberately does **not** do:

| Not covered | Why |
|---|---|
| Amazon demand | Amazon rows in the invoice data are sell-in (replenishment), not consumer demand. Handled by a separate Amazon forecast workstream on Vendor Central sell-out data. |
| Retired products | No forecast needed; their demand is carried by their successors (see pooling). |
| Items with too little history or too few sales events | Blocked, with a recorded reason, rather than given a number that would be a guess. |
| Per-BDM forecasts | BDM is sales-credit attribution (who earns commission), not a demand driver. Splitting an item's demand by BDM models an accounting split. BDM remains reporting context only. |
| Price, promotion, stock-out or event effects | Not modelled (no such inputs are loaded). |

## 2. Architecture and data flow

```
   dbo.INVOICES_TEMP  (raw invoice lines; refreshed by an EXTERNAL process)
          │  + CUSTOMERS (BDM attribution), Territory (channel, BDM code), BDM (region)
          ▼
   database/build_training_data.py    DROP + rebuild  dbo.forecast_training_data
          │                            (item × BDM × region × month; qty + txn count)
          ▼
   main.py  ───────────────────────────────────────────────────────────────────────
     1  load   training data · BDM forecasts · active products · master product ·
               successor map                                     (cached as CSV)
     2  window --production: latest COMPLETE, fully elapsed month → all windows
               --auto-window: development equivalent (month_completeness)
     3  detect channel-mismatch items (demand moved to Amazon)          → Blocked
     4  drop Amazon-channel rows
     5  aggregate to item × month  (BDM and region dropped from the grain here)
     6  pool retired + successor codes into "families" for training
     7  scope: active list → ≥ 24 months history → (optional) A-rated only
     8  classify each item's demand (Syntetos-Boylan) and route it
               Prophet │ Naive-3mo │ Naive-12mo │ Blocked
     9  fit/compute  Prophet per series · trailing average per series
    10  split family forecasts back to real item codes (two ratios — see §5.7)
    11  tag Product Family; development runs also score the test window and
               compare with BDM and prior-year
    12  write CSVs; --write-db → one transaction (schema check + inserts)
          ▼
   dbo.forecast_output  (append-only, run_date stamped; development runs also
          │              write dbo.forecast_accuracy)
          ├─▶ dbo.vw_forecast_output_latest  (MAX(run_date))  →  Power BI
          ▼
   evaluation/delivered_accuracy.py   next cycle, once actuals have arrived
          ▼
   dbo.forecast_delivered_accuracy  →  dbo.vw_forecast_delivered_accuracy
```

Everything between steps 6 and 10 that involves "families" is a **detour around
the fit**: before step 6 and after step 10 the data is plain item-code grain, so
scope filtering, benchmarking and the CSV writers never see the machinery.

## 3. Data sources

| Source | Used for | Notes |
|---|---|---|
| `dbo.INVOICES_TEMP` | Raw sales | Over 330,000 invoice lines (mid-2026). **`Date` and `Qty` are strings.** Refreshed by a process outside this repo. |
| `dbo.CUSTOMERS` | BDM attribution (customer → BDM) and channel | ~95% of invoice lines attribute to a BDM; the rest become `Unattributed` |
| `dbo.Territory` | BDM code, channel (`Amazon` etc.) | `CUSTOMERS.BDM = Territory.Department` |
| `dbo.BDM` | BDM manual forecasts (wide Jan–Dec per planning year), rating (A–E, N), region, and the active-product list | Holds more than one planning year at once (2026 and 2027) |
| `dbo.[MASTER RODUCT TABLE]` | Product Family (reporting tag) | Table name is misspelled in the live DB |
| `dbo.product_successor_map` / `product_successor_review` | Old → new code pairings, status, confirmed changeover date | Many-to-many; see §5.5 |
| `dbo.forecast_training_data` | **The modelling input**, built by `database/forecast_training_data.sql` | ~51k rows, ~740 item codes, Jan 2018 → current month |

Item codes are filtered at build time to the product prefixes `DN`, `A`, `D`,
`E`, `U` (excluding `DISCOUNT`, `Shipping Charge`, `SHIP` and non-positive
quantities). The first letter encodes the region (D = UK, E = EU, U = USA,
A = Australia).

## 4. The data: what is good, what is bad, how it is handled

### 4.1 What is good

- **Long, consistent history.** Monthly data from Jan 2018 — about eight and a
  half years, enough for two or more full yearly cycles on most mature items.
  This is what makes seasonal modelling possible at all.
- **A seasonal pattern worth modelling.** There is a repeatable annual shape on
  regular items, including a hard Apr–Jun trough (genuine months in that
  trough routinely run at 52–70% of their own trailing median). Prophet's yearly
  seasonality earns its keep on those items.
- **Channel is tagged.** Amazon rows carry `channel = 'Amazon'` from the
  Territory table across all Amazon BDM codes, so exclusion needs no per-code
  list and self-maintains as codes are added.
- **Good BDM attribution coverage (~95%)** and an authoritative region source.
- **Successor map exists.** Product changeovers are recorded, with status and (for
  the forecast-relevant families) confirmed changeover dates, which allows
  demand continuity to be recovered across a code change.
- **A manual benchmark exists** (the BDM sheet), including ratings, for the same
  items.

### 4.2 What is bad, and what the pipeline does about it

| Problem | Effect if ignored | Handling | Residual risk |
|---|---|---|---|
| **`INVOICES_TEMP.Date` is a string** with inconsistent formats | Wrong months, silent loss of rows | Build parses the last 10 characters as day/month/year (style 103), falling back to ISO. Same rule as the Power BI model. | Strings that fit neither parse become NULL months. Check for unparseable dates after each upstream refresh. |
| **The newest month is usually partial** (the upstream pull runs part-way through a month) | Training on a fabricated cliff at the most recent point — exactly where trend fitting is most sensitive | `analysis/month_completeness.py` compares each month with the same month a year earlier, normalised for growth (a trailing-median test fails because Apr–Jun is a genuine trough). A month below a 0.60 ratio is treated as incomplete and the window anchors on the previous month. Production also refuses any month that has not ended. | The threshold is calibrated on one known partial month (0.46) vs the worst genuine month (0.70). With transactions running well above last year it is loose: a month about 45% filled would pass today. An elapsed month can still be short through late invoices: the extract dated 2026-08-29 had 2026-08 at 12,264 units against a final 13,512. A revised anchor is picked up by the next cycle's refit and by delivered scoring; the batch already delivered stands. |
| **Amazon sell-in mixed with consumer sales** | Lumpy replenishment orders corrupt seasonality | Rows with `channel='Amazon'` removed before aggregation (logged to `excluded_amazon_rows.csv`). | **Those units are invisible to this forecast**, and the BDM sheet still counts them (§9, issue 1). |
| **Items whose demand migrated to Amazon** (e.g. U35108: 99.5% Amazon, 13 non-Amazon units of 2,597 in 12 months) | Prophet fits a trend to a handful of units and predicts from a pre-migration baseline (9,794% MAPE seen) | `detect_channel_mismatch()`: excluded when Amazon share ≥ 90% **and** non-Amazon volume < 60/yr **and** total volume ≥ 300/yr. The third condition stops it suppressing genuinely small or declining products. Excluded items get no forecast and are listed with their figures. | Items just under the thresholds still get a forecast on thin data. |
| **Product changeovers split history across codes** | A successor starts from zero history and fails the 24-month rule although the *demand* is years old | Successor-family pooling (§5.5) | Depends on map quality and on correct split-back (§9, issue 9). |
| **Ambiguous region** (the same item/BDM pair appears under two regions in `dbo.BDM`; only BDM 'SW' on a few SKUs) | A join fan-out inflated those series exactly 2× | Region lookup collapsed to one row per (item, BDM) using `MIN(Region)`; every collapsed pair is logged to `region_ambiguity_log.csv` | The chosen region is a **resolved ambiguity, not a verified fact**. Forecasts are unaffected (region is not a model input) but region-level reporting needs business confirmation. |
| **Unattributed sales (~5%)** | None for forecasting | Grain has no BDM, so these are fully included | — |
| **Erratic and intermittent demand** | MAPE in the thousands of percent; Prophet seasonality fitted to noise | Demand classification and routing (§5.6); WAPE as primary metric | Intermittent items remain inherently hard to forecast |
| **Short and uneven history** | Seasonal fit on < 4 cycles can overfit | 24-month minimum; low Fourier order (§6.1); history-length experiment (§6.5) | 24% of fitted items are scored on < 6 test months |
| **Product Family missing for ~34% of the wider master table** (mostly Australian A-prefix SKUs) | Gaps in grouped reporting | Tagged `Unknown`, never dropped or guessed; coverage within the fitted scope is logged | Reporting by family is incomplete for some SKUs |
| **Rating "N"** appears on the BDM sheet beside A–E; meaning undocumented | Unclear reporting dimension | Carried through unchanged | Needs a definition from the client before rating is used in client-facing reporting |
| **Training-table total differs by +2 units for one month** (June, 1,762 vs 1,760 expected in a reconciliation) | Negligible | Noted | Cause not investigated |
| **Training table is rebuilt with DROP + SELECT INTO** | A concurrent reader waits for the whole rebuild | Both statements run in one transaction, so a failure rolls back to the old table. Run the cycle at a quiet time | The schema lock blocks readers until commit, so a long rebuild can make them time out. They should not see a missing table. Based on SQL Server's behaviour, not tested against the live database. |

### 4.3 Net assessment

The data is **good enough to forecast the regular, established, non-Amazon part
of the range** — which is most of the volume — and **not good enough to forecast
the long tail** (new, intermittent or channel-migrated items) with any
confidence. The pipeline's design principle is therefore *say less rather than
say something wrong*: block, label and log instead of guessing.

---

## 5. The pipeline step by step

### 5.1 The date windows roll; they are not hard-coded

Every window is a fixed offset from one anchor. There are two layouts, shown here
for anchor 2026-09:

| Window | Development | Production (`--production`) |
|---|---|---|
| Training | `2018-01` → anchor −6 (→ 2026-03) | `2018-01` → anchor (→ 2026-09) |
| Test (held-out accuracy check) | −5 → 0 (2026-04 → 2026-09) | None |
| Forward forecast | +1 → +6 (2026-10 → 2027-03) | +1 → +6 (2026-10 → 2027-03) |
| Horizon, last training month to forecast month | 7 to 12 months | 1 to 6 months |

**Production** is what the monthly cycle runs. The test window belongs to choosing
models and settings; in production the models see all the data and accuracy is
tracked by scoring delivered batches as actuals arrive (§7.1). The anchor is the
newest month that passes `analysis/month_completeness.py` **and** has fully
elapsed, and if there is none the run stops with exit 3. Everything anchored at
`TRAIN_END` (classification, `decline_ratio`, the routing gates, the naive
averages, `test_ratio`, the 24-month checks) follows automatically. `TEST_END`
stays equal to the anchor because `prepare()` and the channel-mismatch check use
it; `TEST_START` sits one month after it, which makes the test window empty.

**Development** is for choosing models and settings, and runs quarterly without
publishing (README §4a). The models train only to six months before the anchor, so
those six months can score them. With `--auto-window` the anchor is the newest
complete month, not `MAX(year_month)`, and the BDM benchmark window rolls with it.
`config.TEST_END` is the fallback for runs without `--auto-window` and the
reference `--require-roll` compares against.

### 5.2 Load

Five inputs, each cached to CSV so `--cached` runs never touch the database:
training data, BDM forecasts, active products, master product table, successor
map. `forecast_training_data` is already monthly-aggregated; it is not the raw
invoice table, so quote its row counts separately.

### 5.3 Exclusions

1. **Channel mismatch** is detected *first*, because the Amazon filter below
   removes the rows needed to detect it (§4.2).
2. **Amazon channel rows** are removed (latest run: 6,013 rows across 319 items,
   logged for audit).

### 5.4 Aggregation to the modelling grain

Rows are summed to one row per `item_code` per month. `bdm_code` and `region` are
dropped here. A side lookup keeps item → region purely for reporting.

### 5.5 Successor-family pooling

**Problem.** When a product is replaced, the new code starts at zero history and
fails the 24-month rule even though its demand has a long history under the old
code.

**Solution.** The retired code and its successors are summed into one series for
the fit, then the family forecast is split back to real item codes.

- Families are resolved as **connected components** of the old ↔ new map, because
  the map is many-to-many (13 successor codes are reachable from two retired
  codes). Resolving "one retired code → its successors" would count shared
  history twice and emit two forecast rows for one item.
- A family is eligible only if at least one *current* code is on the active list.
- Pairings with status "Old code still selling" (the changeover has not happened)
  and one explicitly excluded old code (`D35040`, an unconfirmed data issue) are
  not pooled.
- **Two split ratios**, never one: a `test_ratio` from actuals up to `TRAIN_END`
  for anything scored on the test window, and a `forward_ratio` from the freshest
  actuals for the real forecast. Using one ratio for both lets the split "peek" at
  how demand divided during the test months and flatters accuracy (this was found
  as identical bias numbers across all codes of a family and is eliminated by the
  two-ratio design).
- Ratios come from each code's own recent actuals (trailing six months, at least
  three post-changeover months of sales per code). Otherwise the split is equal
  and flagged `split_method = equal_fallback`. Single-successor families are
  assigned the full family forecast (`split_method = na`).
- In production `TRAIN_END` is the anchor, so the two ratios are equal.
- 76 item codes belong to pooled families in the latest run.

*Note: "Product Family" (`family.py`, a reporting label from the master table) is
unrelated to a "successor family" (`family_pool.py`). The code keeps them apart
as `family` and `family_key`.*

### 5.6 Scope, classification and routing

**Scope.** Active-product allow-list → at least 24 months of history before the
test window (judged on the pooled series for pooled items) → optionally A-rated
only (`--pilot`).

**Classification** (`demand_classification.py`) — Syntetos-Boylan on the series
as it is actually fitted (the pooled family series for pooled items), on data to
`TRAIN_END` only:

| | CV² < 0.49 | CV² ≥ 0.49 |
|---|---|---|
| **ADI < 1.32** | Smooth | Erratic |
| **ADI ≥ 1.32** | Intermittent | Lumpy |

ADI = months of tenure ÷ months with sales (1.0 = sells every month); tenure runs
from the first sale, so a recent launch is not penalised for years it did not
exist. CV² is computed over non-zero months. Fewer than four non-zero months is
reported as *Insufficient Data to Classify*, not guessed. Items within 10% of a
cut-off carry `near_threshold = True` — flagged, never re-routed.
`decline_ratio` (mean of the last 24 months ÷ the 24 before) records the trend.

**Routing** (`model_routing.py`). First matching gate wins:

| # | Condition | Result |
|---|---|---|
| 1 | Retired code | Blocked — "retired, no forecast needed" |
| 2 | Not on active list | Blocked — "not currently active" |
| 3 | Channel mismatch | Blocked — "needs Amazon sell-out data" |
| 4 | No sales history | Blocked |
| 5 | Too few months to classify | Blocked — "insufficient history to classify yet" |
| 6 | Smooth / Erratic | **Prophet** if the fit series has ≥ 24 calendar months, else Blocked ("insufficient calendar history") |
| 7 | Lumpy / Intermittent | **Naive-3mo** if `decline_ratio` < 1, otherwise **Naive-12mo**, provided ≥ 10 months with sales; else Blocked ("insufficient sale occurrences") |

Gates 6 and 7 block for temporary reasons and are re-evaluated every run, so
items leave the blocked set on their own as data accrues. An unknown trend
(`decline_ratio` undefined) routes to the 12-month window on purpose.

### 5.7 Models

**Prophet** — one model per fitted series:

```python
Prophet(yearly_seasonality=3, weekly_seasonality=False, daily_seasonality=False,
        seasonality_mode="multiplicative", seasonality_prior_scale=10.0,
        changepoint_prior_scale=0.05,   # 0.25 for known-changeover families
        interval_width=0.95)
```

Each model predicts the test and forward windows in one call; negative values are
clipped to zero. Fitting is about half a second per series.

**Naive routes** — one flat level per series: the mean monthly quantity over the
last 3 or 12 calendar months to `TRAIN_END`, zeros included, applied to every
test and forward month. The band is `level × (0.13, 2.60)` for Naive-3mo and
`level × (0.30, 3.48)` for Naive-12mo — the empirical p10/p90 of
actual ÷ forecast across this population's cross-validation folds. **It is not a
prediction interval** and is not comparable to Prophet's. Naive-12mo's band rests
on thinner evidence (12 of its 22 originally routed items had standalone fold
data; the rest are pooled families the experiment never tested).

**Split-back.** Family forecasts become item-level rows using the ratios of §5.5.
In the test frame each item's `actual` is its *own* observed sales wherever the
code has a row for that month. Where it has none, `actual` falls back to a
ratio-scaled share of the family total, which is an estimate, not a measured fact.
That applied to 12 of 1,212 rows in the latest run (121.8 units against no recorded
sales), so `test_validation.csv` sums slightly higher than the raw actuals.

### 5.8 Benchmarking

Development runs only. On the months where the test window overlaps the BDM sheet, each item-month is
compared across three numbers: the item's model forecast, the BDM manual forecast
**summed across every BDM who forecasts that item**, and a prior-year baseline
(same item, same month a year earlier). The lowest MAPE wins the item-month.
See §7 and open issue 1 for why the BDM leg is currently not like-for-like.

### 5.9 Output and write

CSVs are written per run (README §5a). With `--write-db`, the run first compares
its first forecast month with the latest batch and stops with exit 3 if it is not
later (`--force` overrides). The output tables are then created if absent, their
columns are validated against the expected layout, and the inserts happen in **one
transaction** — a failure leaves nothing behind. Production has nothing to insert
into `forecast_accuracy`. `--write-db` is refused when combined with `--item`,
`--cached` or `--pilot`.

---

## 6. Why these models: the evidence

The guiding rule was that every non-default choice is justified by a
rolling-origin cross-validation run **inside the training window** (so the test
window is never used to choose settings) and that the experiment code stays in
the repo so the decision can be re-checked.

### 6.1 Prophet for regular demand

*Why Prophet:* monthly data with a real yearly cycle plus occasional level shifts
is exactly what a trend + seasonality decomposition handles, it tolerates gaps,
and it produces an interval. It needs at least two yearly cycles to have a
seasonality to find, hence `MIN_TRAIN_MONTHS = 24`.

*Defaults that were changed, and why:*

| Setting | Choice | Evidence |
|---|---|---|
| `yearly_seasonality` | **3** Fourier pairs (default `True` = 10) | The default is 20 free parameters against 6–8 repeats of a yearly cycle; sampled daily it swings −969% to +1,588% around trend on an item like DN1380. Order 3 beat order 10 on **102 / 152** series by CV MAPE, **121 / 152** by CV WAPE, median MAPE −10.2%. One global value, not tuned per item. |
| `seasonality_mode` | multiplicative (kept) | Additive beat it on only 73 / 152 series — a coin flip — and did not reduce the swing. |
| `seasonality_prior_scale` | 10 (kept) | Tighter priors only helped in combination with per-item Fourier order, i.e. per-series tuning, which was out of scope. |
| `changepoint_prior_scale` | 0.05 standard; **0.25** for families with a confirmed changeover date | A trend audit found 38 of the 68 worst-fitted series had zero changepoints clearing the threshold with the fitted trend >30% from recent actuals: the 0.05 prior was regularising a real level shift away. 0.25 beat 0.05 on **19 / 22** known-changeover families (median MAPE −30.7%; 20 / 22 by WAPE) but only 29 / 43 elsewhere — not enough to loosen it everywhere, hence segmentation. 0.25 was preferred to 0.50 (tied at 19 / 22) because 0.50 is the edge of the tested grid and its worst regression was +127% vs +72%. |

*Caveat:* every pooled family currently has a confirmed changeover date, so the
"known changeover" segment is identical to "pooled" — the segmentation is not yet
discriminating within the pooled population.

### 6.2 Trailing averages for Lumpy / Intermittent demand

Prophet has nothing useful to say about a series that is mostly zeros, and
before routing existed the pipeline discovered this only indirectly (thin series
were silently dropped). The obvious specialist is Croston / TSB. It was tested
properly — Croston Classic, Optimized, SBA and TSB, by rolling-origin CV on the
standalone Lumpy + Intermittent population, WAPE primary — and **none earned an
implementation**: a plain trailing average beat every variant. The apparent edge
of Croston-type models was mostly "react faster to recent months", which a
3-month average already does without the machinery.

Re-run inside trend subgroups, the best window differs: **short (3 months) on
declining items, long (12 months) on stable, growing and unknown-trend items.**
That split is the whole content of the Naive-3mo vs Naive-12mo choice.

### 6.3 Why classify and route at all

Before routing, Prophet was applied to everything that cleared 24 months and
everything else got no forecast, with no record of why. Routing makes the
decision explicit, per item, with a stated reason, and lets the pipeline give a
low-confidence number to items that previously got none.

### 6.4 Why pool successor families

Without pooling, every successor with under 24 months of its own history is
dropped although its demand is years old. Pooling recovers them. The cost is the
split-back step and the assumptions in it (§9, issue 9).

### 6.5 Experiments run but not adopted

| Experiment (`analysis/…`) | Question | Outcome |
|---|---|---|
| `croston_experiment.py` | Which Croston-family model, and how many occurrences does it need? | No variant beat a trailing average → Naive routes (§6.2) |
| `seasonality_experiment.py` | Is the default seasonal fit overfitting? | Yes → order 3 adopted (§6.1) |
| `changepoint_experiment.py`, `trend_audit.py` | Why is the trend rigid on the worst series? | Prior too tight on known level shifts → segmented 0.25 adopted |
| `split_ratio_window_compare.py` | Would an occurrence-based ratio window improve family splits? | **Negative.** The constraint is pre-boundary sales *volume*, not window length (the affected codes had one non-zero month each). Reverted; script kept as the record and no longer runs on current code. |
| `history_length_experiment.py` | Does the global seasonality overfit short-history series? | Exploratory; not adopted |
| `erratic_naive_experiment.py` | Does a trailing average beat Prophet on Erratic items (Australia first)? | Exploratory; not adopted |
| `ets_sarima_experiment.py` | Do ETS or short-period SARIMA close ground on shortlists of short-cycle and trend-divergent items? | Discovery only; nothing adopted |
| `dev_vs_production_backtest.py` | Does training to the anchor beat training six months earlier? | Yes, moderately → production mode adopted (§7.5) |
| `model_tournament.py` | Does per-item model selection beat the fixed routing rule? Includes a deliberate safeguard against selection noise (the best of seven models on ~6 folds always looks good). | Discovery only; nothing adopted |

The conclusions of the four discovery and exploratory experiments above were not written up beyond their docstrings;
re-run the scripts to reproduce the numbers. The client also intends to
benchmark Azure's own forecasting services (Azure ML) against this approach in a
later phase.

---

## 7. Evaluation

### 7.1 Method

- **Delivered scoring** (production): `evaluation/delivered_accuracy.py` runs each
  cycle and compares every delivered batch with the latest actuals, including
  sales still arriving on retired codes at family level. Results are in
  `dbo.vw_forecast_delivered_accuracy` by batch, month and horizon. This is the
  only completely clean accuracy read. It starts from the first batch whose months
  have actuals.
- **Held-out test window** (development): six months before the anchor; models
  never see it during fitting. The quarterly development run keeps this evidence
  current. The figures in §7.2 come from it.
- **Metrics:** WAPE (primary, volume-weighted), MAPE (secondary — undefined at
  zero actuals and explosive at small ones), signed bias. Report medians, not
  means: single items such as U35108 once produced a 9,794% MAPE and dragged an
  entire rating's mean far above its median.
- **Like-for-like comparison against the prior-year baseline** on item-months
  where both exist.
- `analysis/oos_scoring.py` is superseded by delivered scoring and no longer runs
  as it stands. It is kept for its split-family error decomposition.

### 7.2 Latest results (test window 2026-04 → 2026-09, non-Amazon scope)

| Route | Item-months | Model WAPE | Prior-year WAPE | Model bias | Prior-year bias | Model beats prior-year |
|---|---|---|---|---|---|---|
| **Prophet** | 799 | **50.9%** | 62.2% | −10.0% | −11.0% | 53.7% |
| Naive-12mo | 68 | 57.3% | 59.9% | −18.2% | −31.2% | 50.0% |
| Naive-3mo | 28 | 76.5% | 106.3% | −69.8% | −0.7% | 42.9% |
| **Overall** | 895 | **51.3%** | 62.3% | −10.6% | −11.8% | 53.1% |

### 7.3 How to read them

- **Prophet is clearly better than "same as last year"** — about 11 points of
  WAPE on 89% of rows. This is the evidence that supports publishing.
- **Naive-12mo is roughly the prior-year baseline** — expected, since it is
  essentially a smoothed version of it. Neutral.
- **Naive-3mo is low-confidence:** 28 item-months, a −69.8% bias (it systematically
  under-forecasts). Show it, but do not present it as a firm plan.
- **The whole forecast leans about 10% low.** The baseline leans the same way, so
  it is largely genuine growth the history does not capture. The stale training
  cut-off explains only part of it: training to the anchor helps (§7.5), but a
  full six-month window can still run close to 10% low. Say so when presenting.
- **Absolute accuracy is moderate.** A WAPE near 50% at item-month level reflects a
  noisy, partly intermittent book. Accuracy aggregated to Product Family or
  region was **not measured** here; it is expected to be better than item level
  (errors partly cancel) and should be measured before it is promoted as the
  planning view.
- **Rating** (A–E from the BDM sheet) is a useful cut to report by. In earlier
  runs, median error was lowest on A/B items (median MAPE ≈ 58–62%) and highest
  on D/E (≈ 100%), where low volume dominates. Re-derive it from
  `benchmark_comparison.csv` and `model_metrics.csv` for the current run.

### 7.4 What these results do not show

- **BDM comparison is not valid** until issue 1 is resolved. Earlier win-rate
  statistics against the BDM (including the Australian regression finding below)
  were computed with the Amazon mismatch in place and should be treated as
  directional only.
- **No out-of-sample read yet for the latest forward forecast** — that needs
  actuals for 2026-10 onward. Delivered scoring picks it up automatically.
- Sample sizes for the naive routes are small.

### 7.5 Does training to the anchor help?

`python -m analysis.dev_vs_production_backtest` forecasts the same months from the
same inputs twice: trained to the anchor (production, horizon 1–6) and trained six
months earlier (development, horizon 7–12). Scored on the items both forecast.

| Case | Months scored | WAPE dev | WAPE prod | Bias dev | Bias prod | All-zero items dev → prod |
|---|---|---|---|---|---|---|
| Truncated, anchor 2025-09 (peak) | 6 | 59.7% | 56.3% | +3.6% | +1.1% | 27 → 16 |
| Truncated, anchor 2025-12 | 6 | 60.8% | 57.2% | −6.2% | −6.0% | 27 → 10 |
| Truncated, anchor 2026-03 | 6 | 60.7% | 55.0% | −10.9% | −9.7% | 20 → 15 |
| Real extract, anchor 2026-06 | 3 | 56.5% | 53.1% | −9.2% | −4.6% | 13 → 8 |
| Real extract, anchor 2026-07 | 2 | 57.2% | 54.2% | −7.8% | −2.5% | 24 → 13 |
| Real extract, anchor 2026-08 | 1 | 51.9% | 48.9% | −5.8% | −1.6% | 25 → 12 |

The real extract anchored 2026-09 has no later actuals yet.

- **WAPE improves in every case,** by 3.0 to 5.7 points. This is a real but moderate
  gain: item-level error stays above 50%.
- **Bias improves in every case, but by how much depends on the horizon.** Over a
  full six months the change is small (0.2 to 2.5 points). Over the first one to three
  months it is larger (about 4 to 5 points), so freshness helps most near the anchor.
- **Limits:** six cases, and the three real extracts overlap in time, so they are
  not independent. Truncated cases use today's active list and successor map, which
  affects scope in both modes equally. Zero-item counts are over the months scored.

---

## 8. Operating model and failure modes

| Aspect | Behaviour |
|---|---|
| **Cadence** | Monthly, after the upstream invoice refresh. `run_forecast_cycle.sh` = rebuild training table → score delivered batches → `main.py --production --write-db`. A development run without `--write-db` is a quarterly manual step. |
| **Atomicity** | The schema check and the inserts share one transaction, so a crash leaves the output tables untouched. The *training-table rebuild* also runs in one transaction: a failure rolls back, and readers wait until it commits. Delivered scoring replaces its table in one transaction. |
| **Append-only outputs** | Each run adds a `run_date` batch; `vw_forecast_output_latest` returns the newest. A bad batch is immediately "current". Roll back by deleting that `run_date` from `forecast_output`, and from `forecast_accuracy` if it was a development batch. |
| **Re-run protection** | Every `--write-db` run compares its first forecast month with `MIN(year_month)` at `MAX(run_date)` in `forecast_output` and exits 3 if it is not later, before fitting. `--force` overrides. Development `--require-roll` still compares against the static `config.TEST_END`, but no longer protects anything on its own. |
| **Fail-fast** | Missing credentials, unreadable data, bad flag combinations and schema mismatch all stop the run with a distinct exit code before anything is written. A delivered-scoring failure is the one exception: it warns and the forecast continues. |
| **Staleness of inputs** | The completeness check and the elapsed-month gate stop a partial month becoming the anchor. If the upstream refresh did not happen, the first forecast month does not move, so the duplicate check exits 3 and nothing is published. |
| **Manual step each month** | Bump `ACTIVE_SINCE`; verify the output with `database/diagnostics/forecast_output_checks.sql`. |
| **Failure notifications** | None built in; use cron `MAILTO` or wrap the script in your monitoring. |
| **Secrets** | Environment only. |

---

## 9. Open issues and challenges

Ordered by how much they should influence a decision to rely on the numbers.
"Decision" means a business/stakeholder call, not an engineering task.

| # | Issue | Impact | Status / suggested next step |
|---|---|---|---|
| 1 | **BDM benchmark is not like-for-like.** The BDM sheet includes Amazon; the model's actuals and forecasts exclude it. Amazon is ≈ 24% of the Apr–Sep BDM plan (≈ 24.6k vs 76.1k non-Amazon units). `evaluation/benchmark.py` sums BDM across all codes with no Amazon filter. | Any "model vs BDM" claim is invalid; BDM wins are overstated | **Parked by decision** — Amazon and non-Amazon are treated as separate concerns. Fix: exclude Amazon BDM codes from the BDM leg, or add Amazon to the model scope. Meanwhile label the Power BI series "excl. Amazon" and never place it unfiltered beside the BDM measure. |
| 2 | **Prophet forecasts near zero for items that are selling, because its fitted trend goes negative and the result is clipped to 0.** This is not a pooling problem as previously recorded. `UN1350` is not pooled and sold 539 units in Apr–Sep 2026, yet its forecast is 0: its fitted trend is already −10.6 at the end of training even when trained to 2026-09. `D35480` has a split ratio of 1.0, so the split is not the cause either: its family's raw forecast is already about 0. | Items appear to have stopped selling in Power BI. Production mode reduces this but does not remove it: all-zero items fall from 20 to 6 on the 2026-09 data. Trailing six months against the six-month forecast, trained to 2026-03 → to 2026-09: `UN1350` 539 units, 0 → 0; `U35231` 182, 0 → 0; `DN1180` 52, 0 → 0; `D35480` 115, 1.2 → 51.7. | **Open, fix not built.** Options include routing an item to a trailing average when its fitted trend is not positive, or a floor. Either changes forecasts and needs its own validation. |
| 3 | **24% of routed items get no forecast** (64 of 266): retired, short history, too few sale events, or Amazon-migrated. | Gaps in coverage; planners may read blank as zero | By design, with reasons. Surface the blocked list and reasons in Power BI so absence is not misread. |
| 4 | **Under-forecast bias of ~10% overall; Naive-3mo −70%.** | Consistent shortfall against actuals | Training to the anchor helps only partly: near the anchor bias improved by 4–5 points, but over a full six months by 0.2–2.5, and it reached −9.7% (§7.5). Most of it is likely growth the history does not capture. Naive-3mo should be shown as low-confidence. |
| 5 | **Phase 0 sign-offs pending with the client (Samir)**: (a) *Australia regression* — AU Prophet win-rate vs BDM was 30.5% (n = 59), the lowest region; hybrid routing already moved 40% of AU items off Prophet; the remainder are ~82% Erratic and carry about half the volume of non-AU items — a demand-pattern mismatch, not a data-sparsity problem; (b) *adaptive vs fixed seasonality* — untouched; (c) *benchmark-window alignment* — deliberately unchanged. | Methodology not formally signed off | Needs client decision. Note the AU win-rate carries the issue-1 caveat. |
| 6 | **`CROSTON_MIN_OCCURRENCES = 10` is provisional.** It was reasoned for a Croston fit; the route is now arithmetic, which needs no such floor. | Possibly blocks items that a trailing average could serve, or admits ones it should not | Decide whether an occurrence floor belongs on the naive route at all. Lowering it is a scope change. |
| 7 | **`--require-roll` compares to the static `config.TEST_END`.** It is not advanced after a cycle, so it stops protecting anything once it lags the data. | Would have appended duplicate batches | **Resolved** for every `--write-db` run by the duplicate-batch check (§8). The flag remains for development runs only. |
| 8 | **Development runs train six months before the anchor, so their forward forecast never sees the latest six months of actuals.** | Forecast reacts late to recent shifts | **Resolved for the monthly cycle** by production mode (§5.1). Measured benefit: 3–6 points of WAPE and a partial bias improvement (§7.5). Development runs keep this layout on purpose, for honest holdout scoring. |
| 9 | **Pooled-family naive averaging (~9–11 naive-routed items) is an accepted but unvalidated extrapolation.** Family splits rest on thin post-changeover history for some codes (equal-split fallback is flagged). | Item-level split of family totals can be wrong even if the total is right | Treat the family total as more reliable than its item split. |
| 10 | **`ACTIVE_SINCE` is bumped by hand**, so the monthly cycle still has one manual step. | Items that stopped selling stay in scope and get meaningless forecasts if forgotten | Stays manual for now. Auto-rolling it (for example anchor −14) would change scope, so it needs its own decision. |
| 11 | **Region of some SKUs is a resolved ambiguity** (BDM 'SW'). | Region-level reporting may mis-assign those SKUs | Confirm true region with the business before using the stored `region`. |
| 12 | **"Rating N" is undefined**; **~34% of the wider master table has no Product Family** (mostly Australian A-prefix SKUs). | Reporting dimensions incomplete | Obtain definitions / backfill from the client. |
| 13 | **Test-window scoring is uneven**: 24% of fitted items are scored on fewer than six months. `n_test_months` exists but is not surfaced as a reliability flag. | Single-item MAPEs can mislead | Report medians and WAPE; add a reliability flag. |
| 14 | **Upstream dependencies are outside this repo**: the `INVOICES_TEMP` refresh (and its string-typed dates), and the successor map's upkeep. | A stale or malformed refresh degrades every forecast | Agree ownership and a completion signal; add a post-refresh date-parse check. |
| 15 | **Test coverage is partial.** `tests/` covers windows, the calendar gate, the duplicate check and delivered scoring, but not routing, `family_pool` split-back or `month_completeness` calibration. Some experiment scripts no longer run on current code. | Regressions in modelling rely on manual review and reconciliation | Add unit tests for `model_routing` and `family_pool`; consider a swap-table build so readers are not blocked during the rebuild. |
| 16 | **Naive bands are fixed empirical ratios, not prediction intervals.** | Intervals for Naive routes are not comparable to Prophet's | Present as indicative ranges only. |
| 17 | **Seasonal peak accuracy** (Q4 ramp) is a known weak spot across methods in the sister Amazon workstream; not separately measured here. | Peak-season misses are the costliest | Measure peak-month error explicitly in the next review. |
| 18 | **Credentials**: a database password has been present in earlier project history and must be rotated before production. | Security | Rotate; use a least-privilege login. |
| 19 | **Cosmetic**: the routing report warns `model_metrics.csv not found` when run against a fresh per-run folder. A 2-unit June discrepancy in training-table reconciliation is uninvestigated. | None material | Low priority. |
| 20 | **Amazon is not forecast here.** The Amazon forecast (Vendor Central sell-out, separate agents) needs a `channel` discriminator before the two can be merged into one output. | Total-company demand view is incomplete | Pending the Amazon workstream. |
| 21 | **Deferred: pre-save guard.** Nothing yet checks a batch for plausibility before it reaches Power BI, and a bad batch is immediately current. | A wrong batch would be published without warning | **Not built; awaiting agreement with Samir** on the bands, the EU uplift basis and AU handling. Design and calibration findings: §9.1. |
| 22 | **Prophet's `yhat_lower` / `yhat_upper` are not reproducible.** Nothing seeds the uncertainty sampling, so two runs on the same data differ on roughly 280 lower and 920 upper bounds out of 1,212 rows, by up to about 75 units. `yhat` is identical. | Bands move between identical runs; compare `yhat` when checking for regressions | Accepted. Seeding would be a small change if it ever matters. |

### 9.1 Deferred: pre-save guard

**Status: not built, awaiting agreement with Samir.** Recorded here so the design
and the calibration work are not lost. Every band below is provisional.

**Intended design.** A check after the forecast is built and before any database
write, against a centre of *the same six months last year × recent growth*, where
growth is the last six months of actuals over the same six months a year earlier.
An optional adjustments table (region or total, month range, expected uplift, note,
expiry) would move the centre, never the forecast.

- **Stop** (nothing written; a reasoned override is logged): total outside ±50% of
  centre; item count changed by more than 10%; blocked count changed by more than
  10; any NaN or negative value; any region total zero or above 2× centre.
- **Flag only** (written, and shown in a review file and Power BI): total outside
  ±25%; any month outside 0.6–1.6 of its prior-year month; region outside 0.7–1.4;
  family outside 0.5–1.5; item six-month forecast below 0.1× or above 3× its
  trailing six months, where trailing is at least 30 units.

**How each band behaved on the 2026-09 data.** "Delivered" is the development
forecast (train to 2026-03). "Production" is the same anchor trained to 2026-09.
The last column applies the band to what actually sold, as if the forecast had been
perfect, over the 51 anchors from 2022 (57 months for the month band). It shows how
often a band fires when nothing is wrong, though real forecasts are smoother than
actuals, so it overstates that somewhat.

| Band | Delivered | Production | Perfect forecast |
|---|---|---|---|
| Stop: total ±50% | 0.80, pass | 0.84, pass | 0 of 51 |
| Stop: item count ±10% | 202 vs 200, pass | 213 vs 202 (+5.4%), pass | Cached runs: 198, 200, 202 |
| Stop: blocked ±10 | No stored count | **64 → 53, stops** | n/a |
| Stop: NaN or negative | Pass | Pass | n/a |
| Stop: region zero or above 2× | Pass | Pass | **AU 3 of 51** |
| Flag: total ±25% | 0.80, pass | 0.84, pass | 4 of 51 |
| Flag: month 0.6–1.6 | 0.81–1.01 | 0.85–1.07 | 3 of 57 |
| Flag: region 0.7–1.4 | **USA 0.62** | **USA 0.68** | **AU 27, USA 8**; UK and EU 0, of 51 |
| Flag: family 0.5–1.5 | 11 of 43 | 10 of 44 | Not tested |
| Flag: item below 0.1× / above 3× (Prophet) | 16 / 3 | 8 / 2 | Not tested |

**Calibration findings to carry forward:**

- **Compute the centre on forecast scope, including retired codes,** not on all
  non-Amazon sales. About 16% of recent non-Amazon units sit outside the forecast
  scope. On all sales the total reads 0.70 (delivered) and 0.745 (production), a
  false flag; on forecast scope it reads 0.80 and 0.84.
- **The first production batch trips the blocked-count stop**, because six more
  months of history unblock 11 items. Skip that check when the previous batch was a
  different mode. No table stores blocked counts today, so the guard would need one.
- **AU needs its own handling.** Its actuals swing too widely for 0.7–1.4 and even
  for the 2× stop. Give it wider bands or a zero-only stop. USA also flags often.
- **Family bands need a volume floor of 500 prior-year units,** and total rather than
  own growth. That leaves 3 of 29 families flagged (Aura Ring, YoYo, Techne).
  Without a floor, small families dominate: 5th–95th percentile of forecast/actual
  is 0.35–1.48 across all 43, and 0.53–1.38 for families above 500 units.
- **The trailing item test gives seasonal false flags.** It compares Apr–Sep with
  Oct–Mar in a business that sells about 39% more in winter: `E31575` sold 1,041 last
  Oct–Mar, is forecast 1,224, and is flagged at 3× trailing. Compare with last year's
  same months × growth, falling back to trailing only where there is no history.
- **Open question: should growth be clamped?** It swung from 0.90 at the 2026-03
  anchor to 1.235 at 2026-09, and the centre missed actuals by 38% at 2026-03.
- **Open question: what is the basis of an EU uplift?** The business expects EU sales
  up 20–30%. Does that include Amazon, which this forecast excludes? Is it against
  last year, or on top of trend? The centre already carries EU growth of 1.13, so
  stacking double-counts. At +25% replacing growth, EU reads 0.81 (delivered) and
  0.84 (production); stacked at +30%, delivered EU falls to 0.69 and flags. The surge
  is not yet in the data: EU actual over prior year is 1.18 for the last six months,
  1.08 for the last three and 0.985 for the last twelve, below the business's 1.235.
  The delivered EU forecast is only 1.02× last year (1.05× trained to the anchor), so
  if the surge is real the forecast will run about 15–20% low. Whether to put an
  uplift into the forecast itself is a separate decision.

Before fixing the bands, replay them on the real extracts in `prophet_forecast/` and
on peak-season anchors.

---

## 10. Glossary

| Term | Meaning |
|---|---|
| **Item code** | A product SKU (prefix indicates region: D = UK, E = EU, U = USA, A = AU) |
| **BDM** | Business Development Manager; a salesperson who owns accounts and produces a manual forecast |
| **Anchor / `TEST_END`** | The newest complete month; every window is an offset from it. Production also requires it to have fully elapsed |
| **Production / development run** | Production trains to the anchor and publishes; development trains to anchor −6 and scores a held-out test window |
| **Horizon** | Months from the last training month to the forecast month |
| **Successor family / `family_key`** | A retired code plus its replacement code(s), pooled for training |
| **Product Family / `family`** | A reporting label from the master product table (unrelated to the above) |
| **SBC / Syntetos-Boylan** | A four-way demand classification using ADI and CV² |
| **ADI** | Average demand interval: months of tenure ÷ months with sales |
| **CV²** | Squared coefficient of variation of non-zero monthly quantities |
| **Sell-in / sell-out** | Units Daylight ships *to* Amazon / units Amazon sells to consumers |
| **WAPE** | Σ\|actual − forecast\| ÷ Σ actual; volume-weighted error |
| **MAPE** | Mean absolute percentage error; unstable at small quantities |
| **Bias** | Σ(forecast − actual) ÷ Σ actual; negative = under-forecast |
| **Rolling-origin CV** | Repeated forecast-then-score at successive cut-offs inside the training window |
| **Blocked** | An item the pipeline deliberately does not forecast, with a recorded reason |
