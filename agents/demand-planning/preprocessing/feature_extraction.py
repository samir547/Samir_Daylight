"""
Per-item time-series feature extraction — pure discovery, one row per SKU.

WHAT THIS IS
────────────
A DESCRIPTION of every active-scope item's own demand behaviour, written to one
CSV. It fits nothing, routes nothing, and decides nothing. No config value moves,
`preprocessing/model_routing.py` is not imported, no existing output is
rewritten, and nothing goes to the database. The output is a reading surface:
numbers a human sorts and filters to decide which items are worth a closer look.

It exists because the diagnostics that currently answer "which items are
misbehaving, and how?" are all either manual or narrow. `analysis/trend_audit.py`
gives the fullest answer for trend, but it refits Prophet per series and is
therefore run over a handful of high-volume items at a time, not the population.
Everything else — the 2-month alternation spotted on UN1050, the recent softening
on E35500 and E25400, the suspected stockout on A35050 — was found BY EYE, one
chart at a time. Each of those is a shape a statistic can detect. This module
computes those statistics for all 248 items at once, so the eye is spent on a
shortlist instead of on the search.

Nothing here replaces the fuller diagnostics. `trend_divergence_pct` is a fast,
universal SUBSTITUTE for the question `trend_audit` answers properly — it is two
straight lines, not a changepoint analysis — and its job is to say WHICH items
deserve the expensive version, not to stand in for its verdict.

Run:  python -m preprocessing.feature_extraction
      python -m preprocessing.feature_extraction --input-dir output_phase1
      python -m preprocessing.feature_extraction --anchor-max-month

═══════════════════════════════════════════════════════════════════════════════
THE DATE ANCHOR, WHICH IS NOT config.TRAIN_END AND NOT config.TEST_END
═══════════════════════════════════════════════════════════════════════════════
Every feature below is computed on months <= ANCHOR, and ANCHOR is resolved FROM
THE DATA on each run by `analysis.month_completeness.latest_complete_month()`.
It is printed at the top of the run. Three candidates were available and two are
rejected:

  config.TRAIN_END (2025-12) — rejected. It is the training-window boundary, and
      the right anchor for `demand_classification.py`, whose whole purpose is to
      describe the series THE PROPHET FIT SEES. This module is not describing a
      fit. Anchoring discovery at TRAIN_END would blind it to the last six-plus
      months of actuals — which is precisely where the three motivating cases
      (UN1050's alternation, E35500/E25400's softening) live. It is also under
      review pending an unresolved date-range decision, so building a new
      artifact on it would bake in a value that is expected to move.

  MAX(year_month) (2026-08 in the current extract) — rejected, and this is the
      trap. `forecast_training_data` is refreshed by an upstream pull whose most
      recent month is routinely PARTIAL: 2026-08 carries 650 transactions at 52%
      of seasonal expectation against ~1,400-1,600 in the months around it.
      Anchoring there would end every single series on a fabricated cliff, and
      `trend_divergence_pct` fits a line to exactly the last six months — the
      partial month would be one sixth of that fit and would report a fake
      collapse on all 248 items at once. `zero_rate` and `near_zero_rate` would
      be inflated the same way. The one feature most likely to be acted on is the
      one most poisoned by taking MAX literally.

  latest COMPLETE month (2026-07) — used. `month_completeness` compares each
      month to the same calendar month a year earlier and divides out the
      business's overall growth, so seasonality cannot masquerade as truncation;
      see that module for why a trailing-median rule was tried and rejected.
      This is genuinely "the latest available month in the data" with the one
      month that is not really available removed.

Note that this is a THIRD window, distinct from both config values, and that it
moved: the config.py comment describing 2026-07 as a partial pull was written
2026-08-03 and the extract has been refreshed since — 2026-07 now scores 1.050
and passes. The anchor is resolved per run rather than hard-coded precisely so
that a refresh moves it without anyone editing this file.

`--anchor-max-month` forces the bare MAX(year_month) reading. It is there so the
choice above is inspectable rather than merely asserted; it is not a recommended
mode and the run prints a warning when it is used.

CONSEQUENCE: prepare.prepare() IS NOT USED
──────────────────────────────────────────
`prepare()` clips to config.TRAIN_START..config.TEST_END, which would throw away
the anchor month itself (TEST_END is 2026-06). That clip is a training-window
concern and correct where it lives. `_monthly_by_item()` below does the same
coercion and monthly aggregation with no upper clip, and is otherwise a
line-for-line equivalent. It is a deliberate divergence, not an oversight.

═══════════════════════════════════════════════════════════════════════════════
THE GRAIN: PER item_code, NOT PER FIT KEY — AND WHAT THAT MEANS FOR THE
COLUMNS MERGED IN FROM demand_classification.csv
═══════════════════════════════════════════════════════════════════════════════
`demand_classification.classify_scope()` classifies each item on its FIT KEY's
series — the POOLED family series for a pooled item — because the demand pattern
that matters for a routing decision is the pattern of the series that is actually
modelled. That reasoning is correct there and does not transfer here. This module
is describing each REAL SKU's own behaviour, and pooling would obscure exactly
the thing being looked for: a successor code's own launch ramp, a retired code's
own run-down, a member whose recent softening is averaged away by its family.

So every feature computed BELOW is on the item's own standalone series.
`family_key` and `is_pooled` are carried on each row for reference, so a reader
can see which items share a fitted series, but no series is pooled here.

THIS PUTS TWO DIFFERENT WINDOWS AND TWO DIFFERENT SERIES IN ONE ROW.
The seven columns merged straight out of demand_classification.csv —
`category`, `cv2`, `adi`, `n_nonzero_months`, `tenure_months`, `decline_ratio`,
`near_threshold` — are TRAIN_END-anchored (2025-12) and computed on the POOLED
series. Everything this module computes is ANCHOR-anchored (2026-07) and computed
on the STANDALONE series. They are not recomputed here on purpose: re-deriving
them would produce a second, silently different `category` for the same item, and
two disagreeing classifications is a worse outcome than one clearly-labelled
mismatch of basis. The distinction is restated in the run summary, and the
standalone counterparts that ARE computed here carry `_grid` names
(`n_nonzero_grid`, `n_months_grid`) so no column silently shadows a merged one.

The population is that file's item list verbatim — which IS `classify_scope()`'s
active-scope universe, since that is what wrote it.

═══════════════════════════════════════════════════════════════════════════════
THE SERIES EVERY FEATURE IS COMPUTED ON: A ZERO-FILLED MONTHLY GRID
═══════════════════════════════════════════════════════════════════════════════
`prepare()`-shaped frames carry no zero-quantity rows, so a month with no sales
is ABSENT rather than zero. Handed such a frame, every feature here would be
wrong in the same direction: STL would see an evenly-spaced series that sells
every month, `zero_rate` would be 0.0 by construction for every item, and the
ACF lags would be autocorrelations of a series whose gaps have been closed up —
i.e. of a DIFFERENT series. Every item is therefore reindexed onto a complete
monthly grid with gaps filled 0.

The grid starts at the item's FIRST NON-ZERO MONTH, not at config.TRAIN_START.
Same convention as `demand_classification._tenure_months()` and
`croston_experiment.monthly_grid()`, and for the same reason: a code launched in
2025 has not been failing to sell since 2018, it did not exist, and padding it
with seven years of manufactured zeros would drive its zero-rate to ~0.9 and its
trend to a fiction on the strength of its launch date alone.

Amazon rows are removed first (`scope.filter_amazon_channel`), so the series
described is the series the pipeline trains on. The single exception is the
channel-mix block, which is the one feature that needs the Amazon rows and is
computed from unfiltered raw — see FEATURE 5.

═══════════════════════════════════════════════════════════════════════════════
FEATURE 1 — tsfeatures (Nixtla), AND WHY NULL IS NOT ZERO
═══════════════════════════════════════════════════════════════════════════════
`trend_strength`, `seasonal_strength`, `spectral_entropy`, `acf1`,
`seasonal_acf12`, `lumpiness`, `stability`, `spikiness` come from the
`tsfeatures` package — the same ecosystem already evaluated in
`analysis/croston_experiment.py` — rather than from a local STL/entropy
reimplementation, so these are the standard FFORMA definitions and not this
repo's approximation of them.

The package's own feature functions are called PER SERIES rather than through the
batch `tsfeatures()` entry point. Same implementations, same freq=12; what it
buys is the ability to gate and to count. The batch call cannot do either, and it
has to be prevented from doing something worse:

    tsfeatures DOES NOT RETURN NaN ON A SERIES TOO SHORT FOR THE STATISTIC.
    IT RETURNS A FABRICATED NUMBER.

Measured directly (5 draws of i.i.d. gamma noise per length — true trend strength
and true seasonal strength are both ~0 by construction; seed 7, reproducible by
calling `stl_features(x, 12)` / `lumpiness(x, 12)` / `stability(x, 12)`):

      n      trend   seasonal   lumpiness   stability
      12     0.000     1.000        0.00        0.00
      18     1.000     1.000        0.00        0.00
      23     1.000     1.000        0.00        0.00
      24     1.000     1.000   147626.80       44.15
      25     0.864     0.973   172802.10      196.79
      26     0.776     0.932    78291.85      110.34
      28     0.380     0.890    58601.71       46.18
      30     0.301     0.772    61382.43       50.22
      48     0.182     0.520   294120.54       85.81

Two separate failures, both silent:

  * STL-based (`trend_strength`, `seasonal_strength`, `spikiness`). Below two
    full periods STL has more seasonal parameters than data, the seasonal
    component absorbs the whole series, the remainder collapses to ~0, and the
    strength ratios — which are 1 - var(remainder)/var(...) — saturate at 1.
    White noise is reported as a perfectly trending, perfectly seasonal series.
    Gated at MIN_STL_MONTHS = 2 x 12 = 24; null below.

  * `lumpiness` / `stability`. These are the variance of the variances (resp.
    means) of tiled windows of width freq. Below 2 x freq there is one window,
    so there is nothing to take a variance OF, and the package returns a literal
    0 rather than NaN — the value that reads as "perfectly stable, not lumpy at
    all", i.e. the opposite of what a 14-month series warrants. Gated at 24 too.

READ THE TABLE'S n = 24 ROW BEFORE TRUSTING A BOUNDARY VALUE. Two full periods is
the standard minimum and is what is gated on, but on this implementation n = 24
is still fully degenerate (1.000 / 1.000 on noise) and n = 25-26 still badly
inflated. The gate is not raised above the standard — that would be this file
inventing a threshold — but every row whose grid is shorter than
STL_CAUTION_MONTHS = 30 carries `stl_short_window = True`. Same device as
`demand_classification.near_threshold`: it changes no value, it says the value is
a coin-flip. Sort STL features with that column visible or not at all.

`spikiness` IS NOT SCALE-FREE. It is the variance of leave-one-out variances of
the STL remainder, computed on raw quantities, so it scales roughly as the fourth
power of the item's volume. It ranks items against THEMSELVES over time; it does
not compare a 20-unit/month SKU to a 2,000-unit/month one. The same caveat
applies to `lumpiness` and `stability` for the same reason. `trend_strength`,
`seasonal_strength`, `spectral_entropy` and every ACF column ARE scale-free and
do compare across items.

`spectral_entropy` is gated at one full period (MIN_ENTROPY_MONTHS = 12): below a
single cycle a spectrum cannot separate seasonal structure from noise, which is
the only thing the number is being read for.

WHAT A NULL MEANS, PER COLUMN — this is the distinction the output would be
useless without, because several of these features have a MEANINGFUL zero:

  trend_strength     null = grid < 24 months, STL not attempted.
                     0.0  = STL ran and found no trend: the de-seasonalised
                            series is no more predictable than its remainder.
  seasonal_strength  null = grid < 24 months.
                     0.0  = STL ran and found no annual seasonality.
  spikiness          null = grid < 24 months.
                     0.0  = STL ran and the remainder's leave-one-out variances
                            are identical, i.e. no single month dominates.
  lumpiness          null = grid < 24 months, fewer than two tiled windows.
                     0.0  = two or more windows with identical within-window
                            variance — genuinely non-lumpy.
  stability          null = grid < 24 months.
                     0.0  = two or more windows with identical means — a series
                            whose LEVEL never moved.
  spectral_entropy   null = grid < 12 months, or a constant series with no
                            spectrum to take.
                     ~0.0 = a pure, perfectly concentrated cycle. ~1.0 = white
                            noise. This one is normalised, so 0 is an extreme,
                            not an absence.
  acf1/2/3/6,        null = the grid is too short for that lag (see FEATURE 2),
  seasonal_acf12            or the grid is constant so the ACF is 0/0.
                     0.0  = computed, and the series is uncorrelated at that lag.

Every null is counted and reported per column at the end of the run, so a column
that is mostly null is visible before anyone reads a row.

═══════════════════════════════════════════════════════════════════════════════
FEATURE 2 — SHORT-LAG AUTOCORRELATION (lags 2, 3, 6)
═══════════════════════════════════════════════════════════════════════════════
tsfeatures emits lag 1 (`x_acf1`) and lag freq (`seas_acf1`) and nothing between.
The gap matters: UN1050 shows a roughly 2-month high/low alternation, which is
invisible to Prophet's annual seasonality and invisible to both of the lags
tsfeatures reports.

WHAT THE ALTERNATION ACTUALLY LOOKS LIKE HERE, because the textbook answer is
wrong on the very item that motivated this. A PERSISTENT period-2 cycle gives
`acf1` < 0 AND `acf2` > 0 AND `acf3` < 0, and that pair was the first rank key
tried. UN1050's measured ladder is:

    acf1 -0.374 | acf2 +0.016 | acf3 -0.047 | acf6 -0.097 | acf12 +0.217

i.e. a strong month-to-month FLIP that damps to nothing by lag 2, not a
sustained 2-cycle. Ranking on `acf2 - acf1` put UN1050 eleventh and filled the
top of the list with 13-14 month grids whose `acf2` is a handful of lagged pairs.
Ranking on `acf1` alone puts it FIFTH of 229 — so the flag for this shape is a
strongly negative `acf1`, and lags 2/3/6 are what say whether the oscillation
PERSISTS or damps. Both readings need the whole ladder, which is why all five
lags are reported rather than a derived alternation score. The end-of-run
shortlist ranks on `acf1` for this reason.

Computed with `statsmodels.tsa.stattools.acf` using THE SAME CALL tsfeatures
makes internally — `acf(x, nlags=..., fft=False)`, default (biased, n-denominator)
estimator — so lags 1, 2, 3, 6 and 12 form one comparable ladder and `acf1` here
reproduces tsfeatures' `x_acf1` exactly rather than approximating it. `acf1` and
`seasonal_acf12` are taken from this same array for that reason.

`nlags` is capped at n-1, so lag k is reported when the grid has at least k+1
months and null otherwise — the identical availability rule tsfeatures applies to
`seas_acf1` (`acfx[m] if len(acfx) > m`). A lag estimated from two or three
lagged pairs is noise wearing a decimal point; `n_months_grid` is on every row so
that is checkable, and no additional flag column is added for it.

═══════════════════════════════════════════════════════════════════════════════
FEATURE 3 — RECENT vs LONG-RUN TREND DIVERGENCE
═══════════════════════════════════════════════════════════════════════════════
Ordinary least squares against a month index, twice on the same zero-filled grid:
`slope_recent_6m` over the last RECENT_TREND_MONTHS = 6 calendar months ending at
the anchor, `slope_full` over the whole grid. Both in units per month.

    trend_divergence_pct = (slope_recent_6m - slope_full) / |slope_full| * 100

Signed, as asked: negative means the recent six months are heading DOWN relative
to the long run, which is the E35500 / E25400 direction. |slope_full| in the
denominator, not slope_full, so the sign of the numerator survives — dividing by
a negative long-run slope would flip the meaning of the sign on exactly the
declining items this is for.

THE DENOMINATOR IS THE WHOLE DIFFICULTY. A flat long-run series has slope_full
~ 0 and the ratio explodes: a genuinely inert item picks up a five-figure
divergence from arithmetic alone, and sorting on the column returns noise at the
top. So the ratio is reported as NULL when |slope_full| is below
FLAT_SLOPE_FLOOR_FRAC = 0.001 of the item's mean monthly level — 0.1% of level
per month, i.e. under ~1.2% of level per year, a series with no long-run trend to
diverge FROM. Null here means "the question does not apply", which is a different
null from every other one in this file.

Because that null is common and is not a failure, three scale-free columns are
reported ALONGSIDE the ratio and are populated whenever the two fits ran:
`slope_recent_pct_per_month` and `slope_full_pct_per_month`, each slope as a
percentage of the item's own mean monthly level, and their difference
`trend_divergence_pp_per_month`, in PERCENTAGE POINTS of level per month.

THE FLOOR STOPS THE DIVISION-BY-ZERO. IT DOES NOT MAKE THE RATIO A GOOD RANK KEY,
and the current run is the demonstration. Among the 221 items with a defined
ratio, |slope_full_pct_per_month| runs from 0.112 (i.e. sitting on the floor) to
27.4, median 1.56 — so the denominator varies by a factor of ~250 across the
population, and the ratio's magnitude is mostly telling you how flat the long run
was, not how far the recent six months departed from it. 68 of those 221 items
score above 1,000% in absolute terms and 151 above 200%. Sorting on the ratio
returns the items with the smallest denominators, which is the artifact, not the
signal: E15800 reports -17,880% purely because its long-run slope is +0.12%/month.

`trend_divergence_pp_per_month` has no denominator and therefore no such tail.
It is the column to SORT on, and it is what the shortlist at the end of the run
ranks by. `trend_divergence_pct` is the headline the brief asked for and is
reported unchanged; read it per item, after the sort, not as a ranking.

Both fits require ENOUGH grid: null unless the grid has at least
MIN_DIVERGENCE_MONTHS = 12 months. Below that the "recent" six months are half
the "long run" they are being compared against, and the difference measures the
overlap, not a divergence.

This is two straight lines. It cannot tell a level shift from a slope change, it
has no changepoint, and a single outlying month inside a 6-point window moves it
hard (cross-check `spikiness`). It is a triage statistic for choosing which items
`analysis/trend_audit.py` should be pointed at — the audit that actually refits
Prophet and reads its changepoints, and that currently covers 5 of the 50
high-volume items because it is expensive. A high |trend_divergence_pct| here is
a nomination, not a finding.

═══════════════════════════════════════════════════════════════════════════════
FEATURE 4 — ZERO AND NEAR-ZERO RATES
═══════════════════════════════════════════════════════════════════════════════
On the same zero-filled grid:

  zero_rate                share of grid months at exactly 0.
  near_zero_rate           share of grid months strictly below
                           NEAR_ZERO_FRAC = 10% of the item's own NON-ZERO
                           median. Zeros are below that threshold and ARE
                           counted, so near_zero_rate >= zero_rate always.
  near_zero_rate_excl_zero the same share counting only months that sold
                           something, i.e. the genuine DIPS.

The third column is not padding. The pattern this is a proxy for — the
unconfirmed A35050 AU stockout question, where no inventory data exists to
settle it — is an otherwise-consistent series that collapses to a trickle, and
that is `near_zero_rate_excl_zero` well above 0 on an item whose `zero_rate` is
low. Reading `near_zero_rate` alone cannot distinguish "sells intermittently"
(already named by `category`) from "sells continuously except when it can't",
because the zeros dominate the first case and are exactly what the second case
is NOT about.

This remains a PROXY. It cannot see inventory, it cannot tell a stockout from a
demand collapse or a pricing change, and a high value is a candidate for asking
the question, never an answer to it.

The threshold is the item's own non-zero median, so the measure is per-item and
scale-free; `nonzero_median` is reported so a reader can see what 10% of meant.
Null for an item with no sales at all.

═══════════════════════════════════════════════════════════════════════════════
FEATURE 5 — CHANNEL MIX
═══════════════════════════════════════════════════════════════════════════════
Share of each item's quantity by channel over its full history through the
anchor, from UNFILTERED raw — the one block on this row that is not
Amazon-filtered, necessarily, since the Amazon share is the point. It is
therefore not describing the same series as every other column, and is named
`channel_share_*` throughout so it cannot be mistaken for one.

The brief names three channels; `forecast_training_data` carries five plus
blanks, and all of them are emitted rather than three:

    B2B  34,528 rows | Amazon 5,843 | Web 4,975 | (blank) 4,568
    Exhibition 224   | B2C 2

`Web` is the column the brief calls Web-Direct — the value in the data is `Web`
and the column is named for the data. The 4,568 blank-channel rows are ~10% of
the extract and go to `channel_share_unknown` rather than being dropped or
folded into a named channel: silently dropping them would make the remaining
shares sum to 1 while describing 90% of the volume, which is the kind of number
that gets acted on. Shares sum to 1 across ALL SIX columns.

`channel_share_amazon` is a per-item quantity share over full history and is NOT
the quantity `config.CHANNEL_MISMATCH_MIN_AMAZON_SHARE` is tested against —
that gate uses a trailing 12-month window ending at TEST_END and pairs the share
with two volume conditions (see `scope.detect_channel_mismatch`). The two will
not agree, and this column must not be used to second-guess that exclusion.

Shares are null, not zero, where an item's total quantity is <= 0 (returns
outweighing sales); a proportion of a non-positive total is not a proportion.

═══════════════════════════════════════════════════════════════════════════════
FEATURE 6 — CARRIED-THROUGH REFERENCE COLUMNS (computed by nobody)
═══════════════════════════════════════════════════════════════════════════════
`product_name`, `product_category`, `product_family` come from
master_product_table_data.txt (tab-separated, UTF-8 BOM) — the same file
`analysis/build_forecast_review.py` reads. It covers all 248 scope items, which
is better coverage than the DB's master-product join (config.FAMILY_UNKNOWN
exists for that gap); it carries one duplicate NAME, SN1200B, outside the
pipeline's prefix gate, deduplicated on read so the join cannot fan out rows.

`product_family` IS NOT `family_key`. `product_family` is the MASTER PRODUCT
TABLE's marketing family (TriSun, Halo, ...) and is a reporting tag.
`family_key` is the SUCCESSOR family — the pooled fit key from
`preprocessing/family_pool.py`, i.e. which series this item is modelled through.
They are unrelated and the naming keeps them apart on purpose; the pipeline's own
`family` column is the former.

`rating` is from bdm_forecasts.csv (the BDM sheet's Rating). All 248 items carry
exactly one rating each, so no tie-break is needed.

`region` is from raw_data.csv, taken as MIN(region) per item. 12 of 739 items
trade in more than one region and MIN is a deterministic tie-break, not a
finding — `prepare.series_metadata()` uses `first`, which depends on row order,
and this file needs to be reproducible. Every multi-region item additionally
carries `region_is_multi = True` so the collapse is visible in the row rather
than buried here. THE TIE-BREAK IS NOT BUSINESS-CONFIRMED: the cross-region SKUs
resolve to '1 UK' under MIN, and whether that is the right reporting region is an
open question. Read `region` on a `region_is_multi` row as "one of this item's
regions", never as "this item's region".

═══════════════════════════════════════════════════════════════════════════════
WHERE THE INPUTS COME FROM
═══════════════════════════════════════════════════════════════════════════════
The output goes to config.OUTPUT_DIR (`output/`) — the live pipeline directory —
and its absolute path is printed at the top of the run. `output_phase1/` is a
stale snapshot and is never written to.

Reading is the awkward half, because `output/` may not exist at all: it is
gitignored, and on a fresh clone or after a cleanup none of the pipeline's
cached inputs are there. Each input file is therefore resolved by searching
config.OUTPUT_DIR first and `output_phase1/` second, and the directory each file
actually came from is printed. `--input-dir` overrides the search entirely.

The fallback is not arbitrary. `demand_classification.csv` exists ONLY in
output_phase1 — `main.py` does not write it — which is the same fact
`analysis/build_forecast_review.py` documents at length about that directory.
`raw_data.csv`, `active_products.csv` and `bdm_forecasts.csv` are byte-identical
(md5) between output_phase1 and the newest pipeline snapshot output_20082026, so
the fallback is picking up the same bytes either way, not an older vintage. The
stale-copy trap that module warns about concerns `benchmark_comparison.csv` and
its schema; none of the four files read here is affected, and none is read for
anything schema-sensitive.
"""
from __future__ import annotations

import argparse
import logging
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from analysis import month_completeness  # noqa: E402
from preprocessing import scope  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

#: Searched after config.OUTPUT_DIR for each input file. See the docstring's
#: "WHERE THE INPUTS COME FROM" — this holds the only copy of
#: demand_classification.csv, and byte-identical copies of the rest.
FALLBACK_INPUT_DIR = REPO / "output_phase1"

#: Product display name / category / family. Tab-separated, UTF-8 BOM. Same file
#: analysis/build_forecast_review.py reads; kept as a literal here rather than
#: imported, because preprocessing/ must not depend on analysis/ for data.
#: (month_completeness is imported from analysis/ deliberately — that is a
#: shared CHECK with no local equivalent, not a reporting artifact.)
MASTER_TABLE = Path(r"C:\Justin\Daylight\master_product_table_data.txt")

OUTPUT_FILENAME = "item_features.csv"

# ── Seasonal period ───────────────────────────────────────────────────────────
# Monthly data, annual cycle. Every gate below that mentions "periods" is a
# multiple of this.
FREQ = 12

# ── Gates (see FEATURE 1 in the docstring for the measurements behind these) ──

#: Two full periods. STL below this saturates trend/seasonal strength at 1.0 on
#: white noise; lumpiness/stability return a fabricated 0 instead of NaN.
MIN_STL_MONTHS = 2 * FREQ

#: Not a gate — a caution flag. n = 24 is still degenerate on this
#: implementation and n = 25-26 still badly inflated; 30 (2.5 periods) is where
#: the measured inflation has materially decayed. A judgment call, sized off the
#: table in the docstring, and deliberately NOT used to null anything: raising
#: the gate above the standard two periods would be this file inventing a
#: threshold rather than reporting a caveat.
STL_CAUTION_MONTHS = 30

#: One full period. Below a single cycle a spectrum cannot separate seasonal
#: structure from noise, which is the only thing the entropy is read for.
MIN_ENTROPY_MONTHS = FREQ

#: Extra ACF lags this module adds on top of tsfeatures' lag 1 and lag 12.
EXTRA_ACF_LAGS = (2, 3, 6)

#: The window for the "recent" trend fit in FEATURE 3.
RECENT_TREND_MONTHS = 6

#: Minimum grid length for the divergence to mean anything: below twice the
#: recent window, "recent" and "long run" are largely the same months.
MIN_DIVERGENCE_MONTHS = 2 * RECENT_TREND_MONTHS

#: |slope_full| below this fraction of the item's mean monthly level counts as
#: no long-run trend at all, and the ratio is reported null rather than as the
#: five-figure artifact dividing by ~0 produces. 0.1% of level per month.
FLAT_SLOPE_FLOOR_FRAC = 0.001

#: "Near zero" is below this fraction of the item's OWN non-zero median.
NEAR_ZERO_FRAC = 0.10

#: Channel values observed in forecast_training_data, plus the blank bucket.
#: `Web` is what the brief calls Web-Direct; the column is named for the data.
#: Emitted in full rather than narrowed to three, so the shares sum to 1 over
#: the item's whole history — see FEATURE 5.
CHANNELS = ("B2B", "Amazon", "Web", "Exhibition", "B2C")
CHANNEL_UNKNOWN = "Unknown"

#: Merged verbatim from demand_classification.csv. NOT recomputed — these are
#: TRAIN_END-anchored and computed on the POOLED series; see the docstring.
DC_REUSED = ["category", "cv2", "adi", "n_nonzero_months", "tenure_months",
             "decline_ratio", "near_threshold"]

#: The features this module computes, for the null accounting and the
#: distribution summary. Order is the report's order.
TSFEATURE_COLS = ["trend_strength", "seasonal_strength", "spectral_entropy",
                  "acf1", "seasonal_acf12", "lumpiness", "stability",
                  "spikiness"]
ACF_COLS = [f"acf{k}" for k in EXTRA_ACF_LAGS]
TREND_COLS = ["slope_recent_6m", "slope_full", "slope_recent_pct_per_month",
              "slope_full_pct_per_month", "trend_divergence_pp_per_month",
              "trend_divergence_pct"]
ZERO_COLS = ["zero_rate", "near_zero_rate", "near_zero_rate_excl_zero",
             "nonzero_median"]
CHANNEL_COLS = ([f"channel_share_{c.lower()}" for c in CHANNELS]
                + [f"channel_share_{CHANNEL_UNKNOWN.lower()}"])

NEW_FEATURE_COLS = TSFEATURE_COLS + ACF_COLS + TREND_COLS + ZERO_COLS + CHANNEL_COLS

#: Columns whose nullability is a per-series data question rather than a
#: property of the item's own history. `channel_share_*` is null only for the
#: pathological non-positive-total case and `nonzero_median` only for a
#: never-sold item, so neither belongs in the "full vs partial" verdict, which
#: is about series-length gates. `trend_divergence_pct` is excluded for a
#: different reason: its null means "the item has no long-run trend to diverge
#: from", which is an answer, not a gap.
COMPLETENESS_COLS = TSFEATURE_COLS + ACF_COLS + [
    "slope_recent_6m", "slope_full", "slope_recent_pct_per_month",
    "slope_full_pct_per_month", "trend_divergence_pp_per_month"]


# ── Input resolution ──────────────────────────────────────────────────────────

def resolve_input(filename: str, override: Path | None) -> Path:
    """
    Locate one cached input, preferring config.OUTPUT_DIR over the fallback.

    Returns the path; raises FileNotFoundError naming every place looked, so a
    missing input says where it was expected rather than where it wasn't.
    """
    candidates = [override] if override else [config.OUTPUT_DIR, FALLBACK_INPUT_DIR]
    for d in candidates:
        p = Path(d) / filename
        if p.exists():
            return p
    looked = ", ".join(str(Path(d) / filename) for d in candidates)
    raise FileNotFoundError(f"{filename} not found. Looked in: {looked}")


def load_inputs(override: Path | None) -> dict[str, pd.DataFrame]:
    """
    Read the four cached inputs plus the master product table.

    Each file's resolved directory is printed: the candidate directories are not
    interchangeable, and a run that does not say which it used is not
    reproducible.
    """
    frames: dict[str, pd.DataFrame] = {}
    for name in ("demand_classification.csv", "raw_data.csv",
                 "bdm_forecasts.csv", "active_products.csv"):
        path = resolve_input(name, override)
        print(f"  {name:<28} <- {path.parent}")
        frames[name] = pd.read_csv(path, dtype=str)

    if not MASTER_TABLE.exists():
        raise FileNotFoundError(f"Master product table not found: {MASTER_TABLE}")
    print(f"  {MASTER_TABLE.name:<28} <- {MASTER_TABLE.parent}")
    master = pd.read_csv(MASTER_TABLE, sep="\t", dtype=str, encoding="utf-8-sig")
    master.columns = [c.strip() for c in master.columns]
    frames["master"] = master
    return frames


def check_scope_agrees(classes: pd.DataFrame, active: pd.DataFrame) -> None:
    """
    Warn if demand_classification.csv's item list is not inside active scope.

    The population here is taken from that CSV rather than rebuilt, so the file's
    vintage silently becomes this module's scope definition. That is the right
    trade — rebuilding scope would mean forming a second opinion about what
    active scope is, and two definitions is worse than one borrowed one — but it
    is only safe while the borrowed list still agrees with the allow-list the
    pipeline is using now. A stale classification would otherwise describe items
    that have since left scope, with nothing in the output saying so.

    Warns rather than aborts: a drifted item list is a reason to re-run
    `analysis.demand_classification_report`, not a reason to withhold 248 rows of
    features. The count is printed either way.
    """
    scope_items = set(active["item_code"].astype(str).str.strip())
    ours = set(classes["item_code"].astype(str).str.strip())
    extra = sorted(ours - scope_items)
    if extra:
        print(f"  WARNING: {len(extra)} item(s) in demand_classification.csv are "
              f"NOT on the active-product allow-list — that file may be stale. "
              f"Re-run analysis.demand_classification_report. "
              f"First few: {extra[:10]}")
    else:
        print(f"  scope check: all {len(ours):,} classified items are on the "
              f"active-product allow-list ({len(scope_items):,} items).")


# ── Anchor + monthly grid ─────────────────────────────────────────────────────

def resolve_anchor(raw: pd.DataFrame,
                   use_max: bool) -> tuple[pd.Timestamp, pd.DataFrame]:
    """
    The date anchor every feature is computed through, resolved FROM THE DATA.

    Returns (anchor, evidence). See the docstring's date-anchor section: the
    default is the latest month that passes `month_completeness`, not
    MAX(year_month), because the newest month in the extract is routinely a
    partial upstream pull and `trend_divergence_pct` fits a line to exactly the
    six months it would sit in.
    """
    month, evidence = month_completeness.latest_complete_month(raw)
    if use_max:
        month = str(raw["year_month"].astype(str).str.strip().max())
    return pd.Timestamp(month + "-01"), evidence


def _monthly_by_item(raw: pd.DataFrame, anchor: pd.Timestamp) -> pd.DataFrame:
    """
    item_code x month quantities, months <= anchor. Columns: item_code, ds, y.

    `prepare.prepare()` in all but the window: that function clips to
    config.TRAIN_START..config.TEST_END, and TEST_END is BEFORE the anchor, so
    calling it would discard the anchor month itself. The clip is a
    training-window concern and correct where it lives; this is discovery.
    Only the upper bound differs — the lower bound is irrelevant because every
    grid starts at its own first sale anyway.
    """
    df = raw.copy()
    df["item_code"] = df["item_code"].astype(str).str.strip()
    df["monthly_qty"] = pd.to_numeric(df["monthly_qty"], errors="coerce").fillna(0.0)
    df["ds"] = pd.to_datetime(df["year_month"].astype(str).str.strip() + "-01",
                              errors="coerce")
    df = df.dropna(subset=["ds"])
    df = df[df["ds"] <= anchor]
    return (df.groupby(["item_code", "ds"], as_index=False)["monthly_qty"]
              .sum().rename(columns={"monthly_qty": "y"}))


def zero_filled_grid(months: pd.DataFrame, anchor: pd.Timestamp) -> pd.Series:
    """
    One item's series as a complete monthly grid, first non-zero month → anchor.

    Empty Series if the item never sold. See the docstring's grid section for
    why the start is the first sale and not config.TRAIN_START.
    """
    nz = months[months["y"] > 0]
    if nz.empty:
        return pd.Series(dtype=float)
    idx = pd.date_range(nz["ds"].min(), anchor, freq="MS")
    return (months.set_index("ds")["y"].astype(float)
                  .reindex(idx, fill_value=0.0))


# ── FEATURE 1: tsfeatures, gated ──────────────────────────────────────────────

def tsfeature_block(y: np.ndarray) -> dict[str, float | None]:
    """
    The FFORMA features for one grid, or None where the grid is too short.

    Calls the `tsfeatures` package's own feature functions per series rather
    than its batch entry point, so each statistic can be gated and each null
    counted. THE GATES ARE NOT OPTIONAL: below their minimums these functions
    return fabricated numbers, not NaN — see FEATURE 1 in the module docstring
    for the measured table.

    `acf1` and `seasonal_acf12` are NOT taken from here; `acf_ladder()` supplies
    them, so all five reported lags come from one identical call. That call
    reproduces tsfeatures' internal one exactly (statsmodels `acf(..., fft=False)`),
    so `acf1` is its `x_acf1` and `seasonal_acf12` is its `seas_acf1`, not an
    approximation of either.
    """
    from tsfeatures.tsfeatures import (entropy, lumpiness, stability,
                                       stl_features)

    out: dict[str, float | None] = {c: None for c in TSFEATURE_COLS}
    n = len(y)
    if n == 0:
        return out

    def _clean(v):
        """A NaN out of the package is a null here; nothing else is coerced."""
        if v is None:
            return None
        v = float(v)
        return None if not np.isfinite(v) else v

    if n >= MIN_STL_MONTHS:
        # A degenerate STL raises inside the package and is caught there,
        # returning NaN for the whole block; _clean turns that into nulls.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            stl = stl_features(y, FREQ)
            out["trend_strength"] = _clean(stl.get("trend"))
            out["seasonal_strength"] = _clean(stl.get("seasonal_strength"))
            out["spikiness"] = _clean(stl.get("spike"))
            out["lumpiness"] = _clean(lumpiness(y, FREQ).get("lumpiness"))
            out["stability"] = _clean(stability(y, FREQ).get("stability"))

    if n >= MIN_ENTROPY_MONTHS:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            out["spectral_entropy"] = _clean(entropy(y, FREQ).get("entropy"))

    return out


# ── FEATURE 2: the ACF ladder ─────────────────────────────────────────────────

def acf_ladder(y: np.ndarray) -> dict[int, float | None]:
    """
    Autocorrelation at lags 1, 2, 3, 6 and 12 from ONE statsmodels call.

    Deliberately the same call tsfeatures makes internally —
    `acf(x, nlags=..., fft=False)`, default biased estimator — so the lags this
    module adds are on the same footing as the two it inherits. `nlags` is capped
    at n-1, so a lag longer than the grid is simply absent and reported null,
    which is the identical availability rule tsfeatures applies to `seas_acf1`.

    A constant grid has zero variance and no defined ACF at any lag; statsmodels
    returns NaN there and it stays null.
    """
    from statsmodels.tsa.stattools import acf

    lags = sorted({1, *EXTRA_ACF_LAGS, FREQ})
    out: dict[int, float | None] = {k: None for k in lags}
    n = len(y)
    if n < 2:
        return out

    nlags = min(max(lags), n - 1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            values = acf(y, nlags=nlags, fft=False)
        except Exception:
            return out

    for k in lags:
        if k < len(values) and np.isfinite(values[k]):
            out[k] = float(values[k])
    return out


# ── FEATURE 3: recent vs long-run trend ───────────────────────────────────────

def _ols_slope(y: np.ndarray) -> float | None:
    """Units-per-month slope of an OLS fit against a 0..n-1 month index."""
    n = len(y)
    if n < 2:
        return None
    x = np.arange(n, dtype=float)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            slope = float(np.polyfit(x, y.astype(float), 1)[0])
        except Exception:
            return None
    return slope if np.isfinite(slope) else None


def trend_divergence(grid: pd.Series) -> dict[str, float | None]:
    """
    Recent 6-month slope against the full-history slope, and their divergence.

    See FEATURE 3 in the module docstring. The two `*_pct_per_month` columns are
    the scale-free pair to read when `trend_divergence_pct` is null — which it is
    by design whenever the long-run trend is flat enough that a ratio against it
    would be arithmetic noise rather than a finding.
    """
    out: dict[str, float | None] = {c: None for c in TREND_COLS}
    if len(grid) < MIN_DIVERGENCE_MONTHS:
        return out

    y = grid.to_numpy(dtype=float)
    level = float(np.mean(y))

    slope_full = _ols_slope(y)
    slope_recent = _ols_slope(y[-RECENT_TREND_MONTHS:])
    out["slope_full"] = slope_full
    out["slope_recent_6m"] = slope_recent

    if level > 0:
        if slope_full is not None:
            out["slope_full_pct_per_month"] = slope_full / level * 100.0
        if slope_recent is not None:
            out["slope_recent_pct_per_month"] = slope_recent / level * 100.0
        if slope_full is not None and slope_recent is not None:
            # Percentage POINTS of level per month. No denominator beyond the
            # item's own level, so unlike the ratio below it has no tail and is
            # the safe key to sort the population on. See FEATURE 3.
            out["trend_divergence_pp_per_month"] = (
                (slope_recent - slope_full) / level * 100.0)

    if slope_full is None or slope_recent is None or level <= 0:
        return out

    # The flat-long-run guard. Null here means "the item has no long-run trend
    # to diverge from", not "the computation failed" — read the two
    # *_pct_per_month columns instead.
    if abs(slope_full) < FLAT_SLOPE_FLOOR_FRAC * level:
        return out

    out["trend_divergence_pct"] = (slope_recent - slope_full) / abs(slope_full) * 100.0
    return out


# ── FEATURE 4: zero and near-zero rates ───────────────────────────────────────

def zero_rates(grid: pd.Series) -> dict[str, float | None]:
    """
    Share of months at zero, and below 10% of the item's own non-zero median.

    `near_zero_rate` counts zeros (they are below the threshold);
    `near_zero_rate_excl_zero` counts only months that sold something — the
    genuine dips, which is the half of this that the stockout proxy turns on.
    See FEATURE 4.
    """
    out: dict[str, float | None] = {c: None for c in ZERO_COLS}
    n = len(grid)
    if n == 0:
        return out

    y = grid.to_numpy(dtype=float)
    nz = y[y > 0]
    out["zero_rate"] = float((y == 0).sum()) / n
    if nz.size == 0:
        return out

    med = float(np.median(nz))
    out["nonzero_median"] = med
    threshold = NEAR_ZERO_FRAC * med
    out["near_zero_rate"] = float((y < threshold).sum()) / n
    out["near_zero_rate_excl_zero"] = float(((y > 0) & (y < threshold)).sum()) / n
    return out


# ── FEATURE 5: channel mix ────────────────────────────────────────────────────

def channel_mix(raw: pd.DataFrame, anchor: pd.Timestamp) -> pd.DataFrame:
    """
    Per-item quantity share by channel over full history through the anchor.

    Computed on UNFILTERED raw — the Amazon rows are the point — so this is the
    one block on the output row that does not describe the Amazon-filtered
    series every other feature is computed on. Blank channels go to
    `channel_share_unknown` rather than being dropped; the shares sum to 1 across
    all six columns. See FEATURE 5.
    """
    df = raw.copy()
    df["item_code"] = df["item_code"].astype(str).str.strip()
    df["monthly_qty"] = pd.to_numeric(df["monthly_qty"], errors="coerce").fillna(0.0)
    df["ds"] = pd.to_datetime(df["year_month"].astype(str).str.strip() + "-01",
                              errors="coerce")
    df = df.dropna(subset=["ds"])
    df = df[df["ds"] <= anchor]

    ch = df["channel"].astype(str).str.strip()
    df["channel_bucket"] = ch.where(ch.isin(CHANNELS), CHANNEL_UNKNOWN)

    wide = df.pivot_table(index="item_code", columns="channel_bucket",
                          values="monthly_qty", aggfunc="sum", fill_value=0.0)
    for c in list(CHANNELS) + [CHANNEL_UNKNOWN]:
        if c not in wide.columns:
            wide[c] = 0.0
    wide = wide[list(CHANNELS) + [CHANNEL_UNKNOWN]]

    total = wide.sum(axis=1)
    shares = wide.div(total, axis=0)
    # A share of a non-positive total is not a share (returns can outweigh
    # sales over a short history). Null, not zero, and counted in the summary.
    shares.loc[total <= 0, :] = np.nan
    shares.columns = [f"channel_share_{c.lower()}" for c in shares.columns]
    shares["channel_total_qty"] = total
    return shares.reset_index()


# ── FEATURE 6: carried-through reference columns ──────────────────────────────

def reference_columns(raw: pd.DataFrame, bdm: pd.DataFrame,
                      master: pd.DataFrame) -> pd.DataFrame:
    """
    region / rating / product_category / product_family / product_name per item.

    Computed by nobody — every column here is carried through for readability.
    See FEATURE 6, in particular why `product_family` is not `family_key` and
    why `region` on a `region_is_multi` row is one of the item's regions rather
    than the item's region.
    """
    r = raw[["item_code", "region"]].copy()
    r["item_code"] = r["item_code"].astype(str).str.strip()
    r["region"] = r["region"].astype(str).str.strip().replace({"nan": np.nan, "": np.nan})
    reg = (r.dropna(subset=["region"]).groupby("item_code")["region"]
             .agg(region="min", region_n="nunique").reset_index())
    reg["region_is_multi"] = reg["region_n"] > 1
    reg = reg.drop(columns=["region_n"])

    b = bdm[["item_code", "rating"]].copy()
    b["item_code"] = b["item_code"].astype(str).str.strip()
    b["rating"] = b["rating"].astype(str).str.strip().replace({"nan": np.nan, "": np.nan})
    rating = (b.dropna(subset=["rating"]).groupby("item_code", as_index=False)
                .agg(rating=("rating", "first")))

    m = master.copy()
    m["item_code"] = m["NAME"].astype(str).str.strip()
    # One duplicate NAME (SN1200B) sits outside the pipeline's prefix gate;
    # dropped here regardless so the join cannot fan out item rows.
    m = m.drop_duplicates(subset=["item_code"], keep="first")
    m = m.rename(columns={"DISPLAY NAME": "product_name",
                          "PRODUCT CATEGORY": "product_category",
                          "PRODUCT FAMILY": "product_family"})
    m = m[["item_code", "product_name", "product_category", "product_family"]]

    return (reg.merge(rating, on="item_code", how="outer")
               .merge(m, on="item_code", how="outer"))


# ── Assembly ──────────────────────────────────────────────────────────────────

def build_features(raw: pd.DataFrame, classes: pd.DataFrame, bdm: pd.DataFrame,
                   master: pd.DataFrame, anchor: pd.Timestamp) -> pd.DataFrame:
    """
    One row per active-scope item_code, every feature computed through `anchor`.

    The population is `classes["item_code"]` verbatim — that file IS
    `demand_classification.classify_scope()`'s output, so taking its item list is
    taking that function's active-scope universe rather than forming a second
    opinion about what active scope means. `check_scope_agrees()` is the guard on
    that assumption, and it is the only thing active_products.csv is read for.

    Every series measured is the item's OWN standalone series, never its pooled
    family's; `family_key` / `is_pooled` ride along for reference only. See the
    grain section of the module docstring.
    """
    # Channel mix needs the Amazon rows; everything else must not see them.
    channels = channel_mix(raw, anchor)
    refs = reference_columns(raw, bdm, master)

    filtered, _excluded = scope.filter_amazon_channel(raw)
    monthly = _monthly_by_item(filtered, anchor)
    by_item = {code: grp for code, grp in monthly.groupby("item_code")}

    empty = pd.DataFrame({"ds": pd.Series(dtype="datetime64[ns]"),
                          "y": pd.Series(dtype=float)})
    items = classes["item_code"].astype(str).str.strip().tolist()
    rows: list[dict] = []

    for code in items:
        months = by_item.get(code, empty)
        grid = zero_filled_grid(months, anchor)
        y = grid.to_numpy(dtype=float)
        n = len(grid)
        sold = grid > 0

        row: dict = {"item_code": code}
        row["first_sale_month"] = grid.index.min().strftime("%Y-%m") if n else None
        row["last_sale_month"] = (grid[sold].index.max().strftime("%Y-%m")
                                  if sold.any() else None)
        row["n_months_grid"] = n
        row["n_nonzero_grid"] = int(sold.sum())
        row["total_qty_grid"] = float(grid.sum()) if n else 0.0

        row.update(tsfeature_block(y))
        ladder = acf_ladder(y)
        row["acf1"] = ladder.get(1)
        row["seasonal_acf12"] = ladder.get(FREQ)
        for k in EXTRA_ACF_LAGS:
            row[f"acf{k}"] = ladder.get(k)
        row["stl_short_window"] = bool(0 < n < STL_CAUTION_MONTHS)

        row.update(trend_divergence(grid))
        row.update(zero_rates(grid))
        rows.append(row)

    feats = pd.DataFrame(rows)

    dc = classes.copy()
    dc["item_code"] = dc["item_code"].astype(str).str.strip()
    dc = dc[["item_code", "family_key", "is_pooled"]
            + [c for c in DC_REUSED if c in dc.columns]]
    for col in ("cv2", "adi", "n_nonzero_months", "tenure_months", "decline_ratio"):
        if col in dc.columns:
            dc[col] = pd.to_numeric(dc[col], errors="coerce")

    out = (feats.merge(dc, on="item_code", how="left")
                .merge(refs, on="item_code", how="left")
                .merge(channels, on="item_code", how="left"))

    # `full` means every series-length gate cleared, i.e. no feature was
    # withheld because the item's own history was too short. It says nothing
    # about whether the values are GOOD — see stl_short_window for that.
    missing = out[COMPLETENESS_COLS].isna().sum(axis=1)
    out["n_null_features"] = missing.astype(int)
    out["feature_completeness"] = np.where(missing == 0, "full", "partial")

    ordered = (["item_code", "product_name", "product_category", "product_family",
                "region", "region_is_multi", "rating", "family_key", "is_pooled"]
               + DC_REUSED
               + ["first_sale_month", "last_sale_month", "n_months_grid",
                  "n_nonzero_grid", "total_qty_grid"]
               + TSFEATURE_COLS + ["stl_short_window"] + ACF_COLS
               + TREND_COLS + ZERO_COLS
               + CHANNEL_COLS + ["channel_total_qty"]
               + ["feature_completeness", "n_null_features"])
    ordered = [c for c in ordered if c in out.columns]
    return out[ordered].sort_values("item_code").reset_index(drop=True)


# ── Reporting ─────────────────────────────────────────────────────────────────

def print_summary(feats: pd.DataFrame, anchor: pd.Timestamp,
                  evidence: pd.DataFrame, out_path: Path) -> None:
    sep = "=" * 92
    sub = "-" * 92
    max_month = str(evidence["month"].max())
    last = evidence.iloc[-1]

    print(f"\n{sep}\nSETUP\n{sep}")
    print(f"  anchor month        : {anchor.strftime('%Y-%m')}  "
          f"— every feature is computed through this month, inclusive")
    print(f"  rejected anchors    : config.TRAIN_END {config.TRAIN_END} "
          f"(training-window boundary, under review) | "
          f"MAX(year_month) {max_month} "
          f"({'PARTIAL pull' if not bool(last['is_complete']) else 'complete'})")
    print(f"  grain               : per item_code, STANDALONE series — pooled "
          f"families are NOT pooled here; family_key / is_pooled are reference "
          f"columns only")
    print(f"  series measured on  : zero-filled monthly grid, first non-zero "
          f"month → anchor, Amazon rows removed (channel_share_* excepted)")
    print(f"  merged from demand_classification.csv, NOT recomputed: "
          f"{', '.join(DC_REUSED)}")
    print(f"      ^ those seven are TRAIN_END-anchored ({config.TRAIN_END}) and "
          f"computed on the POOLED series — a different window AND a different")
    print(f"        series from every feature this module computes. Do not read "
          f"them as standalone counterparts of the columns beside them.")
    print(f"  output              : {out_path}")

    n = len(feats)
    full = int((feats["feature_completeness"] == "full").sum())
    print(f"\n{sep}\nCOVERAGE — {n:,} active-scope items\n{sep}")
    print(f"  full feature set     : {full:,}  "
          f"(every series-length gate cleared)")
    print(f"  partial              : {n - full:,}")

    if n - full:
        print(f"\n{sub}\n  WHY partial — the gate each item failed "
              f"(one item can fail more than one)\n{sub}")
        reasons = [
            (f"grid < {MIN_STL_MONTHS} months  → STL block null "
             f"(trend / seasonal / spikiness / lumpiness / stability)",
             feats["n_months_grid"] < MIN_STL_MONTHS),
            (f"grid < {MIN_ENTROPY_MONTHS} months  → spectral_entropy null",
             feats["n_months_grid"] < MIN_ENTROPY_MONTHS),
            (f"grid < {MIN_DIVERGENCE_MONTHS} months  → both trend slopes null",
             feats["n_months_grid"] < MIN_DIVERGENCE_MONTHS),
            (f"grid <= {FREQ} months → seasonal_acf12 null (lag exceeds grid)",
             feats["n_months_grid"] <= FREQ),
            ("no sales at all through the anchor → every feature null",
             feats["n_nonzero_grid"] == 0),
        ]
        for label, mask in reasons:
            print(f"    {int(mask.sum()):>4}  {label}")

    print(f"\n{sub}\n  Nulls per computed column — null is NOT zero; see the "
          f"module docstring, FEATURE 1\n{sub}")
    rows = []
    for col in NEW_FEATURE_COLS:
        if col in feats.columns:
            nulls = int(feats[col].isna().sum())
            rows.append({"feature": col, "computed": n - nulls, "null": nulls,
                         "pct_null": round(nulls / max(n, 1) * 100, 1)})
    print(pd.DataFrame(rows).to_string(index=False))

    caution = int(feats["stl_short_window"].sum())
    print(f"\n  stl_short_window = True on {caution:,} row(s): the STL block "
          f"cleared its {MIN_STL_MONTHS}-month gate but the grid is under "
          f"{STL_CAUTION_MONTHS} months,")
    print(f"  where the package's trend / seasonal strengths are measurably "
          f"inflated (1.000 / 1.000 on white noise at n=24). Coin-flip values.")

    print(f"\n{sep}\nDISTRIBUTION of each new feature — min / median / max over "
          f"the rows where it is computed\n{sep}")
    dist = []
    for col in NEW_FEATURE_COLS:
        if col not in feats.columns:
            continue
        s = pd.to_numeric(feats[col], errors="coerce").dropna()
        if s.empty:
            dist.append({"feature": col, "n": 0, "min": None, "median": None,
                         "max": None})
        else:
            dist.append({"feature": col, "n": int(s.size),
                         "min": round(float(s.min()), 4),
                         "median": round(float(s.median()), 4),
                         "max": round(float(s.max()), 4)})
    print(pd.DataFrame(dist).to_string(index=False))

    print(f"\n  Scale note: spikiness, lumpiness and stability are computed on "
          f"raw quantities and are NOT comparable across items of different")
    print(f"  volume — they rank an item against its own history. Every other "
          f"row above is scale-free and does compare across items.")

    print(f"\n{sep}\nSHORTLISTS — what this table was built to surface. "
          f"Top 5 each; NOTHING here is a finding.\n{sep}")

    def _show(title: str, frame: pd.DataFrame, cols: list[str]) -> None:
        print(f"\n  {title}")
        cols = [c for c in cols if c in frame.columns]
        if frame.empty:
            print("    (none)")
        else:
            print(frame[cols].to_string(index=False))

    # Grid floor on the alternation list, and it is not decoration. The scope
    # contains codes whose first sale is two months before the anchor; a lag-2
    # ACF there is computed from a single lagged pair and is arithmetic, not a
    # pattern. Without this the list is topped by 2-4 month launches every run.
    alt = feats[(feats["acf1"] < 0)
                & (feats["n_months_grid"] >= MIN_DIVERGENCE_MONTHS)].copy()
    _show(f"Month-to-month alternation (most negative acf1, grid >= "
          f"{MIN_DIVERGENCE_MONTHS} months) — the UN1050 shape. "
          f"{len(alt)} item(s) with acf1 < 0. Read acf2/acf3 across: positive "
          f"acf2 = the 2-cycle PERSISTS, ~0 = it damps (UN1050's own case):",
          alt.nsmallest(5, "acf1"),
          ["item_code", "product_family", "acf1", "acf2", "acf3", "acf6",
           "seasonal_acf12", "n_months_grid"])

    # Ranked on the percentage-POINT divergence, NOT on trend_divergence_pct.
    # The ratio's denominator varies ~250x across this population, so sorting on
    # it returns the flattest long runs rather than the sharpest departures —
    # see FEATURE 3. The ratio is still shown, per item, beside the rank key.
    soft = feats.dropna(subset=["trend_divergence_pp_per_month"])
    _show(f"Recent softening vs long run (most negative "
          f"trend_divergence_pp_per_month) — the E35500 / E25400 shape. "
          f"{len(soft)} item(s) with both fits:",
          soft.nsmallest(5, "trend_divergence_pp_per_month"),
          ["item_code", "product_family", "trend_divergence_pp_per_month",
           "slope_recent_pct_per_month", "slope_full_pct_per_month",
           "trend_divergence_pct", "decline_ratio", "category"])

    dips = (feats[feats["zero_rate"] < 0.10]
            .dropna(subset=["near_zero_rate_excl_zero"]))
    _show(f"Near-zero dips on an otherwise-consistent series (zero_rate < 10%, "
          f"highest near_zero_rate_excl_zero) — the A35050 PROXY, not an answer:",
          dips.nlargest(5, "near_zero_rate_excl_zero"),
          ["item_code", "product_family", "region", "zero_rate",
           "near_zero_rate_excl_zero", "nonzero_median", "category"])

    print(f"\n{sub}")
    print("  Nothing above is adopted, routed on, or fitted. This file is a "
          "reading surface; the decisions are a human's.")


# ── Entry point ───────────────────────────────────────────────────────────────

def _force_utf8_stdout() -> None:
    """
    Stop a Windows console codepage from killing a completed run.

    Python resolves stdout's encoding from the Windows locale (cp1252 here), and
    the arrows and dashes in the summary below are outside it — so a run that has
    already computed every feature AND written the CSV dies with
    UnicodeEncodeError while printing its own report. `errors="replace"` is the
    belt to the reconfigure's braces: on a console that genuinely cannot render a
    character it degrades to '?' rather than raising.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # not a reconfigurable stream
            pass


def main() -> int:
    _force_utf8_stdout()
    ap = argparse.ArgumentParser(
        description="Per-item time-series feature extraction (read-only discovery)")
    ap.add_argument("--input-dir", default=None,
                    help="Directory holding the cached input CSVs. Overrides the "
                         "config.OUTPUT_DIR → output_phase1 search. The OUTPUT "
                         "always goes to config.OUTPUT_DIR regardless.")
    ap.add_argument("--anchor-max-month", action="store_true",
                    help="Anchor on MAX(year_month) instead of the latest "
                         "COMPLETE month. Not recommended — the newest month in "
                         "the extract is routinely a partial upstream pull.")
    args = ap.parse_args()

    out_dir = Path(config.OUTPUT_DIR).resolve()
    out_path = out_dir / OUTPUT_FILENAME
    print("=" * 92)
    print("FEATURE EXTRACTION — per-item discovery (fits nothing, routes nothing)")
    print("=" * 92)
    print(f"Output file : {out_path}")
    if not out_dir.exists():
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"  NOTE: {out_dir} did not exist and was created. It is "
              f"config.OUTPUT_DIR — the live pipeline directory — and is "
              f"gitignored;")
        print(f"        it currently holds only this file. output_phase1/ is a "
              f"stale snapshot and is never written to.")

    print("\nInputs:")
    override = Path(args.input_dir).resolve() if args.input_dir else None
    frames = load_inputs(override)

    check_scope_agrees(frames["demand_classification.csv"],
                       frames["active_products.csv"])

    raw = frames["raw_data.csv"]
    anchor, evidence = resolve_anchor(raw, args.anchor_max_month)
    print(f"\nANCHOR MONTH: {anchor.strftime('%Y-%m')}   "
          f"(every feature is computed through this month, inclusive)")
    print(f"  MAX(year_month) in the extract = {evidence['month'].max()} | "
          f"config.TRAIN_END = {config.TRAIN_END} | "
          f"config.TEST_END = {config.TEST_END}")
    if args.anchor_max_month:
        print("  WARNING: --anchor-max-month is set. The newest month in the "
              "extract is routinely a PARTIAL upstream pull, and the 6-month "
              "trend fit")
        print("           sits directly on it. This mode exists to inspect the "
              "default choice, not to be relied on.")
    else:
        last = evidence.iloc[-1]
        if not bool(last["is_complete"]):
            print(f"  Skipped {last['month']}: {last['reason']}")

    feats = build_features(raw, frames["demand_classification.csv"],
                           frames["bdm_forecasts.csv"], frames["master"], anchor)

    feats.to_csv(out_path, index=False)
    print(f"\nWrote {out_path}  ({len(feats):,} rows x {len(feats.columns)} columns)")

    print_summary(feats, anchor, evidence, out_path)
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    sys.exit(main())
