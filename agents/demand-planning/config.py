import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# ── Database ──────────────────────────────────────────────────────────────────
DB_SERVER   = os.getenv("DB_SERVER",   "daylight-powerbi-db-1.database.windows.net")
DB_NAME     = os.getenv("DB_NAME",     "")          # REQUIRED – set in .env
# DB_USER / DB_PASSWORD deliberately have NO default: credentials must come from
# .env or the process environment. data.loader._get_engine() raises a clear
# error if either (or DB_NAME) is empty, so a missing .env fails fast instead of
# silently connecting with a baked-in login. Offline runs (--cached) never touch
# the database, so importing this module without credentials is fine.
DB_USER     = os.getenv("DB_USER",     "")          # REQUIRED for any DB access
DB_PASSWORD = os.getenv("DB_PASSWORD", "")          # REQUIRED for any DB access
DB_DRIVER   = os.getenv("DB_DRIVER",   "ODBC Driver 18 for SQL Server")
DB_PORT     = int(os.getenv("DB_PORT", "1433"))

# ── Date windows (YYYY-MM strings) ────────────────────────────────────────────
# Anchored on TEST_END = the most recent COMPLETE month in forecast_training_data,
# as determined by analysis/month_completeness.py (NOT MAX(year_month), and NOT
# today's calendar date). Every other constant here is a fixed offset from it:
#   TRAIN_END = -6, TEST_START = -5, FORECAST_START = +1, FORECAST_END = +6.
# Re-derive the proposed anchor any time with `python -m analysis.window_proposal`.
# ONE static anchor, everything else derived. TEST_END below is only the FALLBACK
# window: it is what a run WITHOUT --auto-window uses, and what --auto-window
# compares the newest complete month against (a run whose newest complete month
# is not after it keeps this window; with --require-roll it exits 3 instead).
# A scheduled run (run_forecast_cycle.sh) overrides all five constants in memory
# from the data, so this value never needs editing for the cron path. Bump it by
# hand only when you want manual / analysis-script runs to default to a newer
# window. TRAIN_END, TEST_START, FORECAST_START and FORECAST_END are computed from
# it, so they can no longer drift out of step with each other.
OFFSETS_FROM_TEST_END = {
    "TRAIN_END": -6, "TEST_START": -5, "TEST_END": 0,
    "FORECAST_START": 1, "FORECAST_END": 6,      # 6-month forward forecast
}


def shift_month(ym: str, months: int) -> str:
    """'YYYY-MM' shifted by `months` (negative = earlier). No pandas needed."""
    total = int(ym[:4]) * 12 + (int(ym[5:7]) - 1) + months
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


TRAIN_START    = "2018-01"   # ~96 months of training data
TEST_END       = "2026-08"   # fallback anchor: newest complete month (hand-set 2026-09-06)
TRAIN_END      = shift_month(TEST_END, OFFSETS_FROM_TEST_END["TRAIN_END"])            # 2026-02
TEST_START     = shift_month(TEST_END, OFFSETS_FROM_TEST_END["TEST_START"])           # 2026-03, 6 months held-out validation
FORECAST_START = shift_month(TEST_END, OFFSETS_FROM_TEST_END["FORECAST_START"])       # 2026-09
FORECAST_END   = shift_month(TEST_END, OFFSETS_FROM_TEST_END["FORECAST_END"])         # 2027-02, 6-month forecast delivered to BDMs

# ── Forecast grouping ─────────────────────────────────────────────────────────
# One Prophet model is trained per unique combination of these columns.
# One model per Item (the demand grain). BDM was dropped from the modelling
# grain (2026-07): it is sales-credit attribution, not a demand driver, so an
# item's demand history is now aggregated across all BDMs into a single series.
# BDM survives only as reporting context; Family (see FAMILY_* below) is the new
# reporting tag joined onto the item-level output.
GROUP_COLS = ["item_code"]

# ── Prophet hyperparameters ───────────────────────────────────────────────────
# `yearly_seasonality = 3` is a deliberate departure from Prophet's default.
# `True` resolves to 10 Fourier term pairs — 20 free parameters fitted against
# 6-8 repeats of a yearly cycle — which in `multiplicative` mode overfits into
# seasonal excursions of hundreds of percent around trend at daily resolution
# (invisible at the monthly resolution this pipeline forecasts at). Order 3 was
# validated by analysis/seasonality_experiment.py under rolling-origin CV inside
# the training window: it beat order 10 on 102/152 fitted series by CV MAPE and
# 121/152 by CV WAPE, median -10.2% MAPE. It is a single global value applied to
# every series — NOT tuned per product.
#
# `seasonality_mode` stays multiplicative on purpose: the same experiment found
# switching to additive beat the default on only 73/152 series (a coin flip) and
# did not reduce the seasonal swing at all.
#
# `seasonality_prior_scale` stays 10.0: the tighter priors that were tested (3.0,
# 1.0) only beat the default in combination with a per-product Fourier order,
# which is per-series tuning and is out of scope here.
PROPHET_PARAMS = dict(
    yearly_seasonality      = 3,                 # Fourier order (was True = 10)
    weekly_seasonality      = False,
    daily_seasonality       = False,
    seasonality_mode        = "multiplicative",  # handles multiplicative growth in sales
    changepoint_prior_scale = 0.05,              # standard segment — see below
    seasonality_prior_scale = 10.0,
    interval_width          = 0.95,              # 95 % confidence bands
)

# ── Segment-level trend flexibility ───────────────────────────────────────────
# `changepoint_prior_scale` is the Laplace prior scale on each candidate trend-
# rate adjustment. At 0.05 a real, sustained level shift is regularised away as
# noise, and the fitted trend sits at the old level while sales run somewhere
# else (analysis/trend_audit.py: 38 of the 68 worst series carry exactly that
# signature — zero changepoints clearing |delta| > 0.01).
#
# The remedy is applied to ONE SEGMENT, not to every series and not per SKU:
# series whose successor family carries a confirmed `estimated_changeover` date
# in dbo.product_successor_review — i.e. the business already knows a real level
# shift happened, so loosening the prior lets Prophet find a change it has
# independent evidence for. analysis/changepoint_experiment.py measured this
# split under rolling-origin CV: on the known-changeover segment 0.25 beat 0.05
# on 19/22 families by CV MAPE (median -30.7%) and 20/22 by CV WAPE. On series
# with no known changeover date the same value wins only 29/43 — not enough to
# justify loosening the trend everywhere, which is why this is segmented.
#
# 0.25 rather than 0.50: the two tie on win rate (19/22), but 0.50 is the largest
# value in the tested grid [0.05, 0.1, 0.25, 0.5] — a boundary solution the grid
# does not bracket — and its worst single-family regression is +127% against
# 0.25's +72%.
#
# Segment membership is resolved by preprocessing.family_pool.known_changeover_keys().
CHANGEPOINT_PRIOR_KNOWN_CHANGEOVER = 0.25

# Minimum months of training data required to attempt a forecast for a series.
# 24 = two full yearly cycles, the minimum to detect yearly seasonality.
MIN_TRAIN_MONTHS = 24

# ── Demand-pattern classification (Syntetos-Boylan) ───────────────────────────
# Cut-offs for the SBC quadrant that preprocessing/demand_classification.py
# assigns to every active-scope item. The two thresholds are the standard
# published SBC values, NOT numbers derived from this dataset:
#
#   ADI  (Average Demand Interval) = calendar months of tenure divided by the
#        number of months the series actually sold in. 1.0 = sells every month.
#        1.32 is the interval at which Croston's estimator starts to beat
#        exponential smoothing on intermittent series.
#   CV^2 = squared coefficient of variation of the monthly quantity over those
#         non-zero months. 0.49 (= 0.7^2) is the corresponding size-variability
#         cut-off.
#
#           CV^2 <  0.49    CV^2 >= 0.49
#   ADI <  1.32   Smooth        Erratic
#   ADI >= 1.32   Intermittent  Lumpy
#
# SB_MIN_MONTHS_TO_CLASSIFY is a judgment call, not an SBC value: below four
# non-zero months both statistics are being read off a handful of points (CV^2
# is undefined below two), and a quadrant assigned from that is a guess wearing
# a label. Such series are reported as 'Insufficient Data to Classify' instead.
#
# The classification is descriptive; preprocessing/model_routing.py is what
# turns it into a model choice. Nothing in the fitting path reads these values.
SB_ADI_THRESHOLD          = 1.32
SB_CV2_THRESHOLD          = 0.49
SB_MIN_MONTHS_TO_CLASSIFY = 4

# ── Croston/TSB eligibility ───────────────────────────────────────────────────
# PROVISIONAL — a placeholder, not a validated threshold. Stated here so nobody
# has to read the code to find that out.
#
# Minimum NON-ZERO months a series needs before an intermittent-demand model
# (Croston / TSB) is worth fitting to it. This is a different quantity from
# MIN_TRAIN_MONTHS, which counts CALENDAR months: Prophet needs two full yearly
# cycles to have a seasonality to find, whereas Croston does not care how long
# the calendar is — it estimates a demand SIZE and an inter-arrival INTERVAL
# from the occurrences alone, so what it needs is enough occurrences. A series
# with 60 calendar months and 6 sales has plenty of the first and almost none of
# the second.
#
# 10 is a starting value chosen by reasoning, not measurement:
#   - n occurrences give only n-1 observed intervals, so below ~10 the interval
#     estimate rests on a handful of gaps and the smoothing initialisation
#     dominates the level for the whole series;
#   - it is deliberately well above SB_MIN_MONTHS_TO_CLASSIFY (4), which is the
#     floor for DESCRIBING a demand pattern. Being able to name a pattern is a
#     much weaker claim than being able to fit it;
#   - on the current 248-item scope it blocks nothing that is already fitted
#     (the fitted Lumpy items carry 26-72 non-zero months), so it is not
#     silently overturning any decision the pipeline has already made.
#
# It has NOT been validated. MIN_TRAIN_MONTHS was in exactly this position until
# rolling-origin CV inside the training window settled the values around it
# (see the yearly_seasonality and CHANGEPOINT_PRIOR_KNOWN_CHANGEOVER notes
# above, and analysis/seasonality_experiment.py for the method). The same
# treatment is owed here once a Croston/TSB implementation exists to measure:
# sweep the value, score each candidate under rolling-origin CV on the
# intermittent segment, and replace this number with the winner and its margin.
# Until then, treat any routing decision that turns on it as provisional.
#
# That measurement will now never happen in the form described above, and the
# reason is not that it was skipped: analysis/croston_experiment.py ran Part A
# first — WHICH Croston-family estimator is being thresholded — and none of them
# earned an implementation. model_routing.py's gate 7 routes Lumpy/Intermittent
# items to a naive trailing average instead, 3 or 12 months depending on trend.
#
# So this gate is now guarding a different thing than the reasoning above chose
# it for. A trailing average has no interval estimate to initialise and needs no
# observed gaps — it is arithmetic, defined from a single sale — so the first
# bullet does not transfer to it, and neither does the second (the "much weaker
# claim" argument was about naming a pattern versus FITTING it, and a mean is
# barely a fit). Whether an occurrence floor belongs on that route at all, and
# whether 10 is it, is genuinely open.
#
# Left at 10 on purpose. Lowering or removing it would start forecasting items
# that get nothing today — a scope change, not a threshold tweak — and it is not
# what the routing change was reasoned about or evidenced. It stays as the
# conservative option until someone decides that question on its own terms.
#
# Used by preprocessing/model_routing.py. Nothing else reads it.
CROSTON_MIN_OCCURRENCES = 10

# ── Naive trailing-average route ──────────────────────────────────────────────
# The two windows behind model_routing.NAIVE_SHORT / NAIVE_LONG. Keyed by the
# route label itself so a route string is the only thing a caller needs to carry
# — models/naive_model.py looks the window up, it does not re-derive which is
# which. Both come from analysis/croston_experiment.py Part A'; nothing here
# re-decides them.
NAIVE_WINDOW_MONTHS = {"Naive-3mo": 3, "Naive-12mo": 12}

NAIVE_BAND_RATIOS = {"Naive-3mo": (0.13, 2.60), "Naive-12mo": (0.30, 3.48)}
# Empirical, not modelled: (p10, p90) of actual_monthly_rate / forecast_level
# across analysis/croston_experiment.py's rolling-origin CV folds, restricted
# to exactly the items each route sends there in model_routing.csv (74 usable
# folds / 18 items for Naive-3mo, 53 usable folds / 12 items for Naive-12mo;
# ~35-38% of folds excluded because the naive forecast itself was zero that
# month, so no ratio is definable — that's the method's real behaviour on
# this population, not a data gap to paper over). p10/p90 chosen over
# p25/p75 (too tight for how volatile this population actually is) and
# p5/p95 (resting on ~3-4 folds at that tail — not enough to trust).
# Naive-12mo's band is the thinner of the two: only 12 of its 22 routed
# items had standalone fold data at all (the other 10 are pooled families
# the experiment never tested) — treat this band as the weaker-evidence one
# of the pair.

# ── POC scope ─────────────────────────────────────────────────────────────────
# A product counts as "active" if it appears in the BDM forecast sheet AND has
# training activity at or after this month. Bound into queries.ACTIVE_PRODUCTS
# as :active_since. NOT rolled automatically by --auto-window: bump it by hand
# (e.g. keep it ~14 months before TEST_END) or items that stopped selling long
# ago stay in scope and get meaningless forecasts.
ACTIVE_SINCE = "2025-06"

# Rating that the --pilot flag restricts to (A-rated items only).
PILOT_RATING = "A"

# ── Amazon exclusion ──────────────────────────────────────────────────────────
# Channel values that identify Amazon replenishment (sell-in) rows in
# forecast_training_data. These are bulk orders from Daylight to Amazon
# fulfilment centres — not consumer demand — and produce lumpy, irregular
# time series that corrupt Prophet's seasonality learning.
# Filtering on channel is preferred over bdm_code because it is self-maintaining:
# Amazon BDMs carry real codes (AKB, AKB1, AKB2, AMUK) with no single shared
# code, but all carry channel = 'Amazon' from the Territory table.
# Phase 2 will replace this signal with Vendor Central sell-out data.
AMAZON_CHANNELS: list[str] = ["Amazon"]

# ── Channel-mismatch exclusion ────────────────────────────────────────────────
# Some items' real demand has migrated almost entirely into the channel this
# pipeline excludes from training (Amazon — see AMAZON_CHANNELS above). For
# those items, forecasting the non-Amazon remainder is not a tuning problem,
# it is a wrong-question problem: there is too little training signal left in
# the channel we fit on, even though the item's total demand is real and
# ongoing. Discovered from U35108 (2026-08): 99.6% Amazon share, 11 non-Amazon
# units/year against 2,971 total — Prophet was fitting a trend against the 11
# remaining units while predicting off a 2018-2023 baseline from before the
# channel shift.
#
# An item is excluded if, in the trailing 12 months ending at TEST_END, ALL
# THREE hold:
#   - Amazon share of total volume >= CHANNEL_MISMATCH_MIN_AMAZON_SHARE
#   - non-Amazon volume            <  CHANNEL_MISMATCH_MAX_NON_AMAZON_UNITS
#   - total (all-channel) volume   >= CHANNEL_MISMATCH_MIN_TOTAL_UNITS
# The third condition is deliberate: it is what keeps this from catching
# genuinely declining low-volume products, which should still get a low
# forecast, not be suppressed. See preprocessing.scope.detect_channel_mismatch().
#
# No forecast is produced for excluded items until Amazon-inclusive
# forecasting exists for them — see output/excluded_channel_mismatch.csv for
# the audit trail each run.
CHANNEL_MISMATCH_MIN_AMAZON_SHARE     = 0.90
CHANNEL_MISMATCH_MAX_NON_AMAZON_UNITS = 60
CHANNEL_MISMATCH_MIN_TOTAL_UNITS      = 300

# ── BDM benchmark ─────────────────────────────────────────────────────────────
# Overlap between the held-out test window and the BDM sheet's own coverage.
# Static default for a run without --auto-window; --auto-window recomputes
# both from the pulled BDM data itself (see main.apply_auto_window() /
# evaluation.benchmark.bdm_coverage_months()).
#
# BDM_FORECAST_YEAR is gone (removed 2026-08): the BDM sheet now holds more
# than one planning year at once (2026 and 2027, as of 2026-08), and
# queries.BDM_FORECASTS / loader.load_bdm_forecasts() pull every year present
# rather than one filtered year, so there is no single "the" year left to name
# here. evaluation.benchmark._bdm_month_col() keys its join off each BDM row's
# own forecast_year column instead of this constant.
BENCHMARK_START = "2026-03"
BENCHMARK_END   = "2026-08"

# ── Product Family tagging ────────────────────────────────────────────────────
# Family is joined onto item-level output for reporting (not a modelling input).
# Source: [dbo].[MASTER RODUCT TABLE].[PRODUCT FAMILY], keyed on [NAME] = item_code.
# [NAME] is 99.9% unique (1 dup, SN1200B, already excluded by the pipeline's
# item-code prefix gate), so the join cannot fan out item rows.
# Item codes with no master-product row (a known, accepted ~34% coverage gap,
# overwhelmingly AU A-prefix SKUs) are tagged with FAMILY_UNKNOWN rather than
# dropped or guessed, so the gap stays visible in every output artifact.
FAMILY_UNKNOWN = "Unknown"

# FORECAST_OUTPUT_DIR (env) lets a scheduled run write each cycle's CSVs to its
# own folder (run_forecast_cycle.sh does this); unset = ./output as before.
OUTPUT_DIR = Path(os.getenv("FORECAST_OUTPUT_DIR") or (Path(__file__).parent / "output"))
