# How the Forecasting System Works

A plain-English walkthrough of what happens when `main.py` runs, kept in sync
with the code on `feature/tuning-prophet-settings`. Numbers below are from the
last real run (`output/`, ~1 week old at time of writing) — not simulated.

---

## The Big Picture

The system looks at **~8 years of past sales** (Jan 2018 onward) to learn how
each **product** sells — when it peaks, when it dips, whether it's growing or
declining. It then predicts the next 6 months for each product.

**This is different from the original design in one important way: BDM is no
longer part of the forecasting grain.** Each product gets ONE model, fitted on
its demand summed across every BDM who sells it. BDM survives only as
reporting context (whose sheet do we compare against), not as something the
model is fitted per. See "Why BDM was dropped from the grain" below.

Think of it like this: if you looked at 8 years of monthly sales for a desk
lamp — across all the salespeople who sell it — and noticed it always spikes
in October and drops in May, you'd reasonably predict next October will spike
too. That's what the system does, for **187 products** at once (last run),
with mathematical precision instead of gut feel.

---

## The date windows move — they are not hard-coded dates

Every window in `config.py` is a fixed offset from `TEST_END`, and `TEST_END`
itself is **not** "today's date" — it's the most recent *complete* month found
in `forecast_training_data`, determined by `analysis/month_completeness.py`
(a month with a partial upstream pull, e.g. running well below seasonal
expectation, is treated as not-yet-complete and skipped).

| Window | Last run's value | Offset from TEST_END |
|---|---|---|
| Training start | 2018-01 | fixed |
| Training end | 2025-12 | -6 |
| Test (accuracy check) start | 2026-01 | -5 |
| Test (accuracy check) end | 2026-06 | anchor |
| Forward forecast start | 2026-07 | +1 |
| Forward forecast end | 2026-12 | +6 |

The window has already rolled forward twice since this pipeline was built —
most recently 2026-08-03, when it moved +2 months because 2026-07 in the
extract was only 46% of seasonal expectation (partial pull, below the 60%
completeness threshold) and 2026-06 was used as the anchor instead. **If
you're reading this doc later and the dates above look stale, that's expected
— re-derive with `python -m analysis.window_proposal` rather than trusting
this table.**

---

## What Happens Step by Step

### Step 1 — Load the Data

The script connects to Azure SQL and pulls five things, each cached locally to
`output/*.csv` so re-runs with `--cached` skip the DB entirely:

| Source | Cached as | Last run |
|---|---|---|
| `forecast_training_data` (monthly qty per item/BDM/region) | `raw_data.csv` | 49,634 rows, 731 distinct items |
| BDM manual forecast sheet | `bdm_forecasts.csv` | 9,254 rows, 286 items |
| Active-product allow-list | `active_products.csv` | 265 items |
| Master product table (→ Product Family) | `master_product.csv` | 1,426 items |
| `product_successor_map` (retired → replacement codes) | `successor_map.csv` | 91 pairings: 71 retired codes → 78 successor codes |

`forecast_training_data` is already monthly-aggregated (item × BDM × region ×
month) — it is **not** the ~330K-row invoice-line table. Don't conflate the
two when quoting row counts.

---

### Step 1b — Detect and Exclude Channel-Mismatch Items

Before anything else is filtered, every item's trailing-12-month sales are
checked for a specific failure pattern: **real demand that has moved almost
entirely into Amazon, the channel this pipeline deliberately excludes from
training (Step 2).** For an item like that, forecasting the non-Amazon
remainder isn't a tuning problem — there's too little real signal left in the
channel being trained on, even though the item's total demand is real and
ongoing.

An item is excluded if, in the trailing 12 months ending at `TEST_END`, ALL
THREE hold:

| Condition | Threshold |
|---|---|
| Amazon share of total volume | >= 90% |
| Non-Amazon volume remaining | < 60 units/year |
| Total (all-channel) volume | >= 300 units/year |

The third condition is what keeps this from catching genuinely low-volume or
declining products — those should still get a low forecast, not be silently
suppressed. Only items with real, substantial demand that's simply invisible
to this pipeline's trained channel get excluded.

**Discovered from `U35108`** (Slimline 3 Table Lamp, USA): 99.6% Amazon
share, only 11 non-Amazon units/year against 2,971 total. Prior to this fix,
Prophet was fitting a trend against those 11 remaining units while predicting
off a 2018–2023 baseline from before the channel shift — producing a 9,794%
test-window MAPE that had nothing to do with model tuning. Investigation also
found `U35108`'s nominal "successor" (`U35109`) is not a stand-in for this
demand — it's a separate, genuinely independent product with its own real
wholesale volume, already correctly forecasted under a different pooled
family (`U35107`). Substituting one for the other would have been wrong in
the other direction.

Excluded items get **no forecast at all** until Amazon-inclusive forecasting
exists for them (see the shelved Amazon-forecasting workstream) — not a
suppressed or substituted number. Every run logs exactly which items were
excluded and why to `output/excluded_channel_mismatch.csv`, with the actual
Amazon share and volume figures, so this is auditable rather than a silent
gap. On the run that surfaced this fix, exactly one currently-active item
(`U35108`) met the threshold; a second item (`U25201`, same "moved to Amazon"
pattern) was also caught but was already excluded from scope for an unrelated
reason (not on the active-product list), confirming the check generalises
rather than being hand-tuned to one SKU.

---

### Step 2 — Exclude Amazon Channel Rows

Before anything else, rows where `channel == 'Amazon'` are stripped out and
saved to `output/excluded_amazon_rows.csv` for inspection.

**Why:** these are bulk replenishment orders Daylight ships *to* Amazon's
fulfilment centres, not consumer purchases — lumpy, irregular, and they
corrupt Prophet's seasonality learning if left in. The filter matches on
`channel`, not on BDM code, so it self-maintains as new Amazon BDM codes get
added. (Phase 2 will replace this signal with Vendor Central sell-out data.)

**Last run: 5,761 rows / 312 distinct items excluded.**

---

### Step 3 — Aggregate to the Modelling Grain (item × month)

Raw rows are summed to one row per `item_code` per month. **`bdm_code` and
`region` are dropped here** — this is where the BDM grain disappears. A
separate lookup (`series_metadata()`) captures item→region from the raw rows
first, purely for reporting breakdowns later; it never re-enters the model.

#### Why BDM was dropped from the grain (2026-07)

BDM is sales-credit attribution — which salesperson gets commission — not a
demand driver. Forecasting "D25090 × Sarah Whyld" separately from
"D25090 × everyone else" was modelling an accounting split, not a different
demand pattern. An item's demand is now summed across all its BDMs into one
series. This is a real design change from an earlier version of this
pipeline, not a bug — if you're comparing against old output files (anything
with a `bdm_name` column at the forecast/metrics grain), those are from
before this change.

---

### Step 4 — Successor-Family Pooling (a detour around the fit, not a rewrite of it)

**The problem:** when a product is discontinued and replaced, the successor
code starts its own sales history at zero. `MIN_TRAIN_MONTHS = 24` then
excludes it — even though the *demand* has years of history under the old
code.

**The fix:** retired codes and their successors are temporarily pooled into
one series for training, then split back into real item codes afterward.
Family membership is resolved as **connected components** over the old↔new
map — not "one retired code → its successors" — because the map is a
many-to-many relation (13 successor codes are each reachable from two
different retired codes in the current data). A family is eligible for
pooling only if at least one of its *current* codes is on the active-product
list.

**Last run: 91 map rows resolved into 59 eligible pooled families**, replacing
those members' individual series for the fit only. Everything before this
step and after the split-back (Step 8) is plain item-level data — this
machinery is invisible to scope filtering, benchmarking, and the CSV writers.

**Splitting the family forecast back out uses TWO different ratios, not one:**

| Ratio | Observed from | Used for |
|---|---|---|
| `test_ratio` | Actuals up to `TRAIN_END` only | Anything scored on the test window (`test_validation.csv`, `model_metrics.csv`) |
| `forward_ratio` | Freshest actuals available, no cutoff | The genuine forward forecast |

Using one ratio for both would let the split "peek" at how demand actually
divided during the test months, quietly flattering test-window accuracy. This
was caught in an earlier version of the output: 6 of 14 split families showed
an *identical* `bias_pct` across every one of their codes — the tell that the
observation window and the scored window were the same months. Under the
two-ratio fix that number is 0.

A family whose codes don't individually have at least 3 post-changeover months
of sales falls back to an equal split and is flagged (`split_method =
equal_fallback`) rather than trusting a mix inferred from one or two months.

---

### Step 5 — Scope Filtering

With families pooled, three filters run in order:

1. **Active-product allow-list** — must be on `active_products.csv` (or be an
   eligible pooled family_key).
2. **Forecastable series** — at least `MIN_TRAIN_MONTHS = 24` months of
   history strictly before the test window (two full yearly cycles, the
   minimum to detect yearly seasonality).
3. **(Optional, `--pilot` flag only)** — restrict to A-rated items for a fast
   validation run.

**Last run, without `--pilot`: 187 series survived scoping** — 111 as
standalone items, plus the 59 pooled families (which expand back to 76 item
codes after Step 8's split).

---

### Step 6 — Assign Each Series to a Changepoint Segment

Every fitted series gets ONE of two `changepoint_prior_scale` values — a
strict two-way branch, no per-item tuning:

| Segment | Value | Applies to |
|---|---|---|
| Standard | 0.05 | Everything by default |
| Known changeover | **0.25** | A pooled family whose successor-review record carries a *confirmed* `estimated_changeover` date |

**Why:** at 0.05, a real sustained level shift (a genuine changeover) gets
regularised away as noise — the model keeps predicting the old level while
actual sales run somewhere else. `analysis/trend_audit.py` found this exact
signature in 38 of the 68 worst-performing series. `0.25` was chosen over
looser values by `analysis/changepoint_experiment.py` under rolling-origin CV:
it beat 0.05 on 19/22 known-changeover families (median MAPE -30.7%) but only
29/43 on series *without* a confirmed changeover — not enough of an edge to
loosen the prior everywhere, hence the segmentation rather than a global
change.

**⚠️ A finding from this engagement's own data, worth knowing before you read
too much into the segmentation:** right now, *every one* of the 59 pooled
families also has a confirmed changeover date — `successor_map.csv` has zero
rows with a blank date. That means the "known changeover" segment and "is
pooled at all" are currently the exact same 59 families — the segmentation
isn't actually discriminating *within* the pooled population yet, it's just
splitting pooled vs. singleton. That's not a bug; the logic is built to
diverge (a pooled family without a confirmed date would stay on the standard
prior) — it just hasn't been exercised by the data so far. If a future
successor-map update adds an undated pooled family, expect this to change.

---

### Step 7 — Train a Model for Each Series

One Prophet model per fitted series (family_key or plain item_code):

```python
Prophet(
    yearly_seasonality      = 3,             # Fourier order — NOT the library default (True = 10)
    weekly_seasonality      = False,
    daily_seasonality       = False,
    seasonality_mode        = "multiplicative",
    changepoint_prior_scale = 0.05 or 0.25,  # segment-dependent, see Step 6
    seasonality_prior_scale = 10.0,
    interval_width           = 0.95,
)
```

**`yearly_seasonality = 3` is a deliberate departure from Prophet's default.**
The default (`True` → 10 Fourier term pairs, 20 free parameters) overfits at
monthly resolution when fit against only 6–8 repeats of a yearly cycle — it
produces seasonal swings of hundreds of percent that are invisible at daily
resolution but absurd at monthly. Order 3 was validated by
`analysis/seasonality_experiment.py` under rolling-origin CV: it beat order 10
on 102/152 series by MAPE and 121/152 by WAPE (median -10.2% MAPE). It's a
single global value, not tuned per product.

**Training time:** ~0.5 sec/series. For 187 series, the full training loop
runs in under 2 minutes.

---

### Step 8 — Generate Predictions, Split Family Forecasts Back to Item Level

Each model predicts the test window + forward window together in one call.
Negative predictions (impossible — you can't sell -5 lamps) are clipped to
zero.

Family-keyed predictions are then split back to real item codes using the
ratios from Step 4 — singletons and one-for-one renames pass straight
through unchanged; multi-way splits get one row per current code per month,
scaled by that code's share.

For the **test** frame specifically, each item's `actual` is replaced with
that item's *own* observed sales (not a ratio-scaled slice of the family
total) wherever it's available — splitting a measured fact would invent data
and make the accuracy numbers meaningless.

**Last run: forward forecast = 1,122 rows (187 items × 6 months).**

---

### Step 9 — Tag Product Family (reporting only)

A `family` column (e.g. "Slimline", "Wafer 1") is joined onto every output
frame from the master product table, purely for grouping in reports — it
never enters the model. Item codes with no master-product row get tagged
`Unknown` rather than dropped or guessed, so any coverage gap stays visible.

**Note — this is a different "family" from Step 4.** `family.py`'s Product
Family is a reporting label from the master table. `family_pool.py`'s
successor family is the training-pooling mechanism. They are unrelated
concepts that happen to share the word "family" — the codebase deliberately
keeps them namespaced apart (`family_key` vs. `family`) and so does this doc.

**Last run: 187/187 (100%) of fitted series matched to a known Product
Family** — the ~34% "Unknown" coverage gap that exists in the wider master
table (mostly AU A-prefix SKUs) didn't affect any item actually in this run's
scope.

---

### Step 10 — Compare Against BDM and a Naive Baseline

For the overlap between the test window and the BDM sheet's forecast year
(2026-01 to 2026-04), the script compares three numbers per item-month:

- **Prophet's prediction**
- **The BDM manual forecast — summed across every BDM who forecasts that
  item**, not one arbitrary BDM's number. This changed alongside the grain
  change in Step 3: since Prophet no longer produces a per-BDM number, the
  correct comparator is the sales team's *total* demand call for the item,
  not a single BDM's slice of it. **Per-BDM win-rate breakdowns are gone as a
  result** — the model doesn't produce a number to break out that way anymore.
- **A naive baseline** — same item, same month, one year prior.

Whichever has the lowest MAPE for that item-month wins. Results are
summarized by product rating (A–E) and region.

---

### Step 11 — Save Everything

| File | What's inside |
|---|---|
| `test_validation.csv` | Item-level predictions vs. actuals, test window |
| `forecast_{start}_{end}.csv` | Forward forecast — filename derived from `config.FORECAST_START/END`, not hard-coded, so it can't silently go stale as the window rolls |
| `model_metrics.csv` | Per-item MAE / RMSE / MAPE / WAPE / bias, plus `changepoint_segment` and family-pooling trace columns |
| `benchmark_comparison.csv` | Prophet vs. BDM vs. naive, with winner |
| `excluded_amazon_rows.csv` | Rows dropped in Step 2, for audit |
| `successor_split_ratios.csv` | Both ratios (test + forward) per pooled family/item, for audit |
| `unmatched_family_log.csv` | In-scope item codes with no Product Family match (empty last run) |

Last run's actual filename was `forecast_2026-07_2026-12.csv` — if you see a
file called `forecast_may_oct_2026.csv` anywhere, it's from a stale run or an
old cached copy, not this pipeline as it stands today.

---

## Accuracy by Product Rating — Last Run

Verified by joining `model_metrics.csv` to the rating on `bdm_forecasts.csv`
(all 187 fitted series matched — 0 unmatched).

| Rating | n series | Median MAPE | Mean MAPE | Median WAPE | Mean WAPE | Median Bias |
|---|---|---|---|---|---|---|
| A | 40 | 58.5% | 344.3% | 49.3% | 285.7% | +8.0% |
| B | 34 | 62.1% | 142.2% | 56.0% | 58.2% | -2.5% |
| C | 46 | 73.2% | 99.2% | 56.8% | 62.8% | -15.5% |
| D | 11 | 100.0% | 229.6% | 100.0% | 151.6% | -35.7% |
| E | 34 | 100.0% (1 undefined) | 345.5% | 81.5% | 330.6% | -11.4% |
| N | 22 | 96.2% | 222.7% | 64.3% | 81.3% | -14.1% |

**Report the median, not the mean, when this goes in front of Samir.** Every
rating's mean is dragged far above its median by one or two extreme
single-series misses — most visibly rating A, where the mean (344.3%) bears no
resemblance to the median (58.5%).

**The A-rating mean is wrecked by one item: `U35108`.** MAPE 9,794%. Its raw
test-window numbers:

| Month | Actual | Prophet forecast |
|---|---|---|
| 2026-01 | 2 units | 263 |
| 2026-02 | 2 units | 222 |
| 2026-06 | 3 units | 163 |

Demand for this item has collapsed to near-zero, but it's tagged
`changepoint_segment = standard` — no confirmed successor record is pulling it
into the loosened-prior segment. This is a strong candidate to cross-check
against the 104 declining products with no confirmed successor, rather than a
random miss to tune away.

**An unresolved data question: rating "N."** `bdm_forecasts.csv` carries a
sixth rating value alongside A–E, covering 22 of the 187 fitted series. Its
meaning isn't documented anywhere in this codebase — confirm with Samir's team
before "rating" is used as a client-facing reporting dimension.

**The four previously-flagged erratic SKUs, current numbers:**

| Item | Rating | MAPE | WAPE | Test months |
|---|---|---|---|---|
| A25090 | A | 87.6% | 52.8% | 5 |
| DN1380 | C | 112.7% | 82.0% | 6 |
| U35070 | D | 874.1% | 89.8% | 3 |
| UN91171 | E | 95.4% | 98.0% | 6 |

WAPE reads far more reasonably than MAPE for all four — consistent with the
Syntetos-Boylan finding that these are volume-weighted-tolerable but
percentage-hostile: the wrong shape of error for a MAPE-based read to
characterise fairly.

**Test-window completeness is uneven, and nothing downstream currently flags
it:**

| Test months available | n series |
|---|---|
| 6 (full) | 142 |
| 5 | 16 |
| 4 | 12 |
| 3 | 6 |
| 2 | 4 |
| 1 | 6 |
| 0 (MAPE undefined) | 1 |

45 of 187 series (24%) are scored on fewer than 6 months. A MAPE built from
1–3 months carries materially less statistical weight than one from 6, and
while `n_test_months` is present as a column in `model_metrics.csv`, it isn't
surfaced anywhere as a reliability signal today. Treat single- and
double-month MAPEs with real caution — U35070 above is one of them (3 months).

---

## Summary

```
DB pull (5 tables, cached to output/*.csv)
        │
        ▼
   Exclude Amazon channel rows
        │
        ▼
   Aggregate to item × month  (BDM + region dropped from the grain here)
        │
        ▼
   Pool retired-code + successor families for training (detour, not a rewrite)
        │
        ▼
   Scope: active-product list → 24mo-history filter → (optional A-rated only)
        │
        ▼
   Assign changepoint segment (known-changeover family → looser prior)
        │
        ▼
   Train one Prophet model per item / pooled family
        │
        ├──► Test window: split back to item level → check accuracy →
        │                  compare vs BDM (summed) and naive baseline
        │
        └──► Forward window: split back to item level → tag Product Family →
                              deliver to Power BI
```

**Last run: 187 fitted series (111 standalone items + 76 item-level rows split
from 59 pooled successor families), covering 265 active products, in under 2
minutes of training time.** Median per-series test-window MAPE / WAPE were
80.5% / 59.4% — high enough, and skewed enough by low-volume/erratic SKUs and a
handful of partial test windows, that raw aggregate numbers shouldn't go to
Samir unsegmented. See "Accuracy by Product Rating" above for the breakdown by
rating, the specific outlier (`U35108`) inflating rating A's mean, and the
test-window-completeness caveat that applies to 24% of fitted series.
