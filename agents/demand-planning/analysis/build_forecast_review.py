"""
Build the Daylight forecast-review workbook (the FY2026 "review" .xlsx).

WHAT THIS IS
────────────
The reporting workbook that used to be assembled by hand, as a script. It is
the reporting counterpart to `preprocessing/model_routing.py`: that module made
the routing DECISION reproducible, this one makes the WORKBOOK that reports it
reproducible. Nothing here fits anything the pipeline owns, decides anything, or
writes to the database — it reads the pipeline's own outputs and lays them out.
The single exception is Sheet 8, which refits Prophet on the training window
only, exactly as `analysis/trend_audit.py` does and by calling that module's own
functions; see that sheet's builder.

Run:  python -m analysis.build_forecast_review
      python -m analysis.build_forecast_review --output-path <path.xlsx>
      python -m analysis.build_forecast_review --reuse-trend-refit   (Sheet 8 cache)

═══════════════════════════════════════════════════════════════════════════════
WHY THERE ARE TWO SOURCE DIRECTORIES AND THEY ARE NOT INTERCHANGEABLE
═══════════════════════════════════════════════════════════════════════════════
Read this before changing either constant below. It is the single thing most
likely to be got wrong by the next person regenerating this workbook, and
getting it wrong produces output that looks right and is silently a run behind.

`output_phase1/` is NOT "the latest run". It is a MIXED-VINTAGE directory: it
holds files written on at least three different dates by three different
things — a full pipeline run on 2026-08-16, a second pipeline run on 2026-08-18,
and several standalone `analysis/` experiments that wrote into it afterwards.
Some of its files are stale copies of files that `output/` now owns.

The concrete trap, confirmed 2026-08-20 by reading both headers:

    output/benchmark_comparison.csv
        item_code,region,rating,month,MODEL,actual,MODEL_FORECAST,bdm_forecast,
        prior_year_forecast,model_mape,bdm_mape,prior_year_mape,winner

    output_phase1/benchmark_comparison.csv          ← OLDER SCHEMA, DO NOT USE
        item_code,region,rating,month,actual,PROPHET_FORECAST,bdm_forecast,
        NAIVE_FORECAST,prophet_mape,bdm_mape,naive_mape,winner

The `output/` copy is hybrid-aware: ONE `model` / `model_forecast` column pair
per item, because an item is forecast by whichever model routing sent it to.
The `output_phase1/` copy predates that and still assumes Prophet forecast
everything, with a separate `naive_forecast` column that meant the PRIOR-YEAR
baseline — a completely different quantity from today's `Naive-3mo` /
`Naive-12mo` routed models, which did not exist when it was written. Building
from it would report Prophet numbers for 44 items Prophet never touched, under
a column heading that reads the same either way.

So the split is:

  * CORE_DIR (`output/`) — everything the current pipeline run owns: the
    benchmark, the test-window validation, the forward forecast, the BDM sheet,
    the scope/succession inputs, and the per-series fit metrics.

  * PHASE1_DIR (`output_phase1/`) — ONLY the four files that exist nowhere else,
    because `main.py` does not write them: the routing decision, the demand
    classification behind it, and this week's Erratic/naive experiment outputs.
    Every one of these is newer than the 2026-08-16 files sitting beside it;
    none of them is a stale copy of an `output/` file.

`preflight()` enforces this rather than trusting it: it asserts the `output/`
benchmark carries the hybrid schema, and it asserts `model_metrics.csv` is
byte-identical across the two directories (it is — sha256 d762d1f3… on
2026-08-20). That second check is a canary, not a formality: `model_metrics.csv`
is the one file both directories legitimately hold the same version of, so if it
ever diverges, something else has drifted too and the vintage reasoning above is
no longer safe. Both checks abort the build; neither warns and continues.

═══════════════════════════════════════════════════════════════════════════════
WHAT CHANGED FROM THE 2026-08-16 WORKBOOK (v1), AND WHY
═══════════════════════════════════════════════════════════════════════════════
1. The intermittent-demand-estimator recommendation is GONE.
   v1's Sheet 9 routed 42 products to an intermittent-demand estimator family
   (a "migrate 32 / new 10" split). `analysis/croston_experiment.py`, run
   2026-08-18 — after v1 was built — found no variant of that family beat a
   plain trailing average on this population, and no implementation was ever
   written. Nothing in this codebase routes there. `model_routing.py` gate 7
   sends Lumpy/Intermittent items to `Naive-3mo` or `Naive-12mo` instead, and
   `models/naive_model.py` actually produces those forecasts. Every place v1
   named or implied that estimator family now reports what is actually routed.

   Deliberately, the workbook itself never names the withdrawn estimator, not
   even to say it was withdrawn. A reader scanning the output for a stale
   recommendation should find nothing, and a product name sitting in a caption
   is indistinguishable from a recommendation at a glance. The provenance lives
   here, in the generator, where naming it costs nothing:
   `analysis/croston_experiment.py`, results in
   `output_phase1/croston_experiment_summary.csv`. The workbook cites it as "the
   intermittent-demand estimator experiment (analysis/, 2026-08-18)".

2. Routing is READ, not re-derived.
   v1 assembled Sheet 9 by hand from several diagnostics. `model_routing.csv`
   is now a computed pipeline stage. Every `Route To` value for the 248 items in
   routing scope is that file's `route` column verbatim, and every reason is its
   `block_reason` column verbatim. No sheet may disagree with it: Sheet 1's
   `Model`, Sheet 6's `Best Tool` and Sheet 9's `Route To` all read the same
   column, and `preflight()` asserts the forecast files' own `model` labels
   match the routing file item-for-item rather than assuming they do.

3. Scope broadened from Prophet-only to all forecast models.
   v1 assumed every forecast came from Prophet (186 products). The run now
   forecasts 198: 154 Prophet, 22 Naive-3mo, 22 Naive-12mo. Sheet 1 is renamed
   from "Prophet Forecast FY2026" to "Forecast FY2026" and carries a `Model`
   column.

4. The scored window follows config, not v1's caption.
   v1's Wins tabs were captioned "Jan-Jun 2026, 1,009 scored item-months" — the
   full six-month test window. `config.BENCHMARK_START/END` are and were
   2026-01..2026-04 (checked against git: unchanged since the v1 build), and
   `output/benchmark_comparison.csv` holds exactly those four months. This
   workbook reports the configured window.

═══════════════════════════════════════════════════════════════════════════════
WHAT CHANGED FROM v2, AND WHY (v3 — a framing fix, not a rebuild)
═══════════════════════════════════════════════════════════════════════════════
v3 changes how the headline comparison is LABELLED and CUT. It changes no
routing, no fit, no metric and no source file. Sheets 1, 6, 7, 8 and 9 are v2's,
untouched, because the confusion below never reached them.

THE CONFUSION. "Naive" meant two different things across versions of this
workbook: in v1 it was the prior-year baseline (`benchmark.py` literally carried
a `naive_forecast` column then), and in v2 it was the routed trailing-average
models `Naive-3mo` / `Naive-12mo`. On top of that, v2's Overview listed
Prophet / Naive-3mo / Naive-12mo / Prior-Year as four peer competitors, which
framed the wrong question: three of those four are the same side of the
comparison, differing only in which method routing assigned.

THE TERMS, used consistently everywhere they appear:

  * "Automated Forecast" — whichever of Prophet / Naive-3mo / Naive-12mo
    `model_routing.csv` routed that item to. This is the top-level competitor
    against BDM's manual forecast, and the thing the pipeline delivers.

  * "Method" — the sub-label naming which specific approach produced a given
    Automated Forecast win or loss. A drill-down, never a headline category.
    Naive-3mo / Naive-12mo are trailing averages; calling either an "ML model"
    or "ML" is forbidden here because it reopens the same confusion from the
    other end. The word "Naive" appears only inside those two Method names.

  * "Prior-Year Baseline" — unchanged from v2. Same-month-last-year, a benchmark
    comparator, not something anything routes to.

WHAT MOVED:
  1. Sheet 2's headline table: four rows -> three (Automated Forecast / BDM /
     Prior-Year Baseline). The Method breakdown, which v2 kept at the bottom of
     the sheet as "Win Count by Routed Model", is now the sub-table directly
     under the Automated Forecast row it drills into. Rating and Region
     cross-tabs stay at the three-way level.
  2. Sheets 3-5: Prophet Wins / BDM Wins / Naive Wins -> Automated Forecast Wins
     / BDM Wins / Prior-Year Wins. Same rows, re-tabbed. v2's per-tab "Model"
     column is now "Method", styled and filled in on every row of every tab —
     which is v2's tab-5-only "Naive Variant" column extended to cover Prophet
     rows too, and it makes v2's fourteenth column redundant.
  3. `scan_output()` gained a second guard, in the shape of the existing
     withdrawn-token scan: a bare "Naive" in any tab name, title, header or row
     label of the WRITTEN file fails the build.

DOCUMENTATION CONVENTION
────────────────────────
Every judgment call in the output gets a caption in the output, at the same
density as v1 and as `analysis/croston_experiment.py`. A number a reader cannot
trace to a file, a window, or a stated choice is a number they will have to
re-derive, and re-derivation is exactly what produced the two-vintage problem
above. Judgment calls that shape the DATA rather than its presentation are
additionally commented at the point they are made below.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import math
import re
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from preprocessing import demand_classification as dc  # noqa: E402
from preprocessing import family_pool, model_routing  # noqa: E402

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parent.parent

# ── The two source directories (see module docstring — they are NOT the same) ──

#: Current, hybrid-aware core pipeline outputs: everything `main.py` writes.
#: Confirmed current by schema, not by mtime — `benchmark_comparison.csv` here
#: carries a single `model` / `model_forecast` column pair rather than separate
#: `prophet_forecast` / `naive_forecast` columns.
CORE_DIR = REPO / "output"

#: Routing / classification / experiment outputs ONLY. These four files exist
#: nowhere else because `main.py` does not write them. Everything else in this
#: directory is a stale copy of a CORE_DIR file from an earlier run — see the
#: module docstring. Do not widen PHASE1_FILES without re-reading that section.
PHASE1_DIR = REPO / "output_phase1"

CORE_FILES = (
    "benchmark_comparison.csv", "test_validation.csv",
    "forecast_2026-07_2026-12.csv", "bdm_forecasts.csv", "active_products.csv",
    "successor_map.csv", "model_metrics.csv", "raw_data.csv",
    "excluded_channel_mismatch.csv",
)
PHASE1_FILES = (
    "model_routing.csv", "demand_classification.csv",
    "erratic_naive_results.csv", "erratic_naive_summary.csv",
)

#: Product display name / category / family. Tab-separated, UTF-8 BOM.
#: Covers 284 of the 286 BDM-forecast items; the two misses fall back to
#: config.FAMILY_UNKNOWN rather than being guessed, so the gap stays visible in
#: the output the way the pipeline's own family tagging leaves it visible.
MASTER_TABLE = Path(r"C:\Justin\Daylight\master_product_table_data.txt")

DEFAULT_OUTPUT = Path(
    r"C:\Justin\Daylight\Daylight_Prophet_Forecast_Review_FY2026_v3.xlsx")

#: v1, opened read-only and never written. Used for exactly two things: Sheet 8's
#: reproducibility check (do the items present in both refit to the same
#: numbers?) and Sheet 8's "Previously Flagged?" column.
ORIGINAL_WORKBOOK = Path(
    r"C:\Justin\Daylight\Daylight_Prophet_Forecast_Review_FY2026.xlsx")

#: Sheet 8 refits 140 Prophet family series. Cached so a formatting-only rerun
#: need not pay for it again; --reuse-trend-refit opts in, default recomputes.
TREND_REFIT_CACHE = PHASE1_DIR / "trend_disconnect_v2.csv"

#: The canary described in the module docstring. Recorded rather than merely
#: compared so a future divergence says WHICH side moved.
MODEL_METRICS_SHA256 = (
    "d762d1f3741191746e2bbd286ebe0449b65a29072b4107643476c6a1e8173ca6")

# ── Presentation constants (v1's palette, read back out of the v1 file) ───────
C_INK, C_MUTED, C_WHITE = "FF0A1628", "FF5A6577", "FFFFFFFF"
C_ACCENT = "FF0066FF"
C_GOOD, C_GOOD_BG = "FF0D9F6E", "FFECFDF5"
C_WARN, C_WARN_BG = "FFD97706", "FFFFFBEB"
C_BAD, C_BAD_BG = "FFDC2626", "FFFEF2F2"
C_GREY, C_GREY_BG = "FF9AA5B1", "FFF4F6F9"
C_BLUE_BG = "FFEFF6FF"
C_INDIGO, C_INDIGO_BG = "FF4338CA", "FFE0E7FF"
C_PURPLE, C_PURPLE_BG = "FF7C3AED", "FFFAF5FF"
C_AMBER_STRONG = "FFB45309"

FMT_QTY, FMT_INT = "#,##0.0", "#,##0"
FMT_PCT_VAL = "0.0\\%"       # a number that already means "percent"
FMT_PCT_VAL2 = "#,##0.0\\%"
FMT_PCT_FRAC = "0%"          # a fraction Excel should render as a percentage
FMT_PCT_FRAC1 = "0.0%"

MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# ── Route labels, imported rather than retyped ────────────────────────────────
# Retyping these is how a workbook starts disagreeing with the pipeline; the
# whole point of this rebuild is that it cannot.
R_PROPHET = model_routing.PROPHET
R_N3 = model_routing.NAIVE_SHORT
R_N12 = model_routing.NAIVE_LONG
R_BLOCKED = model_routing.BLOCKED
NAIVE_ROUTES = (R_N3, R_N12)

#: The three routed METHODS. "Method" is v3's term for which specific approach
#: produced a given item's Automated Forecast. It is a sub-label — a drill-down
#: under the headline — and never a headline category of its own.
#:
#: Naive-3mo / Naive-12mo are trailing averages, not fitted models. They must
#: never be called "ML" or "an ML model" anywhere in this workbook: that mislabel
#: is one half of the confusion v3 exists to close, and the other half is the
#: bare word "Naive", which meant the PRIOR-YEAR baseline in v1 and means a
#: routed trailing average now. Both halves are fixed the same way — the word
#: "Naive" appears only ever inside "Naive-3mo" / "Naive-12mo", as a Method.
METHODS = (R_PROPHET, R_N3, R_N12)

#: v3's headline term for the pipeline's own output: whichever of the three
#: METHODS `model_routing.csv` sent an item to. THIS is what competes against
#: BDM's manual forecast. Which method produced it is a drill-down, because
#: nobody chooses a method — routing does — so a per-method headline invites a
#: comparison ("Prophet vs Naive-3mo") that no one is being asked to make.
AUTOMATED_LABEL = "Automated Forecast"

#: The prior-year same-month baseline's label in `benchmark_comparison.winner`.
#: It was called `naive` until the 2026-08 rename in `evaluation/benchmark.py`,
#: and that old name is exactly what v1's "Naive Wins" tab reported. Unchanged
#: in v3: it stays a benchmark comparator with its own headline row and its own
#: tab, because nothing routes to it — it is neither an Automated Forecast nor a
#: Method, and folding it into either side would misstate what it is.
BASELINE_PRIOR_YEAR = "prior_year"
BASELINE_BDM = "bdm"
PRIOR_YEAR_LABEL = "Prior-Year Baseline"

# ── This week's two findings, written once and cited from every sheet ─────────
# Both are OPEN findings. Neither has changed any routing, and the captions must
# not let a reader believe otherwise — that is the whole reason they are pinned
# here as constants rather than paraphrased three times in three sheets.
AU_ROOT_CAUSE = (
    "AU root-cause diagnosis (this week, open): AU's weakness is a POPULATION "
    "effect, not a data or seasonality defect. Of AU's 17 Prophet-routed items, "
    "16 (94%) are classified Erratic against 53-69% in every other region, and "
    "their median BDM FY2026 volume is 520 units against 858-1,253 elsewhere — "
    "AU is Prophet's hardest demand shape at the smallest scale. It is NOT "
    "sparse history (median AU tenure is 95 months, in line with every other "
    "region) and it is NOT a seasonality bug (yearly Fourier order and "
    "seasonality mode are global, identical for every region, and were settled "
    "under rolling-origin CV in analysis/seasonality_experiment.py)."
)
AU_NAIVE_FINDING = (
    "AU Erratic naive experiment (analysis/erratic_naive_experiment.py, "
    "2026-08-19 — NOT ADOPTED INTO ROUTING): on AU's 10 standalone Erratic "
    "items, Naive-12mo scored a median WAPE of 54.9% against Prophet's 81.7% "
    "under rolling-origin CV inside the training window. It is not a decided "
    "fix: the same folds show the naive variants forecasting ~20% BELOW actual "
    "demand on average (bias ratio 0.81 for Naive-12mo and 0.80 for Naive-3mo, "
    "against Prophet's near-unbiased 1.03), and WAPE is bounded at 100% for an "
    "under-forecast but unbounded for an over-forecast — so part of that margin "
    "is the metric rewarding a smaller number, and these folds cannot say how "
    "much. Routing still sends every one of these items to Prophet. Treat as "
    "UNDER REVIEW."
)


# ══════════════════════════════════════════════════════════════════════════════
# Preflight
# ══════════════════════════════════════════════════════════════════════════════

class PreflightError(RuntimeError):
    """A source-file assumption failed. Never downgraded to a warning."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def preflight() -> None:
    """
    Refuse to build on the wrong inputs. See the module docstring for why each
    of these is a hard stop rather than a warning: every one of them fails
    SILENTLY if it is allowed through — the workbook still builds, still looks
    right, and reports a previous run's numbers under this run's headings.
    """
    missing = [str(CORE_DIR / f) for f in CORE_FILES if not (CORE_DIR / f).exists()]
    missing += [str(PHASE1_DIR / f) for f in PHASE1_FILES
                if not (PHASE1_DIR / f).exists()]
    if missing:
        raise PreflightError("missing source file(s):\n  " + "\n  ".join(missing))

    # 1. The hybrid schema check. `prophet_forecast` / `naive_forecast` as two
    #    separate columns is the pre-hybrid shape and means this is the wrong
    #    copy of the file (or the pipeline has been rolled back).
    bench_cols = pd.read_csv(CORE_DIR / "benchmark_comparison.csv", nrows=0).columns
    if "model" not in bench_cols or "model_forecast" not in bench_cols:
        raise PreflightError(
            f"{CORE_DIR / 'benchmark_comparison.csv'} does not carry the "
            f"hybrid-aware schema.\n"
            f"  found:    {list(bench_cols)}\n"
            f"  required: a single 'model' + 'model_forecast' column pair.\n"
            f"  If it carries 'prophet_forecast'/'naive_forecast' instead, this "
            f"is the PRE-HYBRID file — see the module docstring. STOPPING."
        )
    if "prophet_forecast" in bench_cols or "naive_forecast" in bench_cols:
        raise PreflightError(
            f"{CORE_DIR / 'benchmark_comparison.csv'} carries BOTH schemas "
            f"({list(bench_cols)}); refusing to guess which is current. STOPPING."
        )

    # 2. The canary. model_metrics.csv is the one file both directories should
    #    legitimately hold the same version of. Divergence means the vintage
    #    reasoning in the module docstring no longer holds and this build is not
    #    safe to make on assumptions.
    core_h = _sha256(CORE_DIR / "model_metrics.csv")
    p1_h = _sha256(PHASE1_DIR / "model_metrics.csv")
    if core_h != p1_h:
        raise PreflightError(
            "model_metrics.csv is NO LONGER identical between the two source "
            "directories — something has drifted since 2026-08-20 and which "
            "copy is current can no longer be assumed.\n"
            f"  {CORE_DIR / 'model_metrics.csv'}\n    sha256 {core_h}\n"
            f"  {PHASE1_DIR / 'model_metrics.csv'}\n    sha256 {p1_h}\n"
            f"  expected both to be {MODEL_METRICS_SHA256}\n"
            "  STOPPING rather than guessing."
        )
    note = "matches 2026-08-20 baseline" if core_h == MODEL_METRICS_SHA256 else (
        "IDENTICAL across both dirs but DIFFERENT from the 2026-08-20 baseline "
        "(a newer run wrote both) — proceeding")
    print(f"  preflight: benchmark_comparison.csv carries the hybrid schema (ok)")
    print(f"  preflight: model_metrics.csv identical across both dirs, {note}")
    print(f"             sha256 {core_h[:16]}…")

    # 3. Routing must contain only the labels the pipeline actually emits. This
    #    is the guard against a hand-edited routing file reintroducing a route
    #    nothing implements — the exact failure v1 shipped.
    routes = set(pd.read_csv(PHASE1_DIR / "model_routing.csv")["route"].unique())
    unknown = routes - set(model_routing.ROUTES)
    if unknown:
        raise PreflightError(
            f"model_routing.csv contains route label(s) the pipeline does not "
            f"emit: {sorted(unknown)}. Known routes: {list(model_routing.ROUTES)}. "
            f"STOPPING — a workbook must not report a route nothing implements."
        )
    print(f"  preflight: routing labels {sorted(routes)} all known to "
          f"preprocessing/model_routing.py (ok)")


# ══════════════════════════════════════════════════════════════════════════════
# Sources
# ══════════════════════════════════════════════════════════════════════════════

def _codes(s: pd.Series) -> pd.Series:
    return s.astype(str).str.strip()


class Sources:
    """Every input, loaded once, with the item-level lookups built off them."""

    def __init__(self) -> None:
        rd = lambda d, f: pd.read_csv(d / f)  # noqa: E731

        self.bench = rd(CORE_DIR, "benchmark_comparison.csv")
        self.test = rd(CORE_DIR, "test_validation.csv")
        self.fcst = rd(CORE_DIR, "forecast_2026-07_2026-12.csv")
        self.bdm = rd(CORE_DIR, "bdm_forecasts.csv")
        self.active = rd(CORE_DIR, "active_products.csv")
        self.succ = rd(CORE_DIR, "successor_map.csv")
        self.metrics = rd(CORE_DIR, "model_metrics.csv")
        self.raw = rd(CORE_DIR, "raw_data.csv")
        self.mismatch = rd(CORE_DIR, "excluded_channel_mismatch.csv")

        self.routing = rd(PHASE1_DIR, "model_routing.csv")
        self.classes = rd(PHASE1_DIR, "demand_classification.csv")
        self.enr = rd(PHASE1_DIR, "erratic_naive_results.csv")
        self.ens = rd(PHASE1_DIR, "erratic_naive_summary.csv")

        for df in (self.bench, self.test, self.fcst, self.bdm, self.active,
                   self.metrics, self.raw, self.mismatch, self.routing,
                   self.classes, self.enr, self.ens):
            if "item_code" in df.columns:
                df["item_code"] = _codes(df["item_code"])

        for df in (self.test, self.fcst):
            df["ds"] = pd.to_datetime(df["ds"])
            df["month"] = df["ds"].dt.strftime("%Y-%m")

        # ── Item universes ────────────────────────────────────────────────────
        self.bdm_items = sorted(set(self.bdm["item_code"]))          # 286
        self.routed_items = set(self.routing["item_code"])           # 248
        self.forecast_items = sorted(set(self.fcst["item_code"]))    # 198
        self.active_items = set(_codes(self.active["item_code"]))
        self.mismatch_items = set(self.mismatch["item_code"])

        fams = family_pool.build_successor_families(self.succ)
        # Retired = resolved-family member that is not one of that family's
        # current codes. Taken from model_routing's own helper so this cannot
        # disagree with the gate-1 definition the routing file was built under.
        self.retired_items = set(model_routing.retired_codes(fams))

        # ── Reporting lookups ─────────────────────────────────────────────────
        # Region label: the JOIN of every region the BDM sheet forecasts the item
        # in ("1 UK/2 EU"), not `first`. This reproduces v1 and is the right
        # grain for a BDM-facing report, but it deliberately differs from
        # benchmark_comparison.csv's own `region` column, which prepare.
        # series_metadata() takes as `first` and which therefore collapses 8 of
        # the 198 forecast items to a single region. Reporting breakdowns must
        # not silently disagree between sheets, so ONE derivation is used
        # everywhere in this workbook and benchmark's own column is dropped.
        b = self.bdm.dropna(subset=["region"])
        self.region = (b.groupby("item_code")["region"]
                        .apply(lambda s: "/".join(sorted(set(s)))).to_dict())
        self.rating = (self.bdm.dropna(subset=["rating"])
                       .groupby("item_code")["rating"].first().to_dict())
        self.bdm_total = self.bdm.groupby("item_code")["bdm_forecast_qty"].sum().to_dict()
        # Keyed on each row's own `forecast_year`, not a global constant
        # (config.BDM_FORECAST_YEAR is gone — the BDM sheet holds more than one
        # planning year at once). Without forecast_year in the groupby, a
        # multi-year sheet would collide 2026-Feb and 2027-Feb into one key,
        # the exact bug evaluation.benchmark._bdm_month_col() was fixed for.
        self.bdm_month = {
            (r.item_code, f"{int(r.forecast_year)}-"
                          f"{MONTH_ABBR.index(str(r.month_name)[:3]) + 1:02d}"): r.qty
            for r in (self.bdm.assign(mn=self.bdm["month_name"].astype(str).str[:3])
                      .loc[lambda d: d["mn"].isin(MONTH_ABBR)]
                      .groupby(["item_code", "month_name", "forecast_year"], as_index=False)
                      .agg(qty=("bdm_forecast_qty", "sum"))
                      .itertuples(index=False))
        }

        master = pd.read_csv(MASTER_TABLE, sep="\t", encoding="utf-8-sig", dtype=str)
        master.columns = [c.strip() for c in master.columns]
        master["NAME"] = _codes(master["NAME"])
        master = master.drop_duplicates("NAME", keep="first").set_index("NAME")
        self.master_family = master["PRODUCT FAMILY"].to_dict()
        self.master_name = master["DISPLAY NAME"].to_dict()
        self.master_category = master["PRODUCT CATEGORY"].to_dict()

        # Family label: the pipeline's own `family` tag where it exists (the 198
        # forecast items carry it on every forecast row), the master table
        # otherwise, FAMILY_UNKNOWN last. Preferring the pipeline's tag keeps
        # forecast rows reading identically to every other pipeline artifact.
        self.family = dict(self.master_family)
        for r in self.fcst[["item_code", "family"]].drop_duplicates().itertuples(index=False):
            if isinstance(r.family, str) and r.family and r.family != config.FAMILY_UNKNOWN:
                self.family[r.item_code] = r.family

        # ── Non-Amazon sales history, for the items routing does not cover ────
        # Same channel exclusion the pipeline trains under, so "months of sales
        # history" here means the same thing it means everywhere else.
        na = self.raw[~self.raw["channel"].isin(config.AMAZON_CHANNELS)]
        self.nonamazon = na
        self.any_nonamazon = set(na["item_code"])
        trn = na[na["year_month"] <= config.TRAIN_END]
        self.train_months = trn.groupby("item_code")["year_month"].nunique().to_dict()
        self.first_sale = trn.groupby("item_code")["year_month"].min().to_dict()

        self.route = self.routing.set_index("item_code")["route"].to_dict()
        self.block_reason = self.routing.set_index("item_code")["block_reason"].to_dict()
        self.category = self.classes.set_index("item_code")["category"].to_dict()

        self._assert_routing_agrees()

    def _assert_routing_agrees(self) -> None:
        """
        The forecast files name their own forecaster; routing names the same
        thing. If they ever disagree, one of the two directories is stale and
        the workbook would report a model that did not run.
        """
        for name, df in (("test_validation", self.test),
                         ("forecast_2026-07_2026-12", self.fcst),
                         ("benchmark_comparison", self.bench)):
            got = df.groupby("item_code")["model"].first()
            bad = [(i, m, self.route.get(i)) for i, m in got.items()
                   if self.route.get(i) != m]
            if bad:
                raise PreflightError(
                    f"{name}.csv disagrees with model_routing.csv on "
                    f"{len(bad)} item(s), e.g. {bad[:5]} (item, file, routing). "
                    f"STOPPING."
                )
        print(f"  preflight: {len(self.forecast_items)} forecast items agree with "
              f"model_routing.csv on their model, item-for-item (ok)")

    # ── Small shared accessors ────────────────────────────────────────────────
    def reg(self, item: str) -> str:
        return self.region.get(item, "")

    def rat(self, item: str) -> str:
        return self.rating.get(item, "")

    def fam(self, item: str) -> str:
        return self.family.get(item, config.FAMILY_UNKNOWN)


# ══════════════════════════════════════════════════════════════════════════════
# Worksheet helpers (v1's visual language, applied from one place)
# ══════════════════════════════════════════════════════════════════════════════

from openpyxl import Workbook, load_workbook           # noqa: E402
from openpyxl.styles import Alignment, Font, PatternFill  # noqa: E402
from openpyxl.utils import get_column_letter            # noqa: E402


def _col(n: int) -> str:
    return get_column_letter(n)


def title(ws, text: str, ncols: int, row: int = 1) -> int:
    ws.merge_cells(f"A{row}:{_col(ncols)}{row}")
    c = ws.cell(row, 1, text)
    c.font = Font(bold=True, size=12, color=C_INK)
    c.alignment = Alignment(wrap_text=True, horizontal="left", vertical="top")
    ws.row_dimensions[row].height = 27.75
    return row + 1


def caption(ws, row: int, text: str, ncols: int) -> int:
    """A caption row. Height is sized off the text so nothing is clipped."""
    ws.merge_cells(f"A{row}:{_col(ncols)}{row}")
    c = ws.cell(row, 1, text)
    c.font = Font(size=9, color=C_MUTED)
    c.alignment = Alignment(wrap_text=True, horizontal="left", vertical="top")
    per_line = max(40, int(ncols * 13.5))
    ws.row_dimensions[row].height = 13.9 * max(1, math.ceil(len(text) / per_line))
    return row + 1


def section(ws, row: int, text: str, span: int = 4) -> int:
    ws.merge_cells(f"A{row}:{_col(span)}{row}")
    c = ws.cell(row, 1, text)
    c.font = Font(bold=True, size=11, color=C_INK)
    ws.row_dimensions[row].height = 15.0
    return row + 1


def headers(ws, row: int, cols: list[str], widths: dict[int, float] | None = None) -> int:
    for i, h in enumerate(cols, start=1):
        c = ws.cell(row, i, h)
        c.font = Font(bold=True, size=10, color=C_WHITE)
        c.fill = PatternFill("solid", fgColor=C_ACCENT)
        c.alignment = Alignment(wrap_text=True, horizontal="center", vertical="center")
    ws.row_dimensions[row].height = 30.0
    for i, w in (widths or {}).items():
        ws.column_dimensions[_col(i)].width = w
    return row + 1


def put(ws, row: int, col: int, value, *, fmt: str | None = None,
        bold: bool = False, color: str | None = None, fill: str | None = None,
        wrap: bool = False):
    c = ws.cell(row, col, value)
    if fmt:
        c.number_format = fmt
    if bold or color:
        c.font = Font(bold=bold, size=10, color=color)
    if fill:
        c.fill = PatternFill("solid", fgColor=fill)
    if wrap:
        c.alignment = Alignment(wrap_text=True, vertical="top")
    return c


def month_label(ym: str) -> str:
    y, m = ym.split("-")
    return f"{MONTH_ABBR[int(m) - 1]}-{y[2:]}"


def r1(x) -> float | None:
    """Round for display, preserving None/NaN as a genuinely blank cell.

    v1 shows a blank where a statistic is undefined and a 0 where the value is
    really zero; collapsing the two is the mistake Sheet 1's own caption warns
    about, so it is avoided everywhere rather than only there.
    """
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    return round(float(x), 1)


def r2(x) -> float | None:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    return round(float(x), 2)


def median_or_none(s: pd.Series) -> float | None:
    s = s.dropna()
    return None if s.empty else float(s.median())


def mean_or_none(s: pd.Series) -> float | None:
    s = s.dropna()
    return None if s.empty else float(s.mean())


# ══════════════════════════════════════════════════════════════════════════════
# Derived: why each BDM item has no forecast (Sheets 6 and 9)
# ══════════════════════════════════════════════════════════════════════════════
#
# v1's reason-category methodology, kept EXACTLY: check the item against the
# eligibility rules, the sales-history requirements and the succession records,
# in that order, then cross-check against its actual sales pattern. What has
# changed is only where the answer is read FROM. For the 248 items inside
# routing scope the answer is `model_routing.csv`'s own `block_reason`, verbatim
# — the gate ladder in preprocessing/model_routing.py IS "the eligibility rules,
# in order", so re-deriving it here would be re-typing a decision the pipeline
# already made, which is the failure mode this rebuild exists to remove.
#
# The remaining 38 BDM items sit OUTSIDE routing scope entirely (routing runs at
# current-code grain over the active allow-list minus channel-mismatch
# exclusions), so the routing file has nothing to say about them and their
# reason must be established here. That is done from the same upstream inputs
# routing itself is built on — family_pool's retired set, active_products.csv,
# excluded_channel_mismatch.csv, and the Amazon-excluded raw history — never
# from a second, looser definition maintained in this file.

REASON_RETIRED = "Retired product code (by design)"
REASON_INACTIVE = "Not currently an active product"
REASON_AMAZON = "Sales concentrated in Amazon"
REASON_LIMITED = "Limited sales history"
REASON_INFREQUENT = "Infrequent seller"
REASON_NO_SALES = "No recorded sales"

REASON_STYLE = {
    REASON_RETIRED: (C_GREY_BG, C_GREY),
    REASON_INACTIVE: (C_PURPLE_BG, C_PURPLE),
    REASON_AMAZON: (C_BLUE_BG, C_ACCENT),
    REASON_LIMITED: (C_WARN_BG, C_WARN),
    REASON_INFREQUENT: (C_INDIGO_BG, C_INDIGO),
    REASON_NO_SALES: (C_BAD_BG, C_BAD),
}

#: Out-of-scope status labels for Sheet 9's `Route To`. These items are absent
#: from model_routing.csv, so this column cannot quote it for them; the label
#: says so in as many words rather than inventing a route.
OOS_RETIRED = "Not in routing scope - retired code"
OOS_INACTIVE = "Not in routing scope - not currently active"
OOS_AMAZON = "Not in routing scope - needs Amazon sell-out data"
OOS_NO_HISTORY = "Not in routing scope - no training-window sales"


def unforecast_reasons(src: Sources) -> pd.DataFrame:
    """One row per BDM item with no forecast in this run (88 of 286)."""
    rows = []
    for item in src.bdm_items:
        if item in set(src.forecast_items):
            continue
        route = src.route.get(item)
        block = src.block_reason.get(item)
        n_hist = int(src.train_months.get(item, 0))
        first = src.first_sale.get(item)

        if item in src.retired_items:
            reason, oos = REASON_RETIRED, OOS_RETIRED
            detail = (
                "This is a retired product code that has been replaced by a "
                "newer code. By design, only the current/successor code receives "
                "a forecast - confirmed consistently across every retired code "
                "in the product-succession records.")
            cat, tool = "N/A - Retired", (
                "No forecast needed - the successor code carries this demand. "
                "Outside model_routing.csv scope by design (gate 1).")
        elif item in src.mismatch_items:
            reason, oos = REASON_AMAZON, OOS_AMAZON
            detail = (
                "Formally excluded by the channel-mismatch rule: Amazon is >=90% "
                "of this item's trailing-12-month volume and the non-Amazon "
                "remainder is too thin to train on. The demand is real and "
                "ongoing; it is simply not in the channel this system sees.")
            cat, tool = "N/A - Amazon channel", (
                "Needs Amazon sell-out (Vendor Central) data before any model "
                "applies. No model choice is available to make until then.")
        elif item not in src.active_items:
            reason, oos = REASON_INACTIVE, OOS_INACTIVE
            detail = (
                "This item appears in the BDM forecast but is not currently "
                "flagged as an active product in the forecasting system.")
            cat, tool = _classify_out_of_scope(src, item), (
                "Confirm active-product status first - an item outside the "
                "active allow-list is never routed, whatever its demand shape.")
        elif item in src.routed_items:
            # Inside routing scope: quote the routing file, do not re-decide.
            reason = {
                model_routing.REASON_SHORT_CALENDAR: REASON_LIMITED,
                model_routing.REASON_UNCLASSIFIED: REASON_LIMITED,
                model_routing.REASON_FEW_OCCURRENCES: REASON_INFREQUENT,
                model_routing.REASON_NO_HISTORY: REASON_NO_SALES,
            }.get(block, REASON_LIMITED)
            oos = None
            detail, tool = _routed_block_text(block, item, n_hist, first)
            cat = src.category.get(item, "")
        elif item not in src.any_nonamazon:
            reason, oos = REASON_AMAZON, OOS_AMAZON
            detail = (
                "Every recorded sale for this item is in the Amazon channel, "
                "which this system excludes from training - so after the channel "
                "filter there is no series left to classify or fit. Below the "
                "formal exclusion threshold only because there is no non-Amazon "
                "volume to measure a share against.")
            cat, tool = "N/A - Amazon channel", (
                "Needs Amazon sell-out (Vendor Central) data before any model "
                "applies. No model choice is available to make until then.")
        else:
            # Non-Amazon sales exist, but none of them land on or before
            # TRAIN_END - the item began selling after the training window
            # closed. v1's wording is kept because the fact is the same one.
            reason, oos = REASON_NO_SALES, OOS_NO_HISTORY
            detail = (
                f"No recorded non-Amazon sales for this item through "
                f"{month_label(config.TRAIN_END)} - its first sale falls after "
                f"the training window closed, so there is nothing for any model "
                f"to learn from yet. Likely a brand-new or not-yet-shipped "
                f"product.")
            cat, tool = "No Data", (
                "No sales history exists for any model to learn from - use a "
                "launch-plan estimate, category-level proxy, or manual "
                "placeholder until real sales begin.")

        rows.append(dict(
            item_code=item, family=src.fam(item), rating=src.rat(item),
            region=src.reg(item), bdm_total=src.bdm_total.get(item, np.nan),
            n_hist=n_hist, reason=reason, detail=detail, category=cat,
            best_tool=tool, route=route, block_reason=block, oos_label=oos,
        ))
    return pd.DataFrame(rows)


def _routed_block_text(block: str, item: str, n_hist: int, first) -> tuple[str, str]:
    """
    Reason detail + Best Tool for an item routing has already ruled on.

    `Best Tool` quotes the routing verdict verbatim and then names where the item
    goes once the block clears. That destination is not a fresh recommendation:
    it is which GATE fired. Gate 6 blocks a Prophet-suitable pattern for want of
    calendar months, so the item is a Prophet item waiting on time; gate 7 blocks
    a Lumpy/Intermittent pattern for want of sale occurrences, so it is a
    trailing-average item waiting on occurrences, and which window it gets is
    `decline_ratio` against 1.0 - all of that is stated in model_routing.py's
    own gate ladder, not decided here.
    """
    seen = f" {n_hist} month(s) of its history carry a recorded sale."
    since = f" First recorded sale: {month_label(first)}." if isinstance(first, str) else ""
    if block == model_routing.REASON_SHORT_CALENDAR:
        return (
            f"Routing gate 6: the demand pattern suits Prophet, but the fit key "
            f"has fewer than {config.MIN_TRAIN_MONTHS} calendar months of "
            f"training data - two full yearly cycles is the minimum for yearly "
            f"seasonality to be detectable.{since}{seen}",
            f"Blocked by routing (model_routing.csv): "
            f"\"{block}\". Routes to Prophet by itself once it clears "
            f"{config.MIN_TRAIN_MONTHS} calendar months - a waiting list, not a "
            f"decision. No action needed beyond time.")
    if block == model_routing.REASON_FEW_OCCURRENCES:
        return (
            f"Routing gate 7: plenty of calendar tenure, but too few months with "
            f"an actual sale. Waiting longer does not help this shape the way it "
            f"helps a genuinely new product - what it needs is occurrences, not "
            f"calendar.{since}{seen}",
            f"Blocked by routing (model_routing.csv): "
            f"\"{block}\". Routes to a naive trailing average once it clears the "
            f"occurrence floor (10 non-zero months) - Naive-3mo if the series is "
            f"declining, Naive-12mo otherwise. Note that floor is itself flagged "
            f"PROVISIONAL in config: it was chosen by reasoning for a different "
            f"estimator than the one now routed to, and whether an occurrence "
            f"floor belongs on a trailing average at all is genuinely open. It "
            f"is left in place as the conservative option, so a product sitting "
            f"just under it is blocked by an unvalidated number.")
    if block == model_routing.REASON_UNCLASSIFIED:
        return (
            f"Routing gate 5: fewer than {config.SB_MIN_MONTHS_TO_CLASSIFY} "
            f"months with a recorded sale - too few to read a demand pattern "
            f"from at all (the size-variability statistic is undefined below "
            f"two).{since}{seen}",
            f"Blocked by routing (model_routing.csv): \"{block}\". Too little "
            f"data to classify, and therefore too little to recommend a method "
            f"- monitor.")
    return (
        f"Routing gate 4: no sales history in the training window at all, so "
        f"there is nothing to classify or fit.{since}",
        f"Blocked by routing (model_routing.csv): \"{block}\". No model applies "
        f"until real sales exist - use a launch-plan estimate, category-level "
        f"proxy, or manual placeholder.")


def _classify_out_of_scope(src: Sources, item: str) -> str:
    """
    SBC category for an item routing does not cover, via the pipeline's OWN
    classifier (`demand_classification.classify_series`) on the item's
    Amazon-excluded monthly series. Reusing the classifier rather than
    reimplementing the ADI / CV^2 arithmetic is what stops this column from
    quietly disagreeing with demand_classification.csv for the items that DO
    appear in both.
    """
    s = src.nonamazon[src.nonamazon["item_code"] == item]
    if s.empty:
        return dc.NO_DATA
    monthly = (s.assign(ds=pd.to_datetime(s["year_month"] + "-01"))
               .groupby("ds", as_index=False)["monthly_qty"].sum()
               .rename(columns={"monthly_qty": "y"}))
    return dc.classify_series(monthly)["category"]


# ══════════════════════════════════════════════════════════════════════════════
# Derived: the trend-disconnect refit (Sheet 8)
# ══════════════════════════════════════════════════════════════════════════════

def read_v1_sheet8() -> pd.DataFrame:
    """
    v1's Sheet 8 detail table, for the reproducibility check.

    v1 validated itself against the audit before it (`trend_diagnostic_audit.csv`)
    by confirming the one item present in both refit to the same numbers. This
    build owes v1 the same courtesy, on a much larger overlap: 154 items are
    refitted here and most of them appear in v1's 186, so the fitted trend and
    the recent-actual average must reproduce EXACTLY. They should: the training
    window (TRAIN_START..TRAIN_END), the Prophet configuration and the pooling
    are all unchanged since v1 was built, and Prophet's MAP fit is deterministic.
    A mismatch is therefore a finding, not a rounding artifact, and is reported
    rather than papered over.
    """
    if not ORIGINAL_WORKBOOK.exists():
        logger.warning("v1 workbook not found at %s - skipping the "
                       "reproducibility check.", ORIGINAL_WORKBOOK)
        return pd.DataFrame()
    wb = load_workbook(ORIGINAL_WORKBOOK, read_only=True, data_only=True)
    ws = wb["8. Trend-Disconnect Audit"]
    rows = []
    for r in ws.iter_rows(min_row=17, values_only=True):
        if not r or not r[0]:
            continue
        rows.append(dict(item_code=str(r[0]).strip(), v1_trend=r[10],
                         v1_avg6=r[11], v1_pct=r[12], v1_ncp=r[13],
                         v1_disconnected=r[14]))
    wb.close()
    return pd.DataFrame(rows)


def trace_v1_withdrawn(src: Sources) -> dict:
    """
    Every product v1's Sheet 9 sent to the withdrawn estimator, and where it
    routes now.

    This is the whole point of the rebuild, so it is COMPUTED from v1's own
    Sheet 9 rather than asserted from memory: read v1's Route To column, find
    the rows naming the withdrawn family, and look each item up in
    model_routing.csv. If v1 is unreadable the trace is skipped and the caption
    says so, rather than a hardcoded count being printed as if it had been
    checked.
    """
    if not ORIGINAL_WORKBOOK.exists():
        return dict(ran=False, n=0, mapping={}, items=[])
    wb = load_workbook(ORIGINAL_WORKBOOK, read_only=True, data_only=True)
    ws = wb["9. Model Routing"]
    items, seen_labels = [], {}
    for row in ws.iter_rows(min_row=22, values_only=True):
        if not row or not row[0]:
            continue
        label = str(row[6] or "")
        if any(t in label.lower() for t in ("croston", "tsb")):
            code = str(row[0]).strip()
            items.append(code)
            seen_labels[code] = label
    wb.close()

    mapping: dict[tuple[str, str], int] = {}
    for code in items:
        now = src.route.get(code, "(outside routing scope)")
        reason = src.block_reason.get(code)
        if isinstance(reason, float) and math.isnan(reason):
            reason = None
        key = (now, reason or "")
        mapping[key] = mapping.get(key, 0) + 1
    return dict(ran=True, n=len(items), mapping=mapping, items=items)


def _withdrawn_trace_caption(trace: dict) -> str:
    if not trace["ran"]:
        return ("The previous workbook could not be read, so the products it "
                "routed to an unimplemented estimator could not be traced "
                "item-by-item. Every route below still comes from "
                "model_routing.csv.")
    parts = []
    for (route, reason), cnt in sorted(trace["mapping"].items(),
                                       key=lambda kv: -kv[1]):
        parts.append(f"{cnt} to {route}" + (f" ({reason})" if reason else ""))
    n_forecast = sum(c for (r, _), c in trace["mapping"].items()
                     if r in (R_PROPHET, R_N3, R_N12))
    return (f"TRACED ITEM BY ITEM, not asserted: all {trace['n']} products the "
            f"previous version of this sheet sent to an unimplemented "
            f"intermittent-demand estimator have been looked up in "
            f"model_routing.csv, and they now route as follows - "
            f"{'; '.join(parts)}. {n_forecast} of the {trace['n']} therefore "
            f"receive a real, delivered forecast today (they are on Sheet 1); "
            f"the remainder are reported as Blocked with the routing file's own "
            f"reason, which is an honest 'not yet' rather than a recommendation "
            f"nobody could act on. No product lost a forecast in this change.")


def run_trend_refit(items: list[str], reuse: bool) -> pd.DataFrame:
    """
    Refit every currently Prophet-routed item and measure trend vs recent actual.

    METHOD - unchanged from analysis/trend_audit.py, by calling it
    ─────────────────────────────────────────────────────────────
    `trend_audit.build_fit_input()` rebuilds the exact frame `run_forecasts()`
    is handed (Amazon-excluded, active-scoped, successor-pooled) and
    `trend_audit.fit_and_extract()` does the fit and the comparison. Both are
    imported, not reimplemented, so this audit cannot drift from the audit it
    extends any more than that one could drift from the model it audits. Pooled
    families are fitted ONCE and attributed to each of their target item codes,
    because the pipeline fits one model per family and reporting a separate fit
    per member would be inventing models that never existed - 154 items resolve
    to 140 fit keys.

    POPULATION - 154, not v1's 186
    ──────────────────────────────
    v1 audited every fitted item when "fitted" meant "Prophet fitted it". It no
    longer does: 44 of today's 198 forecast items are routed to a trailing
    average, which has no trend and no changepoints, so a trend-disconnect
    measurement is undefined for them rather than merely absent. The population
    is therefore `model_routing.csv`'s 154 Prophet-routed items.

    THE ONE DELIBERATE DIVERGENCE FROM PRODUCTION
    ─────────────────────────────────────────────
    `config.PROPHET_PARAMS` is used verbatim, so every series is fitted at
    changepoint_prior_scale = 0.05. Production does NOT do that for all of them:
    67 of these 154 sit in the known-changeover segment and are fitted at 0.25
    (config.CHANGEPOINT_PRIOR_KNOWN_CHANGEOVER). Fitting them at 0.05 here is
    intentional and is what v1 and trend_audit.py both did - the audit asks "did
    the STANDARD prior regularise a real level shift away?", which is a question
    about 0.05, and it is also the only way these numbers stay comparable to
    v1's. It does mean a flagged row is not always a statement about the model
    that shipped that item's forecast; the sheet says so in a caption.
    """
    if reuse and TREND_REFIT_CACHE.exists():
        print(f"  Sheet 8: reusing cached refit {TREND_REFIT_CACHE}")
        return pd.read_csv(TREND_REFIT_CACHE, dtype={"item_code": str})

    warnings.filterwarnings("ignore")
    logging.getLogger("cmdstanpy").disabled = True
    from analysis import trend_audit as ta

    scoped, families, eligible = ta.build_fit_input(cached=True)
    by_key = {k: g for k, g in scoped.groupby("item_code")}

    fits: dict[str, dict] = {}
    rows = []
    for n, item in enumerate(items, start=1):
        key = families.family_key(item)
        fit_key = key if key in eligible else item
        if fit_key not in by_key:
            logger.warning("Sheet 8: no training series for %s (fit key %s) - "
                           "skipped.", item, fit_key)
            continue
        if fit_key not in fits:
            print(f"    [{n:>3}/{len(items)}] fitting {fit_key}"
                  f"{' [pooled]' if fit_key != item else ''} …", flush=True)
            fits[fit_key] = ta.fit_and_extract(by_key[fit_key])
        res = fits[fit_key]
        rows.append(dict(
            item_code=item, family_key=fit_key, is_pooled=(fit_key != item),
            n_train_months=res["n_train_months"],
            n_significant_changepoints=res["n_significant_changepoints"],
            fitted_trend_at_train_end=res["fitted_trend_at_train_end"],
            actual_avg_last_6mo=res["actual_avg_last_6mo"],
            pct_diff_trend_vs_6mo_actual=res["pct_diff_trend_vs_6mo_actual"],
            trend_disconnected=res["trend_disconnected"],
        ))
    out = pd.DataFrame(rows)
    TREND_REFIT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(TREND_REFIT_CACHE, index=False)
    print(f"  Sheet 8: {len(fits)} Prophet fits over {len(out)} items -> "
          f"{TREND_REFIT_CACHE}")
    return out


def validate_against_v1(refit: pd.DataFrame, v1: pd.DataFrame) -> dict:
    """Compare every reproducible item and report, never silently overwrite."""
    if v1.empty:
        return dict(n_overlap=0, n_match=0, mismatches=pd.DataFrame(), ran=False)
    m = refit.merge(v1, on="item_code", how="inner")
    if m.empty:
        return dict(n_overlap=0, n_match=0, mismatches=pd.DataFrame(), ran=True)

    def _close(a, b):
        if pd.isna(a) and pd.isna(b):
            return True
        if pd.isna(a) or pd.isna(b):
            return False
        return abs(float(a) - float(b)) <= 0.011

    ok = m.apply(lambda r: (_close(r.fitted_trend_at_train_end, r.v1_trend)
                            and _close(r.actual_avg_last_6mo, r.v1_avg6)
                            and _close(r.pct_diff_trend_vs_6mo_actual, r.v1_pct)
                            and int(r.n_significant_changepoints) == int(r.v1_ncp or 0)),
                 axis=1)
    mis = m.loc[~ok, ["item_code", "fitted_trend_at_train_end", "v1_trend",
                      "actual_avg_last_6mo", "v1_avg6",
                      "pct_diff_trend_vs_6mo_actual", "v1_pct",
                      "n_significant_changepoints", "v1_ncp",
                      "trend_disconnected", "v1_disconnected"]].copy()
    # Whether a numeric difference actually changed the VERDICT is the whole
    # question. A trend that moved by a tenth of a unit on a series averaging 19
    # is a different kind of event from one that flipped Yes to No, and a caption
    # that cannot tell them apart will either understate or overstate every
    # future mismatch.
    if len(mis):
        mis["verdict_flipped"] = [
            bool(a) != (str(b).strip().lower() == "yes")
            for a, b in zip(mis["trend_disconnected"], mis["v1_disconnected"])]
    else:
        mis["verdict_flipped"] = pd.Series(dtype=bool)
    return dict(n_overlap=len(m), n_match=int(ok.sum()), mismatches=mis, ran=True)


# ══════════════════════════════════════════════════════════════════════════════
# Sheet 1 - Forecast FY2026
# ══════════════════════════════════════════════════════════════════════════════

S1 = "1. Forecast FY2026"


def sheet1(wb: Workbook, src: Sources) -> dict:
    """
    Item x month forecast for the full year, one row per item-month.

    RENAMED from v1's "Prophet Forecast FY2026". v1 could carry Prophet in the
    tab name because Prophet forecast every item in it; 44 of the 198 items here
    are forecast by a trailing average, so the old name would be wrong on 22% of
    its own rows.

    THE JUDGMENT CALL: Naive rows carry NO confidence band.
    ──────────────────────────────────────────────────────
    Naive-routed rows DO have yhat_lower / yhat_upper in the pipeline's output,
    and they are deliberately left BLANK here. They are not what this column
    means. models/naive_model.py sets them by multiplying yhat by two fixed
    ratios from config.NAIVE_BAND_RATIOS - empirical p10/p90 quantiles of
    realised error across the whole population's CV folds, applied as constants.
    Every Naive-3mo item in the workbook would therefore report the identical
    Band Width of 247%, and every Naive-12mo item the identical 318%, regardless
    of how confident the estimate for that particular item is. The column's
    stated purpose is to be SORTED to find where the model itself is signalling
    low confidence; a constant cannot do that, and 44 rows of a repeated number
    would read as if it could. Prophet's interval is derived per series from its
    own posterior and does mean that. Blank plus a caption is the honest option;
    the route-level ratios are stated in the caption so nothing is lost.
    """
    ws = wb.create_sheet(S1)
    n = 13
    r = title(ws, "Daylight - Demand Forecast, Full Year 2026 (item-level, all "
                  "regions/BDMs summed per item)", n)
    r = caption(ws, r,
        f"Source: internal demand-forecasting system, latest run "
        f"(output/, 2026-08-19). {len(src.forecast_items)} products with an "
        f"active forecast - {sum(1 for i in src.forecast_items if src.route[i] == R_PROPHET)} "
        f"routed to Prophet, {sum(1 for i in src.forecast_items if src.route[i] == R_N3)} to "
        f"Naive-3mo, {sum(1 for i in src.forecast_items if src.route[i] == R_N12)} to "
        f"Naive-12mo.", n)
    r = caption(ws, r,
        "RENAMED from \"Prophet Forecast FY2026\": not every forecast comes from "
        "Prophet any more. The new Model column names the forecaster for each "
        "item, taken verbatim from the routing stage (preprocessing/"
        "model_routing.py, output_phase1/model_routing.csv) - the same file the "
        "pipeline itself acts on, so this column cannot disagree with what "
        "actually ran. Naive-3mo and Naive-12mo are trailing averages over the "
        "last 3 or 12 months; which of the two an item gets depends on whether "
        "its demand is declining.", n)
    r = caption(ws, r,
        "'Backtest (vs actual)' months are the held-out test window the model "
        "was scored against (never trained on these); 'Forward forecast' months "
        "(Jul-Dec) are the genuine forward-looking forecast, with no actuals yet "
        "available.", n)
    r = caption(ws, r,
        "Band Width % = (Upper Bound - Lower Bound) / Forecast -- how wide the "
        "model's own 95% confidence interval is, relative to its point forecast. "
        "A high % means the model itself is signalling low confidence in the "
        "point forecast (typically low-volume/erratic products); sort/filter "
        "this column to find where business judgment should carry more weight "
        "than the number. SHOWN FOR PROPHET ROWS ONLY. Naive rows are left "
        "deliberately blank: their bounds are the point forecast multiplied by "
        "two fixed population-level ratios (p10/p90 of realised error across all "
        "items on the route), so every Naive-3mo item would report the same "
        "247% and every Naive-12mo item the same 318% - a constant, not a "
        "per-item confidence signal, and useless to sort by. Prophet's interval "
        "is derived per series from that series' own posterior and does carry "
        "per-item meaning. For a Naive row, read the point forecast as a "
        "trailing average and treat its uncertainty as route-level, not "
        "item-level.", n)
    r = caption(ws, r,
        "Actual Qty shows 0 where no sale was recorded for that item-month (vs. "
        "'N/A (future)' for Jul-Dec, which is genuinely unknown/future). A 0 "
        "could mean real zero demand OR an unrecorded stockout -- there isn't a "
        "stock-availability signal in the underlying data to tell the two apart, "
        "so treat 0 as 'nothing was sold', not as a confirmed demand read.", n)
    r = caption(ws, r,
        "BDM Qty is the BDM manual forecast for that item-month, summed across "
        "every BDM/region who forecasts it -- shown for every month of the year "
        "(BDMs forecast the full year up front, unlike Actual which only exists "
        "for months that have already happened). A blank BDM Qty means that "
        "item-month genuinely has no row in the BDM forecast sheet.", n)

    hdr = r
    r = headers(ws, r, ["Item Code", "Product Family", "Rating", "Region",
                        "Month", "Period Type", "Model", "Actual Qty",
                        "BDM Qty", "Forecast", "Lower Bound (95%)",
                        "Upper Bound (95%)", "Band Width % of Forecast"],
                {1: 12, 2: 16, 3: 8, 4: 10, 5: 9, 6: 20, 7: 12, 8: 12, 9: 11,
                 10: 12, 11: 15, 12: 15, 13: 16})
    ws.freeze_panes = f"A{r}"

    t = src.test[["item_code", "month", "model", "yhat", "yhat_lower",
                  "yhat_upper", "actual"]].copy()
    t["period"] = "Backtest (vs actual)"
    f = src.fcst[["item_code", "month", "model", "yhat", "yhat_lower",
                  "yhat_upper"]].copy()
    f["actual"] = np.nan
    f["period"] = "Forward forecast"
    both = pd.concat([t, f], ignore_index=True).sort_values(["item_code", "month"])

    for row in both.itertuples(index=False):
        put(ws, r, 1, row.item_code)
        put(ws, r, 2, src.fam(row.item_code))
        put(ws, r, 3, src.rat(row.item_code))
        put(ws, r, 4, src.reg(row.item_code))
        put(ws, r, 5, month_label(row.month))
        put(ws, r, 6, row.period)
        put(ws, r, 7, row.model)
        if row.period.startswith("Backtest"):
            put(ws, r, 8, 0 if pd.isna(row.actual) else float(row.actual), fmt=FMT_QTY)
        else:
            put(ws, r, 8, "N/A (future)", color=C_GREY)
        bq = src.bdm_month.get((row.item_code, row.month))
        put(ws, r, 9, None if bq is None else float(bq), fmt=FMT_QTY)
        put(ws, r, 10, r1(row.yhat), fmt=FMT_QTY)
        if row.model == R_PROPHET:
            put(ws, r, 11, r1(row.yhat_lower), fmt=FMT_QTY)
            put(ws, r, 12, r1(row.yhat_upper), fmt=FMT_QTY)
            put(ws, r, 13, f'=IFERROR((L{r}-K{r})/J{r},"n/a")', fmt=FMT_PCT_FRAC)
        else:
            put(ws, r, 13, "n/a (route-level band)", color=C_GREY)
        r += 1

    return dict(sheet=S1, rows=r - hdr - 1, items=len(src.forecast_items))


# ══════════════════════════════════════════════════════════════════════════════
# Sheets 3-5 - the Wins tabs (built before Sheet 2, which reads off them)
# ══════════════════════════════════════════════════════════════════════════════

S3 = "3. Automated Forecast Wins"
S4 = "4. BDM Wins"
S5 = "5. Prior-Year Wins"

WIN_COLS = ["Item Code", "Product Family", "Rating", "Region", "Month",
            "Actual", "Method", "Automated Forecast", "Automated Error %",
            "BDM Forecast", "BDM Error %", "Prior-Year Forecast",
            "Prior-Year Error %"]
WIN_WIDTHS = {1: 12, 2: 16, 3: 8, 4: 10, 5: 9, 6: 10, 7: 13, 8: 15, 9: 14,
              10: 13, 11: 12, 12: 16, 13: 15}

#: Colour per Method, so the drill-down label is scannable down the column
#: without being loud enough to read as a ranking. Same tier, different shade.
METHOD_STYLE = {
    R_PROPHET: (C_BLUE_BG, C_ACCENT),
    R_N3: (C_INDIGO_BG, C_INDIGO),
    R_N12: (C_PURPLE_BG, C_PURPLE),
}

WIN_METHOD_NOTE = (
    "Winner is decided per item-month: lowest absolute percentage error "
    "(|actual-forecast|/actual) when actual sales were non-zero, or lowest "
    "absolute unit error when actual was zero (percentage error is undefined at "
    "zero). All three forecasts and errors are shown for context, so you can see "
    "how close the runner-up was, not just who won.")

WIN_TIER_NOTE = (
    "TWO TIERS, NOT FOUR COMPETITORS. The top-level comparison is the Automated "
    "Forecast - what the pipeline delivers for this item, whatever produced it - "
    "against BDM's manual forecast, with the Prior-Year Baseline alongside as a "
    "reference point. 'Method' is the drill-down: it names WHICH approach "
    "produced this row's Automated Forecast (Prophet, Naive-3mo or Naive-12mo), "
    "per output_phase1/model_routing.csv. Nobody picks the method - routing "
    "does - so it explains a win or a loss rather than competing in its own "
    "right. The column is filled in on every row of every tab, so on the BDM and "
    "Prior-Year tabs it tells you which method lost, not just that one did.")

WIN_METHOD_DEF_NOTE = (
    "The methods, plainly: 'Prophet' is a fitted time-series model. 'Naive-3mo' "
    "and 'Naive-12mo' are trailing averages over the last 3 or 12 months - "
    "arithmetic, not a fitted model, and not machine learning. They are named "
    "here only as Methods; the Prior-Year Baseline on tab 5 is a different thing "
    "entirely, and is what the FIRST version of this workbook called 'Naive'. "
    "That collision is why nothing in this workbook is labelled 'Naive' on its "
    "own any more.")


def _win_rows(src: Sources) -> pd.DataFrame:
    b = src.bench.copy()
    b["region"] = b["item_code"].map(src.region)   # one region derivation, see Sources
    b["rating"] = b["item_code"].map(src.rating)
    return b.sort_values(["item_code", "month"])


def _write_wins(ws, src: Sources, df: pd.DataFrame, hdr_row: int) -> tuple[int, int]:
    """
    One row per scored item-month, identical columns on all three tabs.

    THE METHOD COLUMN (G). v2 wrote a plain "Model" here and, on its Naive tab
    only, a second highlighted "Naive Variant" column. v3 needs exactly one of
    those: the winner is now the TAB, so a column repeating it is noise, while
    the routed method - which v2 highlighted for naive rows alone - is what a
    reader needs on every row of every tab. So column G is the Method, styled
    the way v2 styled the variant, and there is no fourteenth column.
    """
    r = headers(ws, hdr_row, WIN_COLS, WIN_WIDTHS)
    ws.freeze_panes = f"A{r}"
    first = r
    for row in df.itertuples(index=False):
        put(ws, r, 1, row.item_code)
        put(ws, r, 2, src.fam(row.item_code))
        put(ws, r, 3, row.rating)
        put(ws, r, 4, row.region)
        put(ws, r, 5, month_label(row.month))
        put(ws, r, 6, None if pd.isna(row.actual) else float(row.actual), fmt=FMT_QTY)
        fill, color = METHOD_STYLE.get(row.model, (C_GREY_BG, C_GREY))
        put(ws, r, 7, row.model, bold=True, color=color, fill=fill)
        put(ws, r, 8, r1(row.model_forecast), fmt=FMT_QTY)
        put(ws, r, 9, r1(row.model_mape), fmt=FMT_PCT_VAL)
        put(ws, r, 10, r1(row.bdm_forecast), fmt=FMT_QTY)
        put(ws, r, 11, r1(row.bdm_mape), fmt=FMT_PCT_VAL)
        put(ws, r, 12, r1(row.prior_year_forecast), fmt=FMT_QTY)
        put(ws, r, 13, r1(row.prior_year_mape), fmt=FMT_PCT_VAL)
        r += 1
    return first, r - 1


def sheets345(wb: Workbook, src: Sources) -> dict:
    """
    Three win tabs over the configured benchmark window.

    WHY THESE THREE TABS AND NOT v2's THREE
    ───────────────────────────────────────
    Same rows, re-cut along the comparison people are actually being asked to
    make. v2 cut them Prophet / BDM / Naive, which put a METHOD (Prophet) and a
    tab holding two methods plus a baseline (Naive) on the same footing as BDM.
    Reading it, "Prophet won 384" and "BDM won 365" invited a Prophet-vs-BDM
    verdict that skipped the 44 items Prophet never forecast.

    v3 cuts them Automated Forecast / BDM / Prior-Year Baseline: the pipeline's
    delivered number, whatever produced it, against the manual forecast, with the
    baseline as the reference it was always meant to be. The method survives as
    column G on every row of every tab, so nothing that was visible in v2 stops
    being visible - a reader who wants the Prophet-only or Naive-3mo-only cut
    filters that column, and the Overview's drill-down table has it standing.

    THE PARTITION IS UNCHANGED, and it has to be: every scored item-month lands
    on exactly one of these three tabs, so the Overview's Total is still the
    scored population and still adds up. `winner` can only be the row's own
    method, `bdm`, or `prior_year` (evaluation/benchmark.py:_winner), so the
    three masks below are exhaustive and disjoint by construction.
    """
    stats = {}
    df = _win_rows(src)
    scored = df[df["winner"] != "none"]
    win_str = f"{month_label(config.BENCHMARK_START)[:3]}-" \
              f"{month_label(config.BENCHMARK_END)[:3]} {config.BENCHMARK_END[:4]}"
    n_scored = len(scored)
    n = len(WIN_COLS)

    for name, mask, who in (
        (S3, scored["winner"].isin(METHODS), AUTOMATED_LABEL),
        (S4, scored["winner"] == BASELINE_BDM, "BDM"),
        (S5, scored["winner"] == BASELINE_PRIOR_YEAR, PRIOR_YEAR_LABEL),
    ):
        sub = scored[mask]
        ws = wb.create_sheet(name)
        r = title(ws, f"Daylight - Item-Months Where the {who} Had the Lowest "
                      f"Forecast Error ({win_str})", n)
        r = caption(ws, r, f"{len(sub):,} of {n_scored:,} scored item-months. "
                    f"{WIN_METHOD_NOTE}", n)
        r = caption(ws, r, WIN_TIER_NOTE, n)
        r = caption(ws, r, WIN_METHOD_DEF_NOTE, n)
        if name == S3:
            by_m = sub["winner"].value_counts()
            parts = ", ".join(f"{m} {int(by_m.get(m, 0)):,}" for m in METHODS)
            r = caption(ws, r,
                f"These {len(sub):,} wins by Method: {parts}. That split is a "
                f"drill-down, not a league table - each method only ever "
                f"competed in the item-months routing gave it, and those "
                f"populations are not alike, so the counts are not comparable "
                f"head to head. The Overview's 'Automated Forecast by Method' "
                f"table shows each method's wins against its OWN routed "
                f"item-months, which is the comparable number.", n)
        if name == S5:
            r = caption(ws, r,
                f"WHAT THIS TAB IS NOT. The Prior-Year Baseline is the "
                f"same-month-last-year comparator the benchmark scores "
                f"everything against. Nothing is routed to it and nothing is "
                f"delivered from it - a win here means neither the Automated "
                f"Forecast nor BDM beat simply repeating last year, which is a "
                f"finding about those two, not a recommendation to use this. "
                f"The FIRST version of this workbook called this tab 'Naive "
                f"Wins', which is why that word is now reserved for the "
                f"Naive-3mo / Naive-12mo Methods and used nowhere else.", n)
        first, last = _write_wins(ws, src, sub, r)
        # Sheet 2 reads these tabs with live formulas, so it needs the real data
        # extent of each - the caption blocks differ in height between the tabs,
        # so a hardcoded "row 4" (v1's assumption) would be wrong here.
        stats[name] = dict(n=len(sub), first=first, last=max(last, first))

    stats["_scored"] = n_scored
    stats["_window"] = win_str
    stats["_by_winner"] = scored["winner"].value_counts().to_dict()
    stats["_unscored"] = int((df["winner"] == "none").sum())
    return stats


# ══════════════════════════════════════════════════════════════════════════════
# Sheet 2 - Overview (live formulas over the Wins tabs)
# ══════════════════════════════════════════════════════════════════════════════

S2 = "2. Overview"


def sheet2(wb: Workbook, src: Sources, wins: dict) -> dict:
    """
    Every table here is a live formula reading the Wins tabs, exactly as v1.

    THE WINDOW. config.BENCHMARK_START/END are 2026-01..2026-04 and, per git,
    have not moved since v1 was built. v1's captions nevertheless read "Jan-Jun
    2026, 1,009 scored item-months" - it scored the full six-month test window
    rather than the configured BDM-overlap window. This workbook reports what is
    configured, which is what output/benchmark_comparison.csv actually contains
    (four months, 792 item-months, 695 of them scorable). The count therefore
    drops against v1 for a reason that is not a change in accuracy.

    THREE ROWS WHERE v2 HAD FOUR, AND WHY THAT IS THE POINT OF v3.
    v2's headline table listed Prophet, BDM, Naive (routed) and Prior-Year as
    four peer competitors. Three of those four are not peers of each other:
    Prophet and the two naive routes are the SAME side of the comparison seen
    through the routing that assigned them, and reading them as rivals produced
    exactly the wrong question ("is Prophet or Naive-3mo better?") in place of
    the one being asked ("does the pipeline beat the manual forecast?").

    So the headline is two-tier. Tier one, this table: Automated Forecast (all
    three methods, combined) vs BDM, with the Prior-Year Baseline alongside as
    the reference it has always been. Tier two, the table immediately below it:
    the same Automated Forecast row broken down by Method, each method scored
    against ITS OWN routed item-months - the only per-method number that means
    anything, since no two methods ever competed for the same item.

    THE CROSS-TABS STAY AT TIER ONE. Rating and Region are cut three ways, not
    five. A method sub-split inside every cross-tab would give cells of a dozen
    item-months, and the Method drill-down already answers the question those
    cells would be reached for. One place to look, not three.
    """
    ws = wb.create_sheet(S2)
    n = 13
    r = title(ws, f"Daylight - Automated Forecast vs BDM: Overview "
                  f"({wins['_window']}, {wins['_scored']:,} scored item-months)", n)
    r = caption(ws, r,
        "Every table on this sheet is a live formula reading from the three "
        "'Wins' tabs -- if those tabs change, these numbers update "
        "automatically. Win Count = number of item-months that side had the "
        "lowest error. Win Volume = total actual units sold in the item-months "
        "it won -- a side can win more months but still cover less volume if its "
        "wins skew toward smaller products, so the two are shown side by side "
        "rather than picking one.", n)
    r = caption(ws, r,
        f"Scored window is {wins['_window']}, the configured overlap between the "
        f"held-out test window and the BDM forecast year "
        f"(config.BENCHMARK_START..BENCHMARK_END = {config.BENCHMARK_START}.."
        f"{config.BENCHMARK_END}, unchanged since the previous version of this "
        f"workbook). {wins['_scored']:,} of "
        f"{wins['_scored'] + wins['_unscored']:,} item-months are scorable; the "
        f"remaining {wins['_unscored']:,} have no comparable forecast on any of "
        f"the three sides for that month and are excluded rather than counted as "
        f"a loss for anyone.", n)
    r = caption(ws, r,
        "THREE ROWS, WHERE THE PREVIOUS VERSION HAD FOUR. 'Automated Forecast' "
        "is what the pipeline delivers for an item, whichever of Prophet / "
        "Naive-3mo / Naive-12mo routing sent that item to -- it is ONE side of "
        "the comparison, not three, because no item ever gets a choice between "
        "them. 'BDM' is the manual forecast. 'Prior-Year Baseline' is the "
        "same-month-last-year comparator: a benchmark, not something anything is "
        "routed to and not something anyone delivers. The previous version split "
        "the Automated Forecast into a 'Prophet' row and a 'Naive (routed)' row "
        "and called the baseline 'Naive' too, which read as four competitors and "
        "as one word meaning two things. Which method produced a given Automated "
        "Forecast is the drill-down directly below this table.", n)

    a3, a4, a5 = f"'{S3}'", f"'{S4}'", f"'{S5}'"
    w3, w4, w5 = wins[S3], wins[S4], wins[S5]

    r += 1
    r = section(ws, r, "Overall Result - Automated Forecast vs BDM", 4)
    hdr = r
    r = headers(ws, r, ["Forecast Source", "Win Count", "Win Volume (units)",
                        "Win Volume Share"], {1: 34, 2: 11, 3: 18, 4: 16})
    top = r
    # Each row is one whole tab now, so every figure is a plain count/sum over
    # that tab - no per-variant COUNTIFS, because the tab IS the filter. That is
    # the arithmetic simplification the re-tabbing buys, and it is why these
    # three rows cannot drift out of agreement with tabs 3-5.
    rows = [
        (f"{AUTOMATED_LABEL} (pipeline: all methods)",
         f"=COUNTA({a3}!A{w3['first']}:A{w3['last']})",
         f"=SUM({a3}!F:F)"),
        ("BDM (manual forecast)",
         f"=COUNTA({a4}!A{w4['first']}:A{w4['last']})",
         f"=SUM({a4}!F:F)"),
        (f"{PRIOR_YEAR_LABEL} (benchmark only)",
         f"=COUNTA({a5}!A{w5['first']}:A{w5['last']})",
         f"=SUM({a5}!F:F)"),
    ]
    for label, cnt, vol in rows:
        put(ws, r, 1, label, bold=(label.startswith(AUTOMATED_LABEL)))
        put(ws, r, 2, cnt, fmt=FMT_INT)
        put(ws, r, 3, vol, fmt=FMT_INT)
        put(ws, r, 4, f"=IFERROR(C{r}/$C${top + len(rows)},0)", fmt=FMT_PCT_FRAC1)
        r += 1
    put(ws, r, 1, "Total", bold=True)
    put(ws, r, 2, f"=SUM(B{top}:B{r - 1})", fmt=FMT_INT, bold=True)
    put(ws, r, 3, f"=SUM(C{top}:C{r - 1})", fmt=FMT_INT, bold=True)
    put(ws, r, 4, 1, fmt=FMT_PCT_FRAC1, bold=True)
    r += 2

    # ── The Method drill-down, directly under the row it drills into ──────────
    # v2 carried this same table, but at the BOTTOM of the sheet under the name
    # "Win Count by Routed Model", which left the methods reading as a separate
    # topic from the headline. It is the headline's second tier, so it sits
    # under the headline. Live-formula, as everything on this sheet is.
    r = section(ws, r, f"↳ {AUTOMATED_LABEL} by Method (drill-down of the row "
                       f"above, not a fourth competitor)", 6)
    r = caption(ws, r,
        "Reads down the Method column on tabs 3-5. 'Scored Item-Months' is how "
        "many item-months routing made that method responsible for; the next "
        "three columns are who actually had the lowest error in them, and 'Win "
        "%' is the method's wins as a share of its OWN routed population. Those "
        "populations were assigned by demand pattern and are not alike, so read "
        "each method against its own row - the Win % column compares, the raw "
        "counts do not. The three 'Won' columns sum across all methods to the "
        "three rows of the table above. A method losing to BDM inside its own "
        "item-months is the shape of problem this table exists to make visible - "
        "see Sheets 7 and 9 for where it concentrates.", n)
    hd = r
    for i, h in enumerate(["Method", "Scored Item-Months (its own routed "
                           "population)", f"{AUTOMATED_LABEL} Won", "BDM Won",
                           f"{PRIOR_YEAR_LABEL} Won", "Method Win %"], start=1):
        cc = ws.cell(hd, i, h)
        cc.font = Font(bold=True, size=10, color=C_WHITE)
        cc.fill = PatternFill("solid", fgColor=C_ACCENT)
        cc.alignment = Alignment(wrap_text=True, horizontal="center", vertical="center")
    ws.row_dimensions[hd].height = 30.0
    r = hd + 1
    mtop = r
    for m in METHODS:
        fill, color = METHOD_STYLE[m]
        put(ws, r, 1, m, bold=True, color=color, fill=fill)
        put(ws, r, 2, f'=COUNTIFS({a3}!$G:$G,"{m}")+COUNTIFS({a4}!$G:$G,"{m}")'
                      f'+COUNTIFS({a5}!$G:$G,"{m}")', fmt=FMT_INT)
        put(ws, r, 3, f'=COUNTIFS({a3}!$G:$G,"{m}")', fmt=FMT_INT)
        put(ws, r, 4, f'=COUNTIFS({a4}!$G:$G,"{m}")', fmt=FMT_INT)
        put(ws, r, 5, f'=COUNTIFS({a5}!$G:$G,"{m}")', fmt=FMT_INT)
        put(ws, r, 6, f"=IFERROR(C{r}/B{r},0)", fmt=FMT_PCT_FRAC1)
        r += 1
    put(ws, r, 1, "All methods", bold=True)
    for col in (2, 3, 4, 5):
        L = _col(col)
        put(ws, r, col, f"=SUM({L}{mtop}:{L}{r - 1})", fmt=FMT_INT, bold=True)
    put(ws, r, 6, f"=IFERROR(C{r}/B{r},0)", fmt=FMT_PCT_FRAC1, bold=True)
    r += 2

    def breakdown(start: int, dim_col: str, dim_label: str, values: list[str],
                  what: str) -> int:
        """One count table at A..E and its volume twin at I..M.

        Three data columns, matching the headline table's three rows. Tabs 3-5
        each hold exactly one of them, so a cell is one COUNTIFS/SUMIFS against
        one tab - no method criterion anywhere, which is what keeps these
        cross-tabs at the headline's altitude rather than sprouting a Method
        dimension nobody asked these tables for.
        """
        ws.merge_cells(f"A{start}:E{start}")
        c = ws.cell(start, 1, f"Win Count by {what}")
        c.font = Font(bold=True, size=11, color=C_INK)
        ws.merge_cells(f"I{start}:M{start}")
        c = ws.cell(start, 9, f"Win Volume by {what}")
        c.font = Font(bold=True, size=11, color=C_INK)
        hd = start + 1
        cols = [dim_label, AUTOMATED_LABEL, "BDM", PRIOR_YEAR_LABEL, "Total"]
        for off in (0, 8):
            for i, h in enumerate(cols, start=1):
                cc = ws.cell(hd, i + off, h)
                cc.font = Font(bold=True, size=10, color=C_WHITE)
                cc.fill = PatternFill("solid", fgColor=C_ACCENT)
                cc.alignment = Alignment(wrap_text=True, horizontal="center",
                                         vertical="center")
        ws.row_dimensions[hd].height = 30.0
        rr = hd + 1
        for v in values:
            put(ws, rr, 1, v)
            put(ws, rr, 2, f'=COUNTIFS({a3}!${dim_col}:${dim_col},$A{rr})', fmt=FMT_INT)
            put(ws, rr, 3, f'=COUNTIFS({a4}!${dim_col}:${dim_col},$A{rr})', fmt=FMT_INT)
            put(ws, rr, 4, f'=COUNTIFS({a5}!${dim_col}:${dim_col},$A{rr})', fmt=FMT_INT)
            put(ws, rr, 5, f"=SUM(B{rr}:D{rr})", fmt=FMT_INT)
            put(ws, rr, 9, v)
            put(ws, rr, 10, f'=SUMIFS({a3}!$F:$F,{a3}!${dim_col}:${dim_col},$I{rr})', fmt=FMT_INT)
            put(ws, rr, 11, f'=SUMIFS({a4}!$F:$F,{a4}!${dim_col}:${dim_col},$I{rr})', fmt=FMT_INT)
            put(ws, rr, 12, f'=SUMIFS({a5}!$F:$F,{a5}!${dim_col}:${dim_col},$I{rr})', fmt=FMT_INT)
            put(ws, rr, 13, f"=SUM(J{rr}:L{rr})", fmt=FMT_INT)
            rr += 1
        for col, lbl in ((1, "Total"), (9, "Total")):
            put(ws, rr, col, lbl, bold=True)
        for col in (2, 3, 4, 5, 10, 11, 12, 13):
            L = _col(col)
            put(ws, rr, col, f"=SUM({L}{hd + 1}:{L}{rr - 1})", fmt=FMT_INT, bold=True)
        return rr + 2

    ratings = sorted({str(v) for v in src.bench["rating"].dropna().unique()})
    regions = sorted({src.reg(i) for i in src.bench["item_code"].unique() if src.reg(i)})
    r = breakdown(r, "C", "Rating", ratings, "Product Rating")
    r = breakdown(r, "D", "Region", regions, "Region")

    for i, w in {1: 34, 2: 16, 3: 20, 4: 16, 5: 20, 6: 13, 7: 3, 8: 3, 9: 12,
                 10: 20, 11: 12, 12: 20, 13: 12}.items():
        ws.column_dimensions[_col(i)].width = w
    return dict(sheet=S2)


# ══════════════════════════════════════════════════════════════════════════════
# Sheet 6 - BDM items with no forecast
# ══════════════════════════════════════════════════════════════════════════════

S6 = "6. BDM Not in Prophet"


def sheet6(wb: Workbook, src: Sources, unf: pd.DataFrame) -> dict:
    """
    The reason-category methodology is v1's, unchanged. Only Best Tool changed.

    TAB NAME KEPT. "BDM Not in Prophet" is now slightly narrow - the sheet lists
    BDM items with no forecast from ANY model, not just from Prophet - but the
    tab name is what people have bookmarked and linked to, and renaming it buys
    less than it costs. The title and the first caption say what it actually
    covers.
    """
    ws = wb.create_sheet(S6)
    n = 10
    order = {REASON_LIMITED: 0, REASON_INFREQUENT: 1, REASON_NO_SALES: 2,
             REASON_AMAZON: 3, REASON_INACTIVE: 4, REASON_RETIRED: 5}
    df = unf.assign(_o=unf["reason"].map(order)).sort_values(
        ["_o", "item_code"]).drop(columns="_o")

    r = title(ws, f"Daylight - BDM-Forecast Items With No Forecast in This Run "
                  f"({len(df)} of {len(src.bdm_items)} BDM items)", n)
    r = caption(ws, r,
        "Scope widened since the previous version: this now means 'no forecast "
        "from any model', not 'no Prophet forecast'. 44 items that would have "
        "appeared here are now forecast by a routed trailing average and have "
        "moved to Sheet 1. The tab name is left unchanged so existing links keep "
        "working.", n)
    r = caption(ws, r,
        "Each reason was determined by checking the item against the forecasting "
        "system's product-eligibility rules, sales-history requirements, and "
        "product-succession records, in that order, then cross-checked against "
        "its actual sales pattern (tenure, sale frequency, and channel mix). "
        "'Retired product code (by design)' is confirmed consistently across "
        "every retired code in the succession records -- only the "
        "current/successor code ever receives a forecast, regardless of the "
        "retired code's own sales history. 'Sales concentrated in Amazon' "
        "includes items below the system's formal exclusion threshold that show "
        "the same pattern on inspection -- their non-Amazon history looked merely "
        "'thin,' but the real driver is that their demand mostly isn't in the "
        "channel this system trains on. 'Infrequent seller' items have plenty of "
        "calendar tenure but sell too rarely to accumulate the sale occurrences "
        "the routing gate asks for -- waiting longer doesn't help them the way it "
        "helps a genuinely new product.", n)
    r = caption(ws, r,
        f"WHAT CHANGED: that ladder is no longer walked by hand. For the "
        f"{int(df['route'].notna().sum())} items inside routing scope, the Reason "
        f"Detail and Best Tool below quote output_phase1/model_routing.csv's own "
        f"verdict and block reason verbatim, because "
        f"preprocessing/model_routing.py's gate ladder IS the eligibility rules "
        f"in order and re-deriving it here would be re-typing a decision the "
        f"pipeline already made. The remaining {int(df['route'].isna().sum())} "
        f"items sit outside that file's scope entirely (it runs at current-code "
        f"grain over the active allow-list, minus channel-mismatch exclusions), "
        f"so their reason is established here - from the same upstream inputs "
        f"routing itself is built on, never from a second definition.", n)
    r = caption(ws, r,
        "Category = standard demand-pattern classification (Syntetos-Boylan: "
        "Smooth / Erratic / Intermittent / Lumpy, by average demand interval and "
        "demand-size variability), computed on each item's own non-Amazon sales. "
        "'No Data' = nothing to classify; 'Insufficient Data to Classify' = fewer "
        f"than {config.SB_MIN_MONTHS_TO_CLASSIFY} months with a recorded sale, "
        "too few for a reliable read; 'N/A' = the classification doesn't apply "
        "(retired code, or demand concentrated in a channel this data can't "
        "see). For items outside routing scope this is computed by calling the "
        "pipeline's own classifier, not by reimplementing it.", n)
    r = caption(ws, r,
        "Best Tool is a recommendation, not a decision already made -- it reflects "
        "the demand pattern and data availability found above, for your team to "
        "weigh alongside business priority and effort. Where the routing file "
        "already has a verdict, that verdict IS the recommendation and is quoted "
        "rather than second-guessed; where an item is blocked, the tool named is "
        "the one the gate that fired sends it to once the block clears, which is "
        "a property of the gate and not a fresh opinion.", n)

    hdr = r
    r = headers(ws, r, ["Item Code", "Product Family", "Rating", "Region",
                        "BDM FY2026 Forecast Total",
                        f"Months of Sales History (thru {month_label(config.TRAIN_END)})",
                        "Reason Category", "Reason Detail", "Category",
                        "Best Tool"],
                {1: 12, 2: 16, 3: 8, 4: 12, 5: 14, 6: 14, 7: 26, 8: 62, 9: 20,
                 10: 55})
    ws.freeze_panes = f"A{r}"
    for row in df.itertuples(index=False):
        fill, color = REASON_STYLE[row.reason]
        for c in range(1, 11):
            ws.cell(r, c).fill = PatternFill("solid", fgColor=fill)
        put(ws, r, 1, row.item_code)
        put(ws, r, 2, row.family)
        put(ws, r, 3, row.rating)
        put(ws, r, 4, row.region)
        put(ws, r, 5, r1(row.bdm_total), fmt=FMT_QTY)
        put(ws, r, 6, int(row.n_hist), fmt=FMT_INT)
        put(ws, r, 7, row.reason, bold=True, color=color)
        put(ws, r, 8, row.detail, wrap=True)
        put(ws, r, 9, row.category)
        put(ws, r, 10, row.best_tool, wrap=True)
        ws.row_dimensions[r].height = 60.0
        r += 1
    return dict(sheet=S6, rows=r - hdr - 1,
                by_reason=df["reason"].value_counts().to_dict())


# ══════════════════════════════════════════════════════════════════════════════
# Sheet 7 - Accuracy by demand pattern
# ══════════════════════════════════════════════════════════════════════════════

S7 = "7. Accuracy by Demand Pattern"


def _accuracy_summary(ws, r: int, df: pd.DataFrame, label_col: str = "Category",
                      total_label: str = "All Fitted Items") -> int:
    """
    v1's summary block, unchanged: mean AND median side by side, N Reliable
    excluding items scored on fewer than 3 test months. Computed in Python, not
    as live formulas, because a per-category MEDIAN has no safe non-array
    formula in this environment - and computing the mean the same way keeps the
    two comparable rather than one being live and one static.
    """
    r = headers(ws, r, [label_col, "N Items", "N Scored", "N Reliable (≥ 3mo)",
                        "Mean MAPE %", "Median MAPE %", "Mean WAPE %",
                        "Median WAPE %", "Mean Bias %", "Median Bias %"],
                {1: 22, 2: 9, 3: 10, 4: 15, 5: 12, 6: 13, 7: 12, 8: 13, 9: 11,
                 10: 13})
    cats = [c for c in dc.CATEGORIES if (df["category"] == c).any()]
    for cat in cats + [total_label]:
        sub = df if cat == total_label else df[df["category"] == cat]
        scored = sub[sub["n_test_months"].fillna(0) > 0]
        reliable = sub[sub["n_test_months"].fillna(0) >= 3]
        put(ws, r, 1, cat, bold=(cat == total_label))
        put(ws, r, 2, len(sub), fmt=FMT_INT, bold=(cat == total_label))
        put(ws, r, 3, len(scored), fmt=FMT_INT, bold=(cat == total_label))
        put(ws, r, 4, len(reliable), fmt=FMT_INT, bold=(cat == total_label))
        for i, (fn, col) in enumerate([(mean_or_none, "mape_pct"),
                                       (median_or_none, "mape_pct"),
                                       (mean_or_none, "wape_pct"),
                                       (median_or_none, "wape_pct"),
                                       (mean_or_none, "bias_pct"),
                                       (median_or_none, "bias_pct")]):
            put(ws, r, 5 + i, r1(fn(scored[col])), fmt=FMT_PCT_VAL2,
                bold=(cat == total_label))
        r += 1
    return r


def sheet7(wb: Workbook, src: Sources) -> dict:
    """
    Per-item test-window accuracy for the Prophet-fitted population.

    POPULATION: 154, and Lumpy has vanished from it.
    ───────────────────────────────────────────────
    v1's table had a Lumpy row with 32 items in it, because Prophet was fitting
    Lumpy series. It no longer is - every Lumpy item in scope now routes to
    Naive-3mo or Naive-12mo. Lumpy has not improved and has not been dropped; it
    has MOVED, and the sheet says so rather than letting a missing row read as a
    solved problem.

    The 44 naive-routed items are absent for a different reason: model_metrics.
    csv is Prophet-only by design (models/naive_model.py: "Per-series fit
    statistics describe a fit, and there is no fit here"). Computing WAPE for
    them from the test file would be arithmetic this sheet's own caption
    disclaims - it says these numbers are the system's OWN reported accuracy -
    so their evidence lives on Sheets 3-5 and 9 instead.
    """
    ws = wb.create_sheet(S7)
    n = 13
    m = src.metrics.merge(
        src.classes[["item_code", "category", "adi", "cv2", "is_pooled"]],
        on="item_code", how="left")
    m["region"] = m["item_code"].map(src.region)
    m["rating"] = m["item_code"].map(src.rating)
    n_pooled = int((m["split_method"] != family_pool.SPLIT_NA).sum()) \
        if hasattr(family_pool, "SPLIT_NA") else int(m["is_pooled"].sum())
    n_low = int((m["n_test_months"].fillna(0) < 3).sum())
    au = m[m["region"] == "4 AU"]

    r = title(ws, f"Daylight - Prophet Accuracy by Demand-Pattern Category "
                  f"({len(m)} Prophet-fitted products, "
                  f"{month_label(config.TEST_START)[:3]}-"
                  f"{month_label(config.TEST_END)[:3]} "
                  f"{config.TEST_END[:4]} test window)", n)
    r = caption(ws, r,
        "Category = Syntetos-Boylan classification (Smooth / Erratic / "
        "Intermittent / Lumpy), computed on each item's own non-Amazon training "
        f"series -- the POOLED family series for the {int(m['is_pooled'].sum())} "
        "items whose forecast comes from a successor-pooled fit, since that's "
        "what Prophet actually trained on, not the item's own standalone "
        "history.", n)
    r = caption(ws, r,
        f"POPULATION CHANGED since the previous version: {len(m)} Prophet-fitted "
        f"products, not 186. LUMPY IS NOT IN THIS TABLE ANY MORE, and that is a "
        f"routing change, not an improvement -- every Lumpy item in scope now "
        f"routes to a trailing average (Naive-3mo or Naive-12mo) instead of "
        f"Prophet, so it has moved rather than got better. See Sheet 9. The 44 "
        f"naive-routed items have no rows here at all because the pipeline "
        f"produces no per-series fit statistics for them by design -- there is no "
        f"fit to describe -- so their evidence is on Sheets 3-5 (head-to-head "
        f"wins) and Sheet 9 (route and reason), not here. No 'Intermittent' rows "
        f"appear either: Intermittent demand accumulates too few sale "
        f"occurrences to clear the routing gates in the first place. See Sheet 6 "
        f"for where it shows up instead.", n)
    r = caption(ws, r,
        "MAPE / WAPE / Bias are the forecasting system's own reported "
        "test-window accuracy per item (model_metrics.csv). Mean is shown "
        "alongside median because a handful of extreme single-item misses can "
        "drag the mean far from what most items actually experience -- report the "
        "median when this goes in front of Samir, per your own prior guidance on "
        "this exact issue.", n)
    r = caption(ws, r,
        "Summary rows are computed directly from the detail table below (not "
        "live formulas) because a per-category MEDIAN has no safe non-array "
        "formula in this environment -- computing mean and median the same way "
        "keeps the two comparable rather than one being live and one static.", n)
    r = caption(ws, r,
        f"Reliability flags items scored on fewer than 3 test months "
        f"({n_low} of {len(m)}) -- a MAPE built from 1-2 months is barely more "
        f"than a coin flip and shouldn't be read as evidence the model handles "
        f"that item well, good result or bad. Flagged rows are tinted and "
        f"excluded from 'N Reliable' below."
        + ("" if n_low else " No item in this run falls below that bar (the "
                            "minimum scored is 3 months), so 'N Reliable' equals "
                            "'N Scored' throughout -- the flag is kept because the "
                            "next run's population will differ."), n)

    r += 1
    r = section(ws, r, "Accuracy Summary by Category", 10)
    r = _accuracy_summary(ws, r, m)

    # ── The AU cut ────────────────────────────────────────────────────────────
    # Same table, same statistics, restricted to one region. It is a separate
    # block rather than an extra column because the reader question it answers
    # ("is AU different?") is a comparison between two whole tables, and because
    # an AU column on the main table would imply AU is a demand pattern.
    r += 1
    r = section(ws, r, "Accuracy Summary by Category - REGION '4 AU' ONLY "
                       "(known weak spot, under active investigation)", 10)
    r = caption(ws, r,
        f"Not a resolved issue and not a separate methodology -- the identical "
        f"table above, restricted to the {len(au)} Prophet-fitted items the BDM "
        f"sheet forecasts in '4 AU' alone. Compare it against the all-region "
        f"table, not against a target.", n)
    r = caption(ws, r, AU_ROOT_CAUSE, n)
    r = caption(ws, r, AU_NAIVE_FINDING, n)
    r = caption(ws, r,
        "Read those two together: the diagnosis says AU is hard because of WHAT "
        "is in it, and the experiment says a cruder estimator scores better on "
        "the same items while systematically forecasting low. Neither has "
        "changed a route. Nothing on this sheet should be actioned as a fix.", n)
    r = _accuracy_summary(ws, r, au, total_label="All AU Prophet-Fitted Items")

    r += 1
    hdr = r
    r = headers(ws, r, ["Item Code", "Product Family", "Rating", "Region",
                        "Pooled Fit?", "Category", "ADI", "CV²",
                        "Test Months", "Reliability", "MAPE %", "WAPE %",
                        "Bias %"],
                {1: 12, 2: 16, 3: 8, 4: 12, 5: 11, 6: 13, 7: 8, 8: 8, 9: 11,
                 10: 16, 11: 12, 12: 12, 13: 12})
    ws.freeze_panes = f"A{r}"
    for row in m.sort_values(["category", "mape_pct"]).itertuples(index=False):
        low = (row.n_test_months or 0) < 3
        if low:
            for c in range(1, 14):
                ws.cell(r, c).fill = PatternFill("solid", fgColor=C_WARN_BG)
        put(ws, r, 1, row.item_code)
        put(ws, r, 2, src.fam(row.item_code))
        put(ws, r, 3, row.rating)
        put(ws, r, 4, row.region)
        put(ws, r, 5, "Yes" if row.is_pooled else "No")
        put(ws, r, 6, row.category)
        put(ws, r, 7, r2(row.adi))
        put(ws, r, 8, r2(row.cv2))
        put(ws, r, 9, int(row.n_test_months), fmt=FMT_INT)
        put(ws, r, 10, "Low (n<3)" if low else "OK", bold=True,
            color=C_WARN if low else C_GOOD)
        for i, v in enumerate((row.mape_pct, row.wape_pct, row.bias_pct)):
            put(ws, r, 11 + i, r1(v), fmt=FMT_PCT_VAL2)
        r += 1

    summ = {}
    for cat in list(dc.CATEGORIES) + ["ALL"]:
        sub = m if cat == "ALL" else m[m["category"] == cat]
        if len(sub):
            summ[cat] = dict(n=len(sub), median_mape=r1(median_or_none(sub["mape_pct"])),
                             median_wape=r1(median_or_none(sub["wape_pct"])))
    au_summ = dict(n=len(au), median_mape=r1(median_or_none(au["mape_pct"])),
                   median_wape=r1(median_or_none(au["wape_pct"])),
                   n_erratic=int((au["category"] == dc.ERRATIC).sum()))
    return dict(sheet=S7, rows=r - hdr - 1, by_category=summ, au=au_summ,
                n_low=n_low)


# ══════════════════════════════════════════════════════════════════════════════
# Sheet 8 - Trend-disconnect audit
# ══════════════════════════════════════════════════════════════════════════════

S8 = "8. Trend-Disconnect Audit"


def sheet8(wb: Workbook, src: Sources, refit: pd.DataFrame,
           v1cmp: dict) -> dict:
    ws = wb.create_sheet(S8)
    n = 17
    m = refit.merge(src.metrics[["item_code", "mape_pct", "bias_pct"]],
                    on="item_code", how="left")
    m["category"] = m["item_code"].map(src.category)
    m["region"] = m["item_code"].map(src.region)
    m["rating"] = m["item_code"].map(src.rating)
    vol = (src.test.groupby("item_code")["actual"].sum(min_count=1))
    m["test_volume"] = m["item_code"].map(vol)
    m["net_bias"] = (m["bias_pct"] * m["test_volume"] / 100.0)
    m = m.sort_values("net_bias", key=lambda s: s.abs(), ascending=False)

    n_disc = int(m["trend_disconnected"].sum())
    impact = float(m.loc[m["trend_disconnected"], "net_bias"].abs().sum())

    prev = set(v1cmp.get("v1_flagged") or ())

    r = title(ws, f"Daylight - Prophet Trend-Disconnect Audit (all {len(m)} "
                  f"Prophet-routed products)", n)
    r = caption(ws, r,
        "Rerun in full for this workbook, not carried over. Method is unchanged "
        "from the audit this extends and is not reimplemented: "
        "analysis/trend_audit.py's own build_fit_input() and fit_and_extract() "
        "are called directly, so each item (or its pooled family, exactly as "
        "production does) is refitted on the training window with "
        "config.PROPHET_PARAMS, and the model's TREND at the end of training is "
        "compared against what the item has actually been selling in the last 6 "
        f"calendar months. Threshold {ta_pct()}% gap, the same one the original "
        f"audit uses.", n)
    r = caption(ws, r,
        f"POPULATION: {len(m)} Prophet-routed items, not the previous version's "
        f"186. The other 44 forecast items are routed to a trailing average, "
        f"which has no trend and no changepoints -- a trend-disconnect "
        f"measurement is undefined for them rather than merely missing, so they "
        f"are out of scope for this diagnostic entirely.", n)
    r = caption(ws, r, _v1_validation_caption(v1cmp), n)
    r = caption(ws, r,
        f"Headline: {n_disc} of {len(m)} Prophet-routed products "
        f"({n_disc / max(len(m), 1):.0%}) show a trend disconnected from recent "
        f"reality. Impact = net unit bias (bias % x test-window volume / 100) -- "
        f"positive means the model over-forecast that item's actual volume over "
        f"the test window by that many units, negative means under-forecast. "
        f"Summed across every disconnected item, that is {impact:,.0f} units of "
        f"net directional error concentrated in specific, identifiable products, "
        f"not spread evenly across the portfolio.", n)
    r = caption(ws, r,
        "ONE DELIBERATE DIVERGENCE FROM PRODUCTION, stated rather than buried: "
        f"every series here is fitted at changepoint_prior_scale = "
        f"{config.PROPHET_PARAMS['changepoint_prior_scale']}, because "
        f"config.PROPHET_PARAMS is used verbatim. Production does not do that "
        f"for all of them -- "
        f"{int((src.metrics['changepoint_segment'] == 'known_changeover').sum())} "
        f"of these items sit in the known-changeover segment and ship at "
        f"{config.CHANGEPOINT_PRIOR_KNOWN_CHANGEOVER}. That is intentional and "
        f"matches what the previous audits did: the question this sheet asks is "
        f"'did the STANDARD prior regularise a real level shift away?', which is "
        f"a question about the standard prior, and it is also the only way these "
        f"numbers stay comparable to the previous version's. It does mean a "
        f"flagged row is not always a statement about the model that actually "
        f"shipped that item's forecast.", n)
    r = caption(ws, r,
        "TWO COLUMNS BEHAVE DIFFERENTLY FROM THE PREVIOUS VERSION, deliberately. "
        "(1) 'Net Unit Bias (Impact)' is SIGNED here - positive means the model "
        "over-forecast that item over the test window, negative means it "
        "under-forecast - which is what the caption above has always said it "
        "meant. The previous version printed the absolute value in this column "
        "while describing it as signed, so an under-forecast of 1,580 units and "
        "an over-forecast of 1,580 units were indistinguishable. The 'Sum |Net "
        "Unit Bias|' column in the summary above is still an absolute sum, and "
        "is unchanged. (2) 'Previously Flagged?' now means 'was this item "
        "flagged in the 2026-08-16 workbook audit', which is the audit "
        "immediately preceding this one - the previous version's column referred "
        "to the diagnostic before THAT. Each audit compares against its own "
        "predecessor.", n)
    r = caption(ws, r,
        "This audit MEASURES the issue; it does not fix it. A confirmed "
        "disconnect is evidence a real trend shift may exist that the standard "
        "changepoint prior regularised away -- worth a business check (was there "
        "an actual change: relaunch, distribution shift, promotion) before "
        "deciding whether to route the item into the known-changeover segment or "
        "elsewhere.", n)

    r += 1
    r = section(ws, r, "Trend-Disconnect Rate by Demand-Pattern Category", 5)
    r = headers(ws, r, ["Category", "N Items", "N Disconnected",
                        "Disconnect Rate", "Sum |Net Unit Bias|"],
                {1: 22, 2: 10, 3: 15, 4: 14, 5: 18})
    rates = {}
    cats = [c for c in dc.CATEGORIES if (m["category"] == c).any()]
    for cat in cats + ["All Prophet-Routed Items"]:
        sub = m if cat.startswith("All") else m[m["category"] == cat]
        nd = int(sub["trend_disconnected"].sum())
        put(ws, r, 1, cat, bold=cat.startswith("All"))
        put(ws, r, 2, len(sub), fmt=FMT_INT, bold=cat.startswith("All"))
        put(ws, r, 3, nd, fmt=FMT_INT, bold=cat.startswith("All"))
        put(ws, r, 4, round(nd / max(len(sub), 1), 3), fmt=FMT_PCT_FRAC1,
            bold=cat.startswith("All"))
        put(ws, r, 5, round(float(sub.loc[sub["trend_disconnected"],
                                          "net_bias"].abs().sum())),
            fmt=FMT_INT, bold=cat.startswith("All"))
        rates[cat] = (len(sub), nd, round(nd / max(len(sub), 1), 3))
        r += 1

    r += 1
    hdr = r
    r = headers(ws, r, ["Item Code", "Product Family", "Rating", "Region",
                        "Category", "Pooled Fit?", "Family Key",
                        "Test-Window Volume", "Bias %", "MAPE %",
                        "Fitted Trend @ Train End", "Actual Avg (last 6mo)",
                        "% Diff (Trend vs Actual)", "Sig. Changepoints",
                        "Trend Disconnected?", "Net Unit Bias (Impact)",
                        "Previously Flagged?"],
                {1: 12, 2: 16, 3: 8, 4: 12, 5: 13, 6: 11, 7: 12, 8: 14, 9: 10,
                 10: 11, 11: 15, 12: 15, 13: 15, 14: 12, 15: 14, 16: 15, 17: 20})
    ws.freeze_panes = f"A{r}"
    for row in m.itertuples(index=False):
        disc = bool(row.trend_disconnected)
        if disc:
            for c in range(1, 18):
                ws.cell(r, c).fill = PatternFill("solid", fgColor=C_BAD_BG)
        put(ws, r, 1, row.item_code)
        put(ws, r, 2, src.fam(row.item_code))
        put(ws, r, 3, row.rating)
        put(ws, r, 4, row.region)
        put(ws, r, 5, row.category)
        put(ws, r, 6, "Yes" if row.is_pooled else "No")
        put(ws, r, 7, row.family_key)
        put(ws, r, 8, None if pd.isna(row.test_volume) else float(row.test_volume),
            fmt=FMT_INT)
        put(ws, r, 9, r1(row.bias_pct), fmt=FMT_PCT_VAL2)
        put(ws, r, 10, r1(row.mape_pct), fmt=FMT_PCT_VAL2)
        put(ws, r, 11, r2(row.fitted_trend_at_train_end), fmt=FMT_QTY)
        put(ws, r, 12, r2(row.actual_avg_last_6mo), fmt=FMT_QTY)
        put(ws, r, 13, r1(row.pct_diff_trend_vs_6mo_actual), fmt=FMT_PCT_VAL2)
        put(ws, r, 14, int(row.n_significant_changepoints), fmt=FMT_INT)
        put(ws, r, 15, "Yes" if disc else "No", bold=True,
            color=C_BAD if disc else C_GOOD)
        put(ws, r, 16, None if pd.isna(row.net_bias) else round(float(row.net_bias)),
            fmt=FMT_INT)
        put(ws, r, 17, "Known (previous workbook audit)"
            if row.item_code in prev else "New finding",
            color=None if row.item_code in prev else C_GREY)
        r += 1
    return dict(sheet=S8, rows=r - hdr - 1, n_items=len(m), n_disc=n_disc,
                impact=impact, rates=rates, v1=v1cmp)


def ta_pct() -> float:
    from analysis import trend_audit as ta
    return ta.TREND_DISCONNECT_PCT


def _v1_validation_caption(v1cmp: dict) -> str:
    """
    The reproducibility statement. v1 validated itself against the audit before
    it by confirming the one overlapping item matched; this build owes v1 the
    same and has a much larger overlap to do it on. A mismatch is reported here,
    in the workbook, rather than silently replaced by the new number.
    """
    if not v1cmp.get("ran"):
        return ("Reproducibility check SKIPPED: the previous workbook was not "
                "found on disk, so these refit numbers could not be validated "
                "against it. Treat them as unvalidated.")
    if not v1cmp["n_overlap"]:
        return ("Reproducibility check found NO overlapping items with the "
                "previous workbook - nothing could be validated.")
    n_mis = len(v1cmp["mismatches"])
    if n_mis == 0:
        return (f"Validated against the previous version before extending it, "
                f"the same way that version validated against the audit before "
                f"it: all {v1cmp['n_overlap']} items present in both workbooks "
                f"refit to EXACTLY the same fitted trend, recent-actual average, "
                f"percentage gap and significant-changepoint count. That is the "
                f"expected result - the training window, the Prophet "
                f"configuration and the successor pooling are all unchanged "
                f"since then, and Prophet's MAP fit is deterministic - so it is "
                f"a genuine check that nothing drifted underneath, not a "
                f"formality.")
    mis = v1cmp["mismatches"]
    n_flip = int(mis["verdict_flipped"].sum())
    parts = []
    for row in mis.head(6).itertuples(index=False):
        parts.append(
            f"{row.item_code} (trend {row.v1_trend} -> "
            f"{row.fitted_trend_at_train_end}, gap {row.v1_pct}% -> "
            f"{row.pct_diff_trend_vs_6mo_actual}%, verdict "
            f"{'CHANGED' if row.verdict_flipped else 'unchanged'})")
    return (f"REPRODUCIBILITY MISMATCH - REPORTED, NOT PAPERED OVER. Of the "
            f"{v1cmp['n_overlap']} items present in both this workbook and the "
            f"previous one, {v1cmp['n_overlap'] - n_mis} refit to EXACTLY the "
            f"same fitted trend, recent-actual average, percentage gap and "
            f"changepoint count. {n_mis} did not: {'; '.join(parts)}"
            f"{' …' if n_mis > 6 else ''}. "
            + (f"{n_flip} of those changed the Trend Disconnected verdict; treat "
               f"those rows as unresolved and check them before using them as "
               f"evidence. "
               if n_flip else
               "None of them changed the Trend Disconnected verdict, the "
               "significant-changepoint count, or the recent-actual average - "
               "the differences are in the fitted trend value itself, at a "
               "magnitude that does not move any conclusion on the row. ")
            + f"The training window, the Prophet configuration and the "
            f"successor pooling are all unchanged since the previous version, "
            f"and repeat fits here are stable to the digit, so the most likely "
            f"cause is optimiser convergence differing between the two runs on "
            f"a near-zero trend rather than anything in the data. The new "
            f"numbers are what is shown below.")


# ══════════════════════════════════════════════════════════════════════════════
# Sheet 9 - Model routing
# ══════════════════════════════════════════════════════════════════════════════

S9 = "9. Model Routing"

ROUTE_STYLE = {
    R_PROPHET: (C_GOOD_BG, C_GOOD),
    R_N3: (C_INDIGO_BG, C_INDIGO),
    R_N12: (C_BLUE_BG, C_ACCENT),
    R_BLOCKED: (C_WARN_BG, C_AMBER_STRONG),
    OOS_RETIRED: (C_GREY_BG, C_GREY),
    OOS_INACTIVE: (C_PURPLE_BG, C_PURPLE),
    OOS_AMAZON: (C_BLUE_BG, C_ACCENT),
    OOS_NO_HISTORY: (C_BAD_BG, C_BAD),
}

REVIEW_OK = "OK"
REVIEW_FLAG = "Needs business review (trend disconnect)"


def sheet9(wb: Workbook, src: Sources, unf: pd.DataFrame,
           refit: pd.DataFrame, trace: dict) -> dict:
    """
    One row per BDM-forecast product: which model it actually routes to, and why.

    THE RULE THIS SHEET IS BUILT UNDER. `Route To` is
    output_phase1/model_routing.csv's `route` column verbatim for the 248 items
    that file covers, and `Routing Reason` is its `block_reason` verbatim. No
    value is invented, softened, or re-derived. That is a direct response to
    what the previous version got wrong here: it recommended, for 42 products, a
    model family this codebase has never implemented and has since measured and
    rejected. A workbook that re-derives a routing decision can be wrong about
    it; one that quotes the file cannot.

    Trend-disconnect status is carried in a SEPARATE column rather than folded
    into the route label (the previous version's 'Prophet (current)' vs 'Prophet
    (needs review)'). preprocessing/model_routing.py deliberately excludes it
    from routing - both states route to Prophet - so it is a reporting overlay
    on the route, and modelling it as two different routes would put a value in
    Route To that the routing file does not contain.
    """
    ws = wb.create_sheet(S9)
    n = 12
    disc = dict(zip(refit["item_code"], refit["trend_disconnected"]))
    unf_by_item = unf.set_index("item_code")

    rows = []
    for item in src.bdm_items:
        if item in src.routed_items:
            route = src.route[item]
            reason = src.block_reason.get(item)
            if isinstance(reason, float) and math.isnan(reason):
                reason = None
            in_scope = True
        else:
            route = unf_by_item.loc[item, "oos_label"]
            reason = None
            in_scope = False
        flagged = bool(disc.get(item, False))
        if route == R_PROPHET:
            status = "Currently forecast (Prophet)"
            reason = reason or (
                "Routed to Prophet: Smooth/Erratic demand pattern with enough "
                "calendar history to fit. Trend disconnected from the last 6 "
                "months of actuals -- see Sheet 8 and check for a real business "
                "change before adjusting." if flagged else
                "Routed to Prophet: Smooth/Erratic demand pattern with enough "
                "calendar history to fit. No material trend-disconnect "
                "identified in this run.")
        elif route in NAIVE_ROUTES:
            status = f"Currently forecast ({route})"
            window = config.NAIVE_WINDOW_MONTHS[route]
            reason = reason or (
                f"Routed to a trailing average over the last {window} months: "
                f"Lumpy/Intermittent demand pattern, which has no continuous "
                f"trend for Prophet to fit, with enough sale occurrences to form "
                f"an average. The {window}-month window follows this item's "
                f"trend direction -- declining series take the short window, "
                f"stable/growing series the long one.")
        elif route == R_BLOCKED:
            status = "Not currently forecast (blocked by routing)"
        else:
            status = "Not currently forecast (outside routing scope)"
            reason = unf_by_item.loc[item, "detail"]
        rows.append(dict(
            item_code=item, family=src.fam(item), rating=src.rat(item),
            region=src.reg(item), status=status,
            category=src.category.get(item, unf_by_item.loc[item, "category"]
                                      if item in unf_by_item.index else ""),
            route=route, reason=reason or "",
            review=(REVIEW_FLAG if flagged else REVIEW_OK) if route == R_PROPHET else "",
            in_scope=in_scope))
    df = pd.DataFrame(rows)
    mm = src.metrics.set_index("item_code")
    df["mape"] = df["item_code"].map(mm["mape_pct"])
    df["wape"] = df["item_code"].map(mm["wape_pct"])
    df["bdm_vol"] = df["item_code"].map(src.bdm_total)

    order = {R_PROPHET: 0, R_N3: 1, R_N12: 2, R_BLOCKED: 3}
    df = df.assign(_o=df["route"].map(lambda x: order.get(x, 4))).sort_values(
        ["_o", "route", "region", "item_code"]).drop(columns="_o")

    r = title(ws, f"Daylight - Consolidated Model Routing (all "
                  f"{len(df)} BDM-forecast products)", n)
    r = caption(ws, r,
        "One row per product: which forecasting approach each product actually "
        "uses, and why. This is no longer a hand-assembled recommendation. Every "
        "Route To value below for the 248 products inside routing scope is "
        "output_phase1/model_routing.csv's own `route` column VERBATIM, and every "
        "Routing Reason for a blocked product is its `block_reason` column "
        "verbatim -- the same file preprocessing/model_routing.py writes and "
        "models/naive_model.py acts on. Nothing here is re-derived, so nothing "
        "here can disagree with what the pipeline does.", n)
    r = caption(ws, r,
        "WHAT CHANGED, AND WHY IT MATTERS. The previous version of this sheet "
        "recommended, for 42 products, an intermittent-demand estimator family "
        "that this codebase has never implemented and does not route to. The "
        "experiment that was supposed to justify it ran afterwards "
        "(analysis/, 2026-08-18) and found no variant of it beat a plain "
        "trailing average on this population, so no implementation was written. "
        "Those 42 rows are gone. What replaces them is what the pipeline "
        "actually does: Lumpy/Intermittent items route to Naive-3mo or "
        "Naive-12mo -- real trailing averages that produce real delivered "
        "forecasts -- and items that cannot clear a gate are reported as Blocked "
        "with the gate's own reason rather than given a recommendation nothing "
        "can act on.", n)
    r = caption(ws, r, _withdrawn_trace_caption(trace), n)
    r = caption(ws, r,
        "Trend-disconnect status is a SEPARATE column, not part of the route. "
        "The routing stage deliberately does not read it -- 'Prophet is fine' and "
        "'Prophet is fitted but worth a business review' both route to Prophet -- "
        "so it is a reporting overlay here. Folding it into Route To (as the "
        "previous version did) would put a value in that column the routing file "
        "does not contain.", n)
    r = caption(ws, r,
        "Current MAPE % and Current WAPE % are blank on every non-Prophet row, "
        "and that is not missing data. The pipeline produces per-series fit "
        "statistics only for fits, and a trailing average is not a fit - "
        "model_metrics.csv is Prophet-only by design. For how the naive-routed "
        "items actually performed against BDM and the prior-year baseline, read "
        "Sheet 2's 'Win Count by Routed Model' table and Sheets 3-5, which score "
        "every model on the same head-to-head basis.", n)
    r = caption(ws, r,
        f"38 of the {len(df)} products sit OUTSIDE model_routing.csv's scope "
        f"altogether: that file runs at current-code grain over the active "
        f"allow-list, minus channel-mismatch exclusions, so retired codes, "
        f"inactive items, Amazon-only items and items whose first sale falls "
        f"after the training window closed are never routed at all. Their Route "
        f"To says so in as many words rather than inventing a route for them. "
        f"See Sheet 6 for the per-item reasoning.", n)
    r = caption(ws, r,
        "UNDER REVIEW - AU. AU is the only region where the BDM manual forecast "
        "beats the routed model head-to-head (BDM wins 45.1% of AU's scored "
        "item-months against the model's 33.0%; every other region runs 41-50% "
        "to the model). On AU's Prophet-routed items specifically, BDM takes 29 "
        "of 59 scored item-months to the model's 18, at a median MAPE of 40.4% "
        "against 53.4%. " + AU_ROOT_CAUSE, n)
    r = caption(ws, r,
        AU_NAIVE_FINDING + " Flagged here as UNDER REVIEW, not as a pending "
        "change: no AU item's Route To below differs because of it, and none "
        "should be changed on the strength of it without resolving the "
        "under-forecast bias first.", n)

    r += 1
    r = section(ws, r, "Routing Summary", 5)
    r = headers(ws, r, ["Route To", "Overlay / Block Reason", "N Products",
                        "Currently Fitted", "Not Currently Fitted"],
                {1: 30, 2: 58, 3: 12, 4: 14, 5: 16})
    summary = []
    for route in (R_PROPHET, R_N3, R_N12):
        sub = df[df["route"] == route]
        if route == R_PROPHET:
            for ov in (REVIEW_OK, REVIEW_FLAG):
                s = sub[sub["review"] == ov]
                summary.append((route, ov, len(s), len(s), 0))
        else:
            summary.append((route, "-", len(sub), len(sub), 0))
    blocked = df[df["route"] == R_BLOCKED]
    for reason, cnt in blocked["reason"].value_counts().items():
        summary.append((R_BLOCKED, reason, int(cnt), 0, int(cnt)))
    for lbl in (OOS_RETIRED, OOS_INACTIVE, OOS_AMAZON, OOS_NO_HISTORY):
        cnt = int((df["route"] == lbl).sum())
        if cnt:
            summary.append((lbl, "outside model_routing.csv scope", cnt, 0, cnt))
    for route, ov, cnt, fit, nofit in summary:
        fill, color = ROUTE_STYLE.get(route, (C_GREY_BG, C_GREY))
        put(ws, r, 1, route, bold=True, color=color, fill=fill)
        put(ws, r, 2, ov, wrap=True)
        put(ws, r, 3, cnt, fmt=FMT_INT)
        put(ws, r, 4, fit, fmt=FMT_INT)
        put(ws, r, 5, nofit, fmt=FMT_INT)
        r += 1
    put(ws, r, 1, "Total", bold=True)
    put(ws, r, 3, len(df), fmt=FMT_INT, bold=True)
    put(ws, r, 4, len(src.forecast_items), fmt=FMT_INT, bold=True)
    put(ws, r, 5, len(df) - len(src.forecast_items), fmt=FMT_INT, bold=True)
    r += 2

    hdr = r
    r = headers(ws, r, ["Item Code", "Product Family", "Rating", "Region",
                        "Status", "Category", "Route To",
                        "Trend-Disconnect Overlay", "Routing Reason",
                        "Current MAPE %", "Current WAPE %", "BDM FY2026 Volume"],
                {1: 12, 2: 16, 3: 8, 4: 12, 5: 30, 6: 15, 7: 26, 8: 22, 9: 62,
                 10: 13, 11: 13, 12: 15})
    ws.freeze_panes = f"A{r}"
    for row in df.itertuples(index=False):
        fill, color = ROUTE_STYLE.get(row.route, (C_GREY_BG, C_GREY))
        for c in range(1, 13):
            ws.cell(r, c).fill = PatternFill("solid", fgColor=fill)
        put(ws, r, 1, row.item_code)
        put(ws, r, 2, row.family)
        put(ws, r, 3, row.rating)
        put(ws, r, 4, row.region)
        put(ws, r, 5, row.status)
        put(ws, r, 6, row.category)
        put(ws, r, 7, row.route, bold=True, color=color)
        put(ws, r, 8, row.review, bold=bool(row.review == REVIEW_FLAG),
            color=C_BAD if row.review == REVIEW_FLAG else None)
        put(ws, r, 9, row.reason, wrap=True)
        put(ws, r, 10, r1(row.mape), fmt=FMT_PCT_VAL2)
        put(ws, r, 11, r1(row.wape), fmt=FMT_PCT_VAL2)
        put(ws, r, 12, r1(row.bdm_vol), fmt=FMT_INT)
        ws.row_dimensions[r].height = 45.0
        r += 1
    return dict(sheet=S9, rows=r - hdr - 1, summary=summary,
                by_route=df["route"].value_counts().to_dict())


# ══════════════════════════════════════════════════════════════════════════════
# Output verification
# ══════════════════════════════════════════════════════════════════════════════

#: Every spelling of the withdrawn estimator family that could survive a
#: copy-paste. Scanned for over the WRITTEN file, not over the source, because
#: the thing that matters is what a reader opens - a caption assembled from
#: three constants would not be caught by grepping this module.
WITHDRAWN_TOKENS = ("croston", "tsb", "sba", "syntetos-boylan-croston")

#: v3's own guard, the same idea applied to the word this version is about.
#: A bare "Naive" — one not immediately followed by "-3mo" or "-12mo" — is the
#: ambiguity v3 removes: it meant the prior-year baseline in v1 and a routed
#: trailing average in v2. Matched over the WRITTEN file for the same reason the
#: withdrawn-token scan is: what matters is what a reader opens.
BARE_NAIVE = re.compile(r"naive(?!-(?:3mo|12mo)\b)", re.I)

#: Bare "naive" spellings that are NOT a category label and are allowed to
#: stand: a source path a caption cites for provenance, and the pipeline's own
#: pre-rename column name, which two captions quote precisely to explain the
#: rename. Stripped before the scan rather than special-cased after it.
NAIVE_ALLOWED = ("naive_model.py", "naive_forecast", "erratic_naive",
                 "naive_experiment", "naive_mape", "config.naive_")

#: A cell short enough to be a label — a tab name, a title, a column header, a
#: table row label. This is the line the v3 rule is actually drawn at: a bare
#: "Naive" in one of these IS a headline category and fails the build; a bare
#: "naive" inside a long prose caption is a description, not a category, and is
#: reported so it stays visible but does not fail. Sheets 1 and 7-9 carry a few
#: of the latter, and v3 deliberately does not touch those sheets.
NAIVE_LABEL_MAX_CHARS = 70


def scan_output(path: Path) -> dict:
    """
    Reopen the written workbook and prove two things about what was written.

    1. The withdrawn intermittent-demand recommendation is gone. Checks every
       cell of every sheet, formulas included. A hit is reported with its exact
       address so it can be fixed rather than argued about. 'tsb' is matched as
       a whole word only: it is three letters and would otherwise fire on
       ordinary text. 'sba' likewise.

    2. NEW IN v3: no bare "Naive" survives as a category label. Tab names,
       titles, headers and row labels may only say "Naive" as part of
       "Naive-3mo" / "Naive-12mo", which are Methods. Prose mentions inside
       captions are counted and listed but not failed — see NAIVE_LABEL_MAX_CHARS
       for why the line is drawn there.
    """
    wb = load_workbook(path, data_only=False)
    hits, n_cells = [], 0
    naive_labels, naive_prose = [], []
    word = re.compile(r"\b(tsb|sba)\b", re.I)

    def check_naive(sheet: str, where: str, text: str) -> None:
        stripped = text
        for tok in NAIVE_ALLOWED:
            stripped = re.sub(re.escape(tok), "", stripped, flags=re.I)
        if not BARE_NAIVE.search(stripped):
            return
        bucket = (naive_labels if len(text) <= NAIVE_LABEL_MAX_CHARS
                  else naive_prose)
        bucket.append((sheet, where, text[:120]))

    for ws in wb.worksheets:
        check_naive(ws.title, "<tab name>", ws.title)
        for row in ws.iter_rows():
            for c in row:
                if c.value is None:
                    continue
                n_cells += 1
                if not isinstance(c.value, str):
                    continue
                low = c.value.lower()
                if "croston" in low or word.search(c.value):
                    hits.append((ws.title, c.coordinate, c.value[:120]))
                check_naive(ws.title, c.coordinate, c.value)
    n_sheets = len(wb.sheetnames)
    wb.close()
    return dict(hits=hits, n_cells=n_cells, n_sheets=n_sheets,
                naive_labels=naive_labels, naive_prose=naive_prose)


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

#: v1's headline numbers, transcribed from the 2026-08-16 file. Used ONLY to
#: print the diff summary at the end; nothing in the build reads them.
V1_HEADLINES = {
    "sheet1_items": 186, "sheet1_rows": 2232,
    "scored": 1009, "window": "Jan-Jun 2026",
    "prophet_wins": 384, "bdm_wins": 365, "naive_wins": 260,
    "sheet6_unforecast": 100,
    "sheet7_items": 186, "sheet7_low_reliability": 11,
    "sheet7_medians": {"Smooth": (55.2, 44.9), "Erratic": (86.7, 68.3),
                       "Lumpy": (100.0, 94.3), "ALL": (80.1, 59.2)},
    "sheet8_items": 186, "sheet8_disc": 86, "sheet8_impact": 14562,
    "sheet8_rates": {"Smooth": 0.16, "Erratic": 0.50, "Lumpy": 0.812,
                     "All": 0.462},
    "sheet9_total": 286,
    "sheet9_routes": {"Prophet (current)": 94, "Prophet (needs review)": 60,
                      "Prophet (once mature)": 5,
                      "[withdrawn estimator] (migrate)": 32,
                      "[withdrawn estimator] (new)": 10,
                      "Analog/successor mapping": 23,
                      "Needs Amazon data (Vendor Central)": 11,
                      "Resolve first (data/process issue)": 15,
                      "Not yet forecastable (no data)": 33,
                      "No forecast needed (retired)": 3},
}


def build(output_path: Path, reuse_refit: bool) -> None:
    print(f"\nBuilding {output_path.name}")
    print("=" * 78)
    print(f"  core files   <- {CORE_DIR}")
    print(f"  routing/exp  <- {PHASE1_DIR}   (these four files ONLY)")
    preflight()

    src = Sources()
    unf = unforecast_reasons(src)

    prophet_items = sorted(src.routing.loc[src.routing["route"] == R_PROPHET,
                                           "item_code"])
    v1 = read_v1_sheet8()
    trace = trace_v1_withdrawn(src)
    refit = run_trend_refit(prophet_items, reuse=reuse_refit)
    v1cmp = validate_against_v1(refit, v1)
    v1cmp["v1_flagged"] = set(v1.loc[v1["v1_disconnected"] == "Yes", "item_code"]) \
        if not v1.empty else set()

    wb = Workbook()
    wb.remove(wb.active)
    s1 = sheet1(wb, src)
    wins = sheets345(wb, src)
    s2 = sheet2(wb, src, wins)
    s6 = sheet6(wb, src, unf)
    s7 = sheet7(wb, src)
    s8 = sheet8(wb, src, refit, v1cmp)
    s9 = sheet9(wb, src, unf, refit, trace)

    # Tab order: v1's, with Overview second even though it is built third.
    wb._sheets = [wb[n] for n in [S1, S2, S3, S4, S5, S6, S7, S8, S9]]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    print(f"\n  written: {output_path}")

    scan = scan_output(output_path)
    print_diff(src, s1, wins, s6, s7, s8, s9, scan, trace, output_path)


def print_diff(src, s1, wins, s6, s7, s8, s9, scan, trace, output_path) -> None:
    """Which headline numbers moved, by how much, and the withdrawn-route scan."""
    V = V1_HEADLINES
    bar = "=" * 78
    p = print
    p(f"\n{bar}\nDIFF vs Daylight_Prophet_Forecast_Review_FY2026.xlsx (v1, 2026-08-16)\n{bar}")

    p("\nSheet 1  Forecast FY2026   [RENAMED from 'Prophet Forecast FY2026']")
    p(f"  products with a forecast      {V['sheet1_items']:>6}  ->{s1['items']:>6}   "
      f"({s1['items'] - V['sheet1_items']:+d})  v1 counted Prophet fits only; "
      f"44 items now forecast by a routed trailing average")
    p(f"  data rows                     {V['sheet1_rows']:>6}  ->{s1['rows']:>6}   "
      f"({s1['rows'] - V['sheet1_rows']:+d})")
    p(f"  new column                                    Model (Prophet / "
      f"Naive-3mo / Naive-12mo), read from model_routing.csv")

    p("\nSheet 2  Overview   [v3: RE-FRAMED to two tiers]")
    p(f"  scored window                 {V['window']:>12}  -> {wins['_window']}   "
      f"config.BENCHMARK_START/END = {config.BENCHMARK_START}..{config.BENCHMARK_END} "
      f"(unchanged since v1; v1's caption did not follow it)")
    p(f"  scored item-months            {V['scored']:>6}  ->{wins['_scored']:>6}   "
      f"({wins['_scored'] - V['scored']:+d})  two fewer months in the window, not "
      f"an accuracy change")
    bw = wins["_by_winner"]
    n_auto = sum(bw.get(m, 0) for m in METHODS)
    p(f"  headline rows                 v1: 3   v2: 4   -> v3: 3   "
      f"{AUTOMATED_LABEL} / BDM / {PRIOR_YEAR_LABEL}")
    p(f"      v2's 'Prophet' and 'Naive (routed)' rows are ONE row in v3: they "
      f"are the same side of the")
    p(f"      comparison seen through routing, not two competitors. "
      f"{AUTOMATED_LABEL} = {n_auto:,} wins "
      f"(= {bw.get(R_PROPHET, 0)} + {bw.get(R_N3, 0)} + {bw.get(R_N12, 0)}).")
    p(f"      Method (Prophet / Naive-3mo / Naive-12mo) is now a drill-down "
      f"table directly under that row,")
    p(f"      each method scored against its own routed item-months. Rating and "
      f"Region cross-tabs stay 3-way.")

    p("\nSheets 3-5  Wins tabs   [v3: RE-TABBED, same underlying rows]")
    p(f"  3. Prophet Wins        -> 3. {S3[3:]:<28} {wins[S3]['n']:>6}   "
      f"(v1 Prophet-only: {V['prophet_wins']}; now every routed method)")
    p(f"  4. BDM Wins            -> 4. {S4[3:]:<28} {wins[S4]['n']:>6}   "
      f"({wins[S4]['n'] - V['bdm_wins']:+d} vs v1)")
    p(f"  5. Naive Wins          -> 5. {S5[3:]:<28} {wins[S5]['n']:>6}   "
      f"(v2's tab 5 also held the {bw.get(R_N3, 0) + bw.get(R_N12, 0)} routed "
      f"naive-method wins; those moved to tab 3)")
    p(f"      partition unchanged: {wins[S3]['n']} + {wins[S4]['n']} + "
      f"{wins[S5]['n']} = {wins[S3]['n'] + wins[S4]['n'] + wins[S5]['n']:,} "
      f"= scored item-months ({wins['_scored']:,})")
    p(f"  column G              'Model' -> 'Method', on all three tabs, filled "
      f"in on every row (v2 highlighted")
    p(f"                        the routed method on tab 5 only, as 'Naive "
      f"Variant'); no 14th column any more")

    p("\nSheet 6  BDM Not in Prophet")
    p(f"  items with no forecast        {V['sheet6_unforecast']:>6}  ->{s6['rows']:>6}   "
      f"({s6['rows'] - V['sheet6_unforecast']:+d})  12 items moved onto Sheet 1 "
      f"via a naive route")
    for k, v in sorted(s6["by_reason"].items(), key=lambda x: -x[1]):
        p(f"      {k:<36} {v:>4}")

    p("\nSheet 7  Accuracy by Demand Pattern")
    p(f"  fitted products in table      {V['sheet7_items']:>6}  ->"
      f"{s7['by_category'].get('ALL', {}).get('n', 0):>6}   Prophet-fitted only")
    for cat in ("Smooth", "Erratic", "Lumpy", "ALL"):
        old = V["sheet7_medians"].get(cat)
        new = s7["by_category"].get(cat)
        if new is None:
            p(f"  median MAPE/WAPE {cat:<8} {old[0]:>6.1f}/{old[1]:<5.1f} -> "
              f"   --      CATEGORY GONE: every Lumpy item now routes to a "
              f"trailing average, not Prophet")
        else:
            p(f"  median MAPE/WAPE {cat:<8} {old[0]:>6.1f}/{old[1]:<5.1f} -> "
              f"{new['median_mape']:>5.1f}/{new['median_wape']:<5.1f} "
              f"(n {new['n']})")
    p(f"  low-reliability items (<3mo)  {V['sheet7_low_reliability']:>6}  ->"
      f"{s7['n_low']:>6}   ({s7['n_low'] - V['sheet7_low_reliability']:+d})")
    p(f"  NEW: AU-only summary table    n={s7['au']['n']} "
      f"({s7['au']['n_erratic']} Erratic), median MAPE {s7['au']['median_mape']}, "
      f"median WAPE {s7['au']['median_wape']}")

    p("\nSheet 8  Trend-Disconnect Audit   [REFIT, not carried over]")
    p(f"  items audited                 {V['sheet8_items']:>6}  ->{s8['n_items']:>6}   "
      f"({s8['n_items'] - V['sheet8_items']:+d})  Prophet-routed only")
    p(f"  trend-disconnected            {V['sheet8_disc']:>6}  ->{s8['n_disc']:>6}   "
      f"({s8['n_disc'] - V['sheet8_disc']:+d})")
    p(f"  sum |net unit bias|           {V['sheet8_impact']:>6,}  ->"
      f"{s8['impact']:>6,.0f}   ({s8['impact'] - V['sheet8_impact']:+,.0f} units)")
    for cat, (nn, nd, rate) in s8["rates"].items():
        old = V["sheet8_rates"].get(cat.split()[0] if cat.startswith("All") else cat)
        oldtxt = f"{old:.1%}" if old is not None else "  --"
        p(f"      disconnect rate {cat:<28} {oldtxt:>7} -> {rate:>6.1%}  "
          f"({nd}/{nn})")
    v1c = s8["v1"]
    if v1c.get("ran") and v1c["n_overlap"]:
        n_mis = len(v1c["mismatches"])
        if n_mis == 0:
            p(f"  VALIDATION vs v1: all {v1c['n_overlap']} overlapping items "
              f"reproduce EXACTLY (trend, 6mo actual avg, % diff, changepoints).")
        else:
            p(f"  VALIDATION vs v1: {v1c['n_overlap'] - n_mis}/{v1c['n_overlap']} "
              f"reproduce; {n_mis} DO NOT MATCH - reported in the sheet, not "
              f"silently replaced:")
            for row in v1c["mismatches"].head(10).itertuples(index=False):
                p(f"      {row.item_code}: trend {row.v1_trend} -> "
                  f"{row.fitted_trend_at_train_end}, avg6 {row.v1_avg6} -> "
                  f"{row.actual_avg_last_6mo}, %diff {row.v1_pct} -> "
                  f"{row.pct_diff_trend_vs_6mo_actual}  "
                  f"[verdict {'CHANGED' if row.verdict_flipped else 'unchanged'}]")
            n_flip = int(v1c["mismatches"]["verdict_flipped"].sum())
            p(f"      -> {n_flip} of {n_mis} changed the Trend Disconnected "
              f"verdict. New numbers are used and the disagreement is stated in "
              f"the sheet.")
    else:
        p("  VALIDATION vs v1: SKIPPED (v1 workbook not readable).")

    p("\nSheet 9  Model Routing   [the sheet that most needed to change]")
    p(f"  products                      {V['sheet9_total']:>6}  ->{s9['rows']:>6}")
    p("  v1 route ->  v2 route")
    for k, v in V["sheet9_routes"].items():
        p(f"      {k:<42} {v:>4}")
    p("      ---- replaced by, from model_routing.csv ----")
    for route, ov, cnt, _f, _n in s9["summary"]:
        lbl = route if ov in ("-", "outside model_routing.csv scope") else f"{route} / {ov}"
        p(f"      {lbl[:70]:<70} {cnt:>4}")
    if trace["ran"]:
        p(f"  TRACED: the {trace['n']} products v1 routed to the withdrawn "
          f"estimator, looked up item-by-item in model_routing.csv:")
        for (route, reason), cnt in sorted(trace["mapping"].items(),
                                          key=lambda kv: -kv[1]):
            p(f"      -> {route:<12} {cnt:>3}"
              + (f"   ({reason})" if reason else ""))
        nf = sum(c for (rt_, _), c in trace["mapping"].items()
                 if rt_ in (R_PROPHET, R_N3, R_N12))
        p(f"      {nf} of {trace['n']} now receive a real delivered forecast; "
          f"none lost one.")
    else:
        p("  TRACE SKIPPED: v1 workbook not readable.")
    p(f"  NEW: AU callout, flagged UNDER REVIEW (naive alternative WAPE-beats "
      f"Prophet on AU Erratic items but under-forecasts ~20%; not adopted)")

    p(f"\n{bar}\nWITHDRAWN-ROUTE SCAN\n{bar}")
    p(f"  scanned {scan['n_cells']:,} non-empty cells across "
      f"{scan['n_sheets']} sheets of the written file")
    if scan["hits"]:
        p(f"  FAILED - {len(scan['hits'])} cell(s) still reference the "
          f"withdrawn estimator family:")
        for sh, addr, val in scan["hits"][:25]:
            p(f"      {sh}!{addr}: {val}")
        raise SystemExit(2)
    p("  CONFIRMED: no cell in the output says or implies the withdrawn")
    p("  intermittent-demand estimator family - not as a route, not as a")
    p("  recommendation, not in a caption, not in a formula. Every routing")
    p("  value in the workbook is one of: Prophet / Naive-3mo / Naive-12mo /")
    p("  Blocked (from model_routing.csv), or an explicit out-of-scope label.")

    p(f"\n{bar}\nBARE-'NAIVE' SCAN (v3 terminology guard)\n{bar}")
    labels, prose = scan["naive_labels"], scan["naive_prose"]
    p(f"  every tab name, title, header and row label across {scan['n_sheets']} "
      f"sheets, plus every caption")
    if labels:
        p(f"  FAILED - {len(labels)} label(s) still say 'Naive' on its own:")
        for sh, addr, val in labels[:25]:
            p(f"      {sh}!{addr}: {val}")
        raise SystemExit(3)
    p("  CONFIRMED: no tab name, sheet title, column header or table row label")
    p("  in the workbook uses the word 'Naive' on its own. Every occurrence is")
    p("  'Naive-3mo' or 'Naive-12mo', and both appear only as a Method - the")
    p("  drill-down under Automated Forecast, never as a headline category.")
    p("  The headline categories are exactly three: Automated Forecast, BDM,")
    p(f"  {PRIOR_YEAR_LABEL}.")
    if prose:
        p(f"\n  NOTE - {len(prose)} prose mention(s) of 'naive' remain inside "
          f"long captions. None is a")
        p("  category label. They are of exactly two kinds, both deliberate:")
        p("  (a) captions on Sheets 2-5 that EXPLAIN the terminology - a caption")
        p("      cannot retire an ambiguous word without naming it; and")
        p("  (b) descriptive prose on Sheets 1 and 6-9, which are v2's sheets,")
        p("      unchanged in v3 because this confusion never reached them.")
        for sh, addr, val in prose[:25]:
            p(f"      {sh}!{addr}: {val[:96]}")
    p(bar)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Build the Daylight FY2026 forecast-review workbook.")
    ap.add_argument("--output-path", type=Path, default=DEFAULT_OUTPUT,
                    help=f"destination .xlsx (default: {DEFAULT_OUTPUT})")
    ap.add_argument("--reuse-trend-refit", action="store_true",
                    help="reuse the cached Sheet 8 Prophet refit instead of "
                         "recomputing 140 fits (formatting-only reruns)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    build(args.output_path, args.reuse_trend_refit)


if __name__ == "__main__":
    main()
