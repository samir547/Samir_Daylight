"""
ETS / SARIMA discovery experiment — do they close ground on BDM? (read-only)

WHAT THIS IS
────────────
`analysis/erratic_naive_experiment.py` asked whether arithmetic beats Prophet on
the Erratic population, and answered on rolling-origin folds inside the training
window. `analysis/croston_experiment.py` asked the same of Lumpy/Intermittent.
Both compared Prophet against models with NO fitted trend at all.

This file asks the other half of that question: is there a FITTED competitor —
one with an explicit trend or an explicit short seasonal period — that Prophet's
yearly-Fourier-plus-piecewise-linear shape is leaving on the table? Two
candidates, on two populations chosen for two different reasons:

    ETS         — `statsmodels.tsa.holtwinters.ExponentialSmoothing`, additive
                  trend, no seasonal term. Two variants, damped and linear,
                  scored side by side rather than picked between (see
                  ETS_VARIANTS).
    SARIMA      — `statsmodels.tsa.statespace.sarimax.SARIMAX` at a SHORT
                  seasonal period, not 12. See SARIMA_SEASONAL_PERIOD.

    Shortlist A — short-cycle candidates, from `item_features.csv`'s lag-2/lag-3
                  autocorrelation ladder. Tested with ETS AND SARIMA: the
                  question is whether a short cycle Prophet cannot see is worth
                  a model that can.
    Shortlist B — trend-divergence candidates, recent six months departing from
                  the long run. Tested with ETS ONLY: the question is trend, and
                  a seasonal term is not what would answer it.

NOTHING IS ADOPTED. `preprocessing/model_routing.py`, `config.py` and the fitting
path are untouched, and no existing output CSV is rewritten. This is discovery
for the demo.

WHY tbats IS NOT THE SEASONAL CANDIDATE
───────────────────────────────────────
The brief allowed SARIMA or TBATS. `tbats` is not installed and `statsmodels`
0.14.6 already is — and was added to `requirements.txt` this week as a DIRECT
dependency for `preprocessing/feature_extraction.py`'s ACF ladder. Using SARIMAX
adds no install, and it puts the seasonal period under an explicit constant this
file can state and defend rather than inside TBATS' automatic period search,
which would choose its own period per item and make "which period won" an
unanswerable question. `requirements.txt` is NOT edited by this file either.

═══════════════════════════════════════════════════════════════════════════════
THE FRAMING PROBLEM, STATED BEFORE ANY NUMBER
═══════════════════════════════════════════════════════════════════════════════
The brief asks for every candidate scored on IDENTICAL folds against BOTH the
incumbent AND BDM. Those two are not the same scoring exercise and cannot be
made into one:

  * The CV folds are rolling origins INSIDE the training window (<= TRAIN_END,
    2026-01). That is what makes them uncontaminated by the held-out window.
  * BDM's manual sheet is a CALENDAR-2026 artifact. `config.BENCHMARK_START..END`
    is 2026-02..2026-07 — precisely the held-out TEST window, and by
    construction disjoint from every CV fold.

There is no month on which a CV fold and a BDM forecast both exist. So this file
runs TWO tracks and never averages across them:

  TRACK 1 (CV)   — rolling-origin folds inside the training window. Every
                   candidate on identical folds, by the same mechanism
                   `erratic_naive_experiment.variant_cv_frames()` uses: fold
                   membership comes from Prophet's own `cross_validation`
                   output and every other candidate's yhat is laid against those
                   exact (cutoff, ds, y) rows. WAPE primary, MAPE secondary.
                   BDM CANNOT APPEAR HERE and is not reported as if it could.

  TRACK 2 (BDM)  — the held-out window, 2026-02..2026-07. Each candidate is fit
                   ONCE on history <= TRAIN_END and forecast six months forward;
                   BDM's own numbers are read from `benchmark_comparison.csv`.
                   All candidates and BDM are scored on the SAME item-months.
                   This is the only track in which "closes ground against BDM"
                   means anything, and it rests on ONE six-month window per item
                   — a single fold. Track 1 is the one with fold structure;
                   track 2 is the one with BDM. Neither substitutes for the
                   other and the summary says so per item.

WHICH DIRECTORY THIS READS AND WRITES
─────────────────────────────────────
`config.OUTPUT_DIR` (i.e. `output/`), NOT `output_phase1/`, and that is a
resolved question rather than a default:

  * `output_phase1/` is the PRE-ROLL vintage — its forecast file is
    `forecast_2026-07_2026-12.csv` and it has no `item_features.csv` at all.
  * `output/` is the POST-ROLL vintage — `forecast_2026-08_2027-01.csv`,
    matching `config.FORECAST_START/END` as they stand in the working tree
    (TRAIN_END 2026-01, TEST 2026-02..2026-07), and it is the ONLY directory
    holding `item_features.csv`, which is where both shortlists come from.

Selecting a population from `output/item_features.csv` and scoring it against
`output_phase1/`'s forecasts would mix the two vintages — the same mistake that
had to be unpicked earlier this week. `--input-dir` still exists so the question
can be re-asked of another directory, but the default is `output/` deliberately.

A VINTAGE MISMATCH THAT REMAINS, AND IS NOT A BUG
─────────────────────────────────────────────────
`item_features.csv` is ANCHOR-anchored: its grid runs to 2026-07, the latest
complete month, which is INSIDE the held-out test window. The CV folds train to
2026-01. So the shortlists are nominated using months that track 1's models
never see and that track 2 is scored on.

That is acceptable for what a shortlist IS — `feature_extraction.py`'s own
docstring calls a high divergence "a nomination, not a finding" — but it is NOT
acceptable to then read a track-2 win as evidence the feature predicted it. The
feature had already seen the answer. Track 2 wins are reported as measurements
of the candidate models, never as validation of the selection rule. Track 1,
whose folds end before the feature window opens, is the only place the selection
rule is tested at arm's length.

THE SHRINKAGE / BIAS CHECK IS NOT OPTIONAL
──────────────────────────────────────────
WAPE is bounded above at 100% for a forecast of zero and unbounded for an
over-forecast, so a candidate can "win" by predicting small. That is a fact about
the metric, not about skill, and it is what qualified the AU naive result.
`shrinkage_table()` here is `erratic_naive_experiment.shrinkage_table()`'s
question asked of this candidate set, with its constants IMPORTED rather than
restated so the two files' columns mean the same thing.

ETS has a second failure mode a naive model cannot have, and it is checked
separately: a fitted linear trend extrapolated six months can go NEGATIVE, and
production clips at zero. A clipped-to-zero ETS forecast scores exactly 100%
WAPE and looks like a shrunk forecast; `pct_folds_clipped` separates "the model
predicted nothing" from "the model predicted less than nothing".

Run:  python -m analysis.ets_sarima_experiment --shortlist-only
      python -m analysis.ets_sarima_experiment --sample 3
      python -m analysis.ets_sarima_experiment
      python -m analysis.ets_sarima_experiment --blend-only
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prophet import Prophet  # noqa: E402
from prophet.diagnostics import cross_validation  # noqa: E402
from statsmodels.tsa.holtwinters import ExponentialSmoothing  # noqa: E402
from statsmodels.tsa.statespace.sarimax import SARIMAX  # noqa: E402

import config  # noqa: E402
import main as pipeline  # noqa: E402  — reused for load_all()'s CSV-cache logic
from analysis.changepoint_experiment import (  # noqa: E402  — reused, not re-derived
    CV_HORIZON,
    CV_HORIZON_MONTHS,
    CV_PERIOD_MONTHS,
    MAX_CUTOFFS,
    MIN_CV_TRAIN_MONTHS,
    _fold_scores,
    make_cutoffs,
)
from analysis.croston_experiment import monthly_grid  # noqa: E402  — zero-filled grid
from analysis.demand_classification_report import build_scope_input  # noqa: E402
from analysis.erratic_naive_experiment import (  # noqa: E402  — reused, not re-derived
    MIN_FOLDS_FOR_VERDICT,
    NEAR_ZERO_LEVEL,
    NEAR_ZERO_RATE_FRACTION,
    SHRINKAGE_MATERIAL,
    SHRINKAGE_SEVERE,
    TIE_TOLERANCE_PP,
    naive_level,
)
from models.prophet_model import TRAIN_END_TS, TRAIN_START_TS  # noqa: E402
from preprocessing import model_routing as mr  # noqa: E402
from preprocessing import scope  # noqa: E402

logging.getLogger("cmdstanpy").setLevel(logging.ERROR)
logging.getLogger("prophet").setLevel(logging.ERROR)


# ── Candidate names ───────────────────────────────────────────────────────────

#: Prophet's label is `model_routing.PROPHET` and the naive labels are
#: `config.NAIVE_WINDOW_MONTHS`' keys, so a row here and a row in
#: `model_routing.csv` are talking about the same thing. Nothing is invented.
CAND_PROPHET = mr.PROPHET
NAIVE_VARIANTS = list(config.NAIVE_WINDOW_MONTHS)

CAND_ETS_DAMPED = "ETS-damped"
CAND_ETS_LINEAR = "ETS-linear"
ETS_VARIANTS = [CAND_ETS_DAMPED, CAND_ETS_LINEAR]
CAND_SARIMA = "SARIMA"

#: The incumbent set — what the pipeline already ships and what any new
#: candidate has to beat before the question of BDM even arises.
INCUMBENTS = [CAND_PROPHET] + NAIVE_VARIANTS

#: Shortlist A gets the seasonal candidate, Shortlist B does not. Both get ETS.
#: Stated as two lists rather than one flag because the asymmetry is the design:
#: B was selected on a TREND statistic and a seasonal term is not the thing that
#: would answer a trend question.
CANDIDATES_A = INCUMBENTS + ETS_VARIANTS + [CAND_SARIMA]
CANDIDATES_B = INCUMBENTS + ETS_VARIANTS
ALL_CANDIDATES = INCUMBENTS + ETS_VARIANTS + [CAND_SARIMA]

#: Track 2's extra column. Not a model this file fits — read from
#: `benchmark_comparison.csv`, which is the same BDM number `main()` reports.
BDM = "BDM"


# ── Experiment design constants (judgment calls, all stated in the summary) ────

#: SARIMA's seasonal period. THREE, and not 12.
#:
#: The brief's instruction was "a short seasonal period (not forced to 12)", and
#: three is the shortest period that spans BOTH lags Shortlist A's rule tests.
#: The rule requires structure at lag 2 AND lag 3 simultaneously; a period-2
#: model can only express the first and a period-12 model expresses neither.
#: Fixing it rather than searching per item is deliberate: a per-item search
#: over {2, 3, 6} would pick the period that fits best on the same data the
#: candidate is then scored on, and "SARIMA won" would be unreadable against
#: "the period search won". One period, stated, for every Shortlist A item.
SARIMA_SEASONAL_PERIOD = 3

#: SARIMA orders. One AR term at lag 1, one at the seasonal lag, a constant, no
#: differencing.
#:
#: No differencing because these are monthly demand grids with real zeros in
#: them: a first difference of a series that drops to zero and back manufactures
#: a swing the model then has to explain, and `d=1` on a 30-month grid spends a
#: degree of freedom this population cannot afford. The parameter count is the
#: binding constraint — the smallest Shortlist A grid is in the thirties and a
#: fold trains on less than that — so this is the smallest spec that can express
#: "last month matters and the month `SARIMA_SEASONAL_PERIOD` back matters",
#: which is exactly the hypothesis the shortlist was built to test. It is not a
#: tuned model and is not presented as one.
SARIMA_ORDER = (1, 0, 0)
SARIMA_SEASONAL_ORDER = (1, 0, 0, SARIMA_SEASONAL_PERIOD)

#: Both ETS variants are scored and reported; NEITHER is selected between.
#:
#: Damped is the robust default and linear is the one that can actually keep
#: extrapolating a divergence, which is the whole hypothesis behind Shortlist B.
#: Running both and reporting the better one as "ETS" would be a two-shot at the
#: same target scored as one — the multiple-comparison problem the seasonality
#: experiment's `additive_mode` result is a standing reminder of. So they carry
#: separate names in every table, and the summary counts a shortlist item as an
#: ETS win only when the SAME variant wins, which is stated per item.
ETS_TREND = "add"

# ── Shortlist A: short-cycle selection rule ───────────────────────────────────

#: Floor on |acf| at lags 2 and 3. 0.25.
#:
#: Read as a white-noise significance band: the standard +-1.96/sqrt(n) band is
#: 0.245 at n = 64 months, so 0.25 is approximately "significant for a series of
#: about five years". It is a FLOOR and not the whole test — ACF_BAND_Z below
#: applies each item's OWN band on top of it, so a 103-month series is not
#: waved through on a 0.25 that its own length would call significant while a
#: 30-month series is held to the same absolute number.
ACF_MIN = 0.25

#: The per-item white-noise band, 1.96/sqrt(n_months_grid). Applied IN ADDITION
#: to ACF_MIN, never instead of it. This is the constant that answers the
#: UN1050 lesson in the general case: `feature_extraction.py`'s docstring warns
#: that ranking short grids by acf2 "filled the top of the list with 13-14 month
#: grids whose acf2 is a handful of lagged pairs", and a 14-month grid's band is
#: 0.524 — so such an item now has to clear 0.524, not 0.25.
ACF_BAND_Z = 1.96

#: "Comparable magnitude": the smaller of |acf2|, |acf3| over the larger.
#:
#: 0.5 — i.e. neither lag may be less than half the other. This is what stops a
#: single strong lag from carrying a pair that the rule is supposed to require
#: BOTH halves of, which is the precise failure UN1050 demonstrated at lag 1:
#: acf1 -0.374 with acf2 +0.016 is a damping flip, not a cycle, and no rule that
#: can be satisfied by one lag alone would have caught that.
ACF_COMPARABLE_RATIO = 0.5

#: Minimum grid length for a shortlisted item, in months. 24 = the same bar
#: `config.MIN_TRAIN_MONTHS` sets before the pipeline will fit a series at all,
#: and the same bar `MIN_CV_TRAIN_MONTHS` sets before a CV fold exists. An item
#: below it could be selected but could never be scored, which would put a name
#: on the shortlist and a blank row in every results table.
MIN_GRID_MONTHS = config.MIN_TRAIN_MONTHS

#: The alternating arm's threshold: acf1 <= -ALT_ACF_MIN and acf2 >= +ALT_ACF_MIN.
#:
#: This arm exists because a PERSISTENT period-2 cycle has acf2 and acf3 with
#: OPPOSITE signs (+, -), so it cannot clear the same-sign rule above no matter
#: how strong it is — and a period-2 cycle is a short cycle. Same per-item band,
#: and `acf2/|acf1| >= ACF_COMPARABLE_RATIO` so the flip has to SURVIVE to lag 2
#: rather than damp there. UN1050 fails it on exactly that clause, which is the
#: arm's own regression test.
ALT_ACF_MIN = 0.20

# ── Shortlist B: trend-divergence selection rule ──────────────────────────────

#: Percentile cut on |trend_divergence_pp_per_month|. The 90th.
#:
#: The COLUMN is `feature_extraction.py`'s own instruction — it says in terms
#: that `trend_divergence_pp_per_month` "is the column to SORT on" and that
#: `trend_divergence_pct` must be read per item and never ranked on, because its
#: denominator varies by a factor of ~250 across this population. Absolute
#: value, because a divergence downward is as much a trend divergence as one
#: upward and ETS is being asked about both.
#:
#: The 90th and not the 95th because the 95th leaves too few items to survive
#: the raw-data confirmation below — which removes most of what the percentile
#: nominates — and not the 80th because that reaches down to ~20pp/month, where
#: the population is dense and the cut stops meaning anything.
DIVERGENCE_PERCENTILE = 90

#: How many trailing months the raw-data confirmation reads. Three.
#:
#: `trend_divergence_pp_per_month` fits a line to exactly six months, and
#: `feature_extraction.py`'s own docstring says "a single outlying month inside
#: a 6-point window moves it hard (cross-check spikiness)". Three is the
#: shortest window in which "moved in the same direction" is a statement about
#: more than one step.
#:
#: The test itself: EVERY one of the last CONFIRM_MONTHS months must sit on the
#: divergence's own side of the mean of the CONFIRM_MONTHS months before them.
#: The two weaker tests were both written and both rejected, and the rejection
#: is worth recording:
#:   * "mean of last 3 vs mean of prior 3" passes every candidate. It is almost
#:     implied by the metric being large and separates nothing.
#:   * "the last two month-over-month steps share the divergence's sign" passes
#:     D31875, whose trailing six months are 21, 27, 31, 19, 50, 381. A single
#:     381 at the anchor is the definition of the blip this test exists to
#:     catch, and a monotone-steps test waves it through because 19 -> 50 -> 381
#:     is monotone.
#: Requiring all three months on the same side of the prior level is what
#: actually separates a level shift from a spike. It is a veto on the ranking,
#: not a second ranking: it has no opinion on magnitude, and it cannot tell a
#: genuine shift from one that merely started three months ago.
CONFIRM_MONTHS = 3


# ── Shortlist A ───────────────────────────────────────────────────────────────

def _band(n_months: pd.Series) -> pd.Series:
    """Per-item white-noise band for the ACF, ACF_BAND_Z / sqrt(n)."""
    return ACF_BAND_Z / np.sqrt(n_months.astype(float))


def shortlist_a(feat: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """
    Short-cycle candidates: sustained structure at lags 2 AND 3, or a period-2
    flip that survives to lag 2.

    Returns (selected, funnel). The funnel is a subtraction, reported before
    anything is fitted, the same way `erratic_naive_experiment.load_population()`
    reports its own.

    ARM 1 (`same_sign`) — acf2 and acf3 share a sign, both clear
    max(ACF_MIN, own band), and neither is less than ACF_COMPARABLE_RATIO of the
    other. This is the arm the brief specified.

    ARM 2 (`alternating`) — acf1 <= -ALT_ACF_MIN, acf2 >= +ALT_ACF_MIN, both
    clear their own band, and acf2 is at least ACF_COMPARABLE_RATIO of |acf1|.
    Added because arm 1 cannot express a period-2 cycle at all: a real one has
    acf2 > 0 and acf3 < 0, which arm 1's same-sign clause rejects by
    construction. Without arm 2 the shortlist would silently be "short cycles
    except the shortest one".
    """
    f = feat.copy()
    funnel: dict[str, int] = {"items in item_features.csv": len(f)}

    f = f[f["acf2"].notna() & f["acf3"].notna() & f["acf1"].notna()]
    funnel["acf1/acf2/acf3 all present"] = len(f)

    f = f[f["n_months_grid"] >= MIN_GRID_MONTHS]
    funnel[f"n_months_grid >= {MIN_GRID_MONTHS} (MIN_TRAIN_MONTHS)"] = len(f)

    band = _band(f["n_months_grid"])
    floor = np.maximum(ACF_MIN, band)
    a2, a3, a1 = f["acf2"].abs(), f["acf3"].abs(), f["acf1"]

    same_sign = (
        (np.sign(f["acf2"]) == np.sign(f["acf3"]))
        & (a2 >= floor) & (a3 >= floor)
        & ((np.minimum(a2, a3) / np.maximum(a2, a3)) >= ACF_COMPARABLE_RATIO)
    )
    alternating = (
        (a1 <= -ALT_ACF_MIN) & (f["acf2"] >= ALT_ACF_MIN)
        & (a1.abs() >= np.maximum(ALT_ACF_MIN, band))
        & (a2 >= np.maximum(ALT_ACF_MIN, band))
        & ((a2 / a1.abs()) >= ACF_COMPARABLE_RATIO)
    )

    funnel["ARM 1 same-sign acf2/acf3"] = int(same_sign.sum())
    funnel["ARM 2 alternating (period-2)"] = int(alternating.sum())

    sel = f[same_sign | alternating].copy()
    ss, al = same_sign[sel.index], alternating[sel.index]
    sel["arm"] = np.where(ss & al, "both", np.where(ss, "same_sign", "alternating"))
    sel["acf_band"] = _band(sel["n_months_grid"])
    sel["shortlist"] = "A"
    sel["selection_basis"] = "rule"
    funnel["Shortlist A (either arm)"] = len(sel)
    return sel.sort_values("item_code").reset_index(drop=True), funnel


# ── Shortlist B ───────────────────────────────────────────────────────────────

def anchor_grid(raw: pd.DataFrame, anchor: pd.Timestamp) -> dict[str, pd.Series]:
    """
    Every item's zero-filled monthly grid from its first non-zero month to
    `anchor`, straight from `raw_data.csv`.

    Rebuilt here rather than imported from `feature_extraction.py` because the
    confirmation has to be an INDEPENDENT read of the raw rows — the whole point
    is to check whether the feature's number reflects the data, and computing it
    through the same code that produced the feature would only check that the
    code is deterministic. The construction (sum to item x month, first non-zero
    month to anchor, zeros filled) is reproduced from that module's docstring,
    and `check_grid_reproduction()` asserts the lengths agree with
    `n_months_grid` so a silent divergence in construction cannot pass unnoticed.

    `scope.filter_amazon_channel()` IS applied, and it is the one step that
    cannot be re-derived from the docstring alone. Six items disagreed with
    `n_months_grid` without it — DL10101 at 7 months against 24, UL10200 at 2
    against 23 — because their earliest sales are Amazon rows that
    `feature_extraction.py` drops before the grid starts, so an unfiltered
    rebuild begins the grid years earlier and reads a different series. None of
    the six is on either shortlist, so the shortlists were unaffected either
    way; the check is what said so, which is why it runs before the selection
    rather than after it.
    """
    raw, _excluded = scope.filter_amazon_channel(raw)
    m = raw.copy()
    m["item_code"] = m["item_code"].astype(str).str.strip()
    m["ds"] = pd.to_datetime(m["year_month"].astype(str) + "-01")
    m = m[m["ds"] <= anchor]
    m = m.groupby(["item_code", "ds"], as_index=False)["monthly_qty"].sum()

    out: dict[str, pd.Series] = {}
    for item, g in m.groupby("item_code"):
        nz = g[g["monthly_qty"] > 0]
        if nz.empty:
            continue
        idx = pd.date_range(nz["ds"].min(), anchor, freq="MS")
        out[str(item)] = (g.set_index("ds")["monthly_qty"]
                          .reindex(idx, fill_value=0.0).astype(float))
    return out


def resolve_feature_anchor(feat: pd.DataFrame) -> pd.Timestamp:
    """
    The month `item_features.csv` was built through, read off the file itself.

    `last_sale_month`'s maximum is the newest month any item sold in within the
    feature grid, which IS the anchor whenever at least one item sold in it —
    true for a 199-item book. Read from the file rather than from `config` on
    purpose: `feature_extraction.py` resolves its anchor from data on every run
    precisely so it does not depend on the date constants while the window
    decision is open, and re-deriving it from `config.TEST_END` here would
    re-introduce the dependency that module went out of its way to avoid.
    """
    return pd.Timestamp(str(feat["last_sale_month"].dropna().max()) + "-01")


def check_grid_reproduction(feat: pd.DataFrame, grids: dict[str, pd.Series]
                            ) -> pd.DataFrame:
    """
    Do the grids rebuilt here have the lengths `item_features.csv` recorded?

    A mismatch means the two constructions have drifted and the confirmation
    below would be reading a different series from the one the metric was
    computed on. Returns the mismatching rows — empty is the pass — and the
    caller prints the count either way rather than asserting silently.
    """
    rows = []
    for r in feat.itertuples():
        g = grids.get(str(r.item_code))
        mine = 0 if g is None else len(g)
        if pd.notna(r.n_months_grid) and int(r.n_months_grid) != mine:
            rows.append({"item_code": r.item_code,
                         "n_months_grid_feature": int(r.n_months_grid),
                         "n_months_grid_rebuilt": mine})
    return pd.DataFrame(rows)


def confirm_divergence(item: str, divergence: float,
                       grids: dict[str, pd.Series]) -> dict:
    """
    Does the raw series actually show a sustained shift, or one loud month?

    Returns a dict of evidence — never a bare boolean — because the shortlist
    table has to show WHY an item was vetoed, not just that it was. `confirmed`
    is the test described at CONFIRM_MONTHS; `blip_note` names the shape when it
    fails, so `0 0 0 0 0 10` reads as a blip in the CSV without anyone
    re-opening the raw data.
    """
    empty = {"confirmed": False, "confirm_reason": "", "last_6": "",
             "prev_mean": np.nan, "recent_mean": np.nan,
             "n_months_on_side": 0, "blip_note": ""}
    g = grids.get(item)
    if g is None or len(g) < 2 * CONFIRM_MONTHS:
        return {**empty,
                "confirm_reason": f"grid shorter than {2 * CONFIRM_MONTHS} months"}

    sign = np.sign(divergence)
    recent = g.iloc[-CONFIRM_MONTHS:].to_numpy(dtype=float)
    prev = g.iloc[-2 * CONFIRM_MONTHS:-CONFIRM_MONTHS].to_numpy(dtype=float)
    prev_mean = float(prev.mean())
    on_side = int((np.sign(recent - prev_mean) == sign).sum())
    confirmed = bool(on_side == CONFIRM_MONTHS and sign != 0)

    note = ""
    if not confirmed:
        # Name the failure shape. One month carrying the whole recent move is
        # the case the veto exists for and is worth saying out loud in the CSV.
        moves = np.abs(recent - prev_mean)
        share = float(moves.max() / moves.sum()) if moves.sum() > 0 else 0.0
        note = (f"single month carries {share:.0%} of the move"
                if share >= 0.6 else
                f"only {on_side}/{CONFIRM_MONTHS} months on the divergence side")

    return {
        "confirmed": confirmed,
        "confirm_reason": (f"all {CONFIRM_MONTHS} recent months on the "
                           f"divergence side of the prior {CONFIRM_MONTHS}"
                           if confirmed else f"{on_side}/{CONFIRM_MONTHS} on side"),
        "last_6": " ".join(str(int(round(v)))
                           for v in g.iloc[-2 * CONFIRM_MONTHS:]),
        "prev_mean": prev_mean,
        "recent_mean": float(recent.mean()),
        "n_months_on_side": on_side,
        "blip_note": note,
    }


def shortlist_b(feat: pd.DataFrame, grids: dict[str, pd.Series]
                ) -> tuple[pd.DataFrame, dict[str, int], float]:
    """
    Trend-divergence candidates: top-decile |pp/month|, confirmed against raw.

    Returns (frame, funnel, cutoff). Every item that clears the percentile is
    carried through the confirmation WITH its evidence attached and stays in the
    returned frame either way — confirmed rows as candidates, vetoed rows as
    evidence — so the funnel can report how many the raw data threw out, which on
    this run is the more interesting of the two numbers.
    """
    f = feat.copy()
    funnel: dict[str, int] = {"items in item_features.csv": len(f)}

    f = f[f["trend_divergence_pp_per_month"].notna()]
    funnel["trend_divergence_pp_per_month present"] = len(f)

    cutoff = float(np.percentile(f["trend_divergence_pp_per_month"].abs(),
                                 DIVERGENCE_PERCENTILE))
    f = f[f["trend_divergence_pp_per_month"].abs() >= cutoff]
    funnel[f"|pp/month| >= p{DIVERGENCE_PERCENTILE} ({cutoff:.2f})"] = len(f)

    f = f[f["n_months_grid"] >= MIN_GRID_MONTHS]
    funnel[f"n_months_grid >= {MIN_GRID_MONTHS} (MIN_TRAIN_MONTHS)"] = len(f)

    ev = pd.DataFrame([confirm_divergence(str(r.item_code),
                                          r.trend_divergence_pp_per_month, grids)
                       for r in f.itertuples()], index=f.index)
    f = pd.concat([f, ev], axis=1)
    f["shortlist"] = "B"
    f["arm"] = "divergence"
    f["selection_basis"] = np.where(f["confirmed"], "rule", "vetoed_by_raw_data")
    funnel[f"confirmed by raw_data ({CONFIRM_MONTHS}-month rule)"] = \
        int(f["confirmed"].sum())
    return f.sort_values("item_code").reset_index(drop=True), funnel, cutoff


# ── Exclusions and named items ────────────────────────────────────────────────

#: The seven-item severe-bias cluster, excluded from this experiment by name.
#:
#: `erratic_naive_experiment.py` produced a validated naive-routing answer for
#: this population. Re-testing them here would mean any result on them could be
#: read either as "the new model helps" or as "the known bias explains it", and
#: nothing in this file could separate the two. They are excluded from BOTH
#: shortlists and from the shortlist track's aggregates. They are NOT excluded
#: from the book-wide blend track, which scores every item the pipeline shipped
#: and would be a different population if it dropped seven.
BIAS_CLUSTER = ["E35090", "E35231", "E35070", "E35152", "EN1540", "UN1350",
                "UN91171"]

#: E35152 is in the bias cluster AND was named for Shortlist A on its acf1 of
#: -0.72. It is carried in Shortlist A as a NAMED item, flagged
#: `named_bias_cluster`, and it is excluded from Shortlist A's aggregate
#: medians. The brief's reason for including it — the cycle question — is a
#: per-item question and is answered per item; folding it into an average would
#: re-import the bias population the exclusion above exists to keep out.
NAMED_A = ["E35152"]

#: Named for Shortlist B by the brief, to be confirmed rather than assumed.
NAMED_B = ["E35500", "E25400", "D36401", "EN1230", "D31875"]

#: Bases that get fitted. A row kept only as veto evidence is not one of them.
TESTED_BASES = ["rule", "named_not_rule_selected", "named_also_rule_selected",
                "named_bias_cluster"]


def build_shortlists(feat: pd.DataFrame, grids: dict[str, pd.Series]
                     ) -> tuple[pd.DataFrame, dict, dict, float]:
    """
    Both shortlists, plus every named item, in one frame.

    `selection_basis` is the column that keeps the two apart: 'rule' for an item
    the stated rule selected, 'named_*' for one the brief asked for by name and
    which therefore has to be reported WHETHER OR NOT it clears, and
    'vetoed_by_raw_data' for a row kept purely as evidence. Aggregates in this
    file use 'rule' rows only; per-item verdicts use every fitted row.
    """
    a, funnel_a = shortlist_a(feat)
    b_all, funnel_b, cutoff = shortlist_b(feat, grids)

    a = a[~a["item_code"].isin(BIAS_CLUSTER)]
    funnel_a["minus severe-bias cluster"] = len(a)

    b_rule = b_all[b_all["selection_basis"] == "rule"]
    b_rule = b_rule[~b_rule["item_code"].isin(BIAS_CLUSTER)]
    funnel_b["minus severe-bias cluster"] = len(b_rule)

    frames = [a, b_rule, b_all[b_all["selection_basis"] == "vetoed_by_raw_data"]]

    # PER SHORTLIST, not pooled. E35500 and EN1230 are rule-selected into
    # Shortlist A on their ACF ladder and were ALSO named for Shortlist B; a
    # pooled membership set would label them "also rule selected" for B, which
    # is false — E35500's divergence is -8.6 pp/month against a 32.29 cut and
    # EN1230's is a single-month blip the raw-data veto threw out. Whether a
    # named item clears is a question about the rule it was named for.
    rule_selected = {"A": set(a["item_code"]), "B": set(b_rule["item_code"])}

    for names, lst in ((NAMED_A, "A"), (NAMED_B, "B")):
        for item in names:
            row = feat[feat["item_code"] == item]
            if row.empty:
                continue
            already = item in rule_selected[lst]
            if already:
                continue  # the rule row already carries it; one row per (item, list)
            r = row.copy()
            r["shortlist"] = lst
            r["arm"] = "named"
            r["acf_band"] = _band(r["n_months_grid"])
            r["selection_basis"] = ("named_bias_cluster" if item in BIAS_CLUSTER
                                    else "named_not_rule_selected")
            td = r["trend_divergence_pp_per_month"].iloc[0]
            for k, v in confirm_divergence(
                    item, float(td) if pd.notna(td) else 0.0, grids).items():
                r[k] = v
            frames.append(r)

    out = pd.concat(frames, ignore_index=True)
    # `named` marks the row REGARDLESS of how it got there, so a named item the
    # rule also selected keeps its single rule row and is still reportable as a
    # named item. Without this the report could only see names the rule missed.
    named_pairs = {("A", i) for i in NAMED_A} | {("B", i) for i in NAMED_B}
    out["named"] = [(s, i) in named_pairs
                    for s, i in zip(out["shortlist"], out["item_code"])]
    return out, funnel_a, funnel_b, cutoff


def tested_population(shortlists: pd.DataFrame) -> pd.DataFrame:
    """
    One row per item to fit, with the candidate set it gets.

    An item on BOTH shortlists is fitted ONCE and gets Shortlist A's candidate
    set (the superset), with `shortlist` recording both. Fitting it twice would
    put two different fold sets under one name and make the per-item verdict
    depend on which table it was read from.
    """
    df = shortlists[shortlists["selection_basis"].isin(TESTED_BASES)].copy()
    join = lambda s: "+".join(sorted(set(s.astype(str))))  # noqa: E731
    agg = df.groupby("item_code").agg(shortlist=("shortlist", join),
                                      arm=("arm", join),
                                      selection_basis=("selection_basis", join))
    out = df.drop_duplicates("item_code").set_index("item_code")
    out[["shortlist", "arm", "selection_basis"]] = agg
    out = out.reset_index()
    out["candidates"] = np.where(out["shortlist"].str.contains("A"),
                                 ",".join(CANDIDATES_A), ",".join(CANDIDATES_B))
    cols = ["item_code", "product_family", "category", "is_pooled", "region",
            "family_key", "shortlist", "arm", "selection_basis", "candidates",
            "acf1", "acf2", "acf3", "acf6", "acf_band",
            "trend_divergence_pp_per_month", "slope_recent_pct_per_month",
            "slope_full_pct_per_month", "spikiness", "n_months_grid",
            "n_nonzero_grid", "confirmed", "confirm_reason", "last_6",
            "blip_note"]
    return (out[[c for c in cols if c in out.columns]]
            .sort_values(["shortlist", "item_code"]).reset_index(drop=True))


# ── The candidate forecasters ─────────────────────────────────────────────────

def _as_monthly(grid: pd.DataFrame) -> pd.Series:
    """
    The zero-filled grid as a Series on a `freq='MS'` DatetimeIndex.

    `statsmodels` needs a regular frequency; Prophet does not. `train` as it
    reaches this module is `prepare()`'s output, which emits NO zero-quantity
    rows, so handing it straight to `ExponentialSmoothing` would silently
    compress a series with gaps into a shorter contiguous one and shift every
    lag the shortlist was selected on. `monthly_grid()` is imported from
    `croston_experiment` for exactly this reason and is the same zero-inclusive
    convention `naive_level()` reads.
    """
    s = grid.set_index("ds")["y"].astype(float)
    s.index = pd.DatetimeIndex(s.index, freq="MS")
    return s


def _ets_forecast(hist: pd.Series, steps: int, damped: bool) -> np.ndarray:
    """
    ETS point forecast, `steps` months ahead. Raises on a series it cannot fit.

    Additive trend, NO seasonal term, `initialization_method='estimated'`.

    No seasonal term because a monthly ETS seasonal period is 12 and a
    12-period seasonal ETS needs two full cycles before it is identified at all;
    the folds here train on as few as 24 months, where that spends every degree
    of freedom the series has. Prophet's yearly Fourier term already occupies
    that hypothesis in this comparison, and SARIMA occupies the short-period
    one. ETS is in this experiment for its TREND, which is what both shortlists
    were selected on and what `damped` toggles.

    Multiplicative trend is not offered: these grids contain real zeros and a
    multiplicative form is undefined on them.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = ExponentialSmoothing(
            hist, trend=ETS_TREND, damped_trend=damped, seasonal=None,
            initialization_method="estimated")
        return np.asarray(model.fit(optimized=True).forecast(steps), dtype=float)


def _sarima_forecast(hist: pd.Series, steps: int) -> np.ndarray:
    """
    SARIMA point forecast at SARIMA_SEASONAL_PERIOD. Raises on a failed fit.

    `enforce_stationarity` and `enforce_invertibility` are both off. That is not
    a shortcut — on a demand grid with zeros the unconstrained optimiser
    regularly lands just outside the unit circle and the enforced version raises
    rather than returning the fit it already has. Off, the fit is returned and
    is scored on its forecasts like every other candidate; an unstable AR
    extrapolated six months will show up as a terrible WAPE, which is the
    correct outcome and a visible one, rather than as a skipped item.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = SARIMAX(hist, order=SARIMA_ORDER,
                        seasonal_order=SARIMA_SEASONAL_ORDER, trend="c",
                        enforce_stationarity=False, enforce_invertibility=False)
        return np.asarray(model.fit(disp=False).forecast(steps), dtype=float)


def _statsmodels_yhat(candidate: str, grid: pd.DataFrame,
                      cutoff: pd.Timestamp, target_ds: pd.Series) -> np.ndarray:
    """
    One candidate's yhat for the months in `target_ds`, from history <= cutoff.

    The forecast is produced for EVERY month from cutoff+1 to the latest target
    month and then reindexed onto `target_ds`, rather than for len(target_ds)
    steps. The two differ whenever the fold's observed months are not
    contiguous — which is common here, because `target_ds` comes from Prophet's
    cv frame and Prophet only sees months that sold. Forecasting `len(target_ds)`
    steps and zipping them would align a three-step-ahead forecast against a
    six-month-ahead actual on exactly the sparse series this experiment cares
    about.
    """
    hist = _as_monthly(grid[grid["ds"] <= cutoff])
    target = pd.DatetimeIndex(pd.to_datetime(target_ds).unique()).sort_values()
    horizon = pd.date_range(cutoff + pd.DateOffset(months=1), target.max(),
                            freq="MS")
    if len(horizon) == 0:
        return np.full(len(target_ds), np.nan)

    if candidate in ETS_VARIANTS:
        path = _ets_forecast(hist, len(horizon),
                             damped=(candidate == CAND_ETS_DAMPED))
    elif candidate == CAND_SARIMA:
        path = _sarima_forecast(hist, len(horizon))
    else:
        raise ValueError(f"not a statsmodels candidate: {candidate}")

    lookup = pd.Series(path, index=horizon)
    return lookup.reindex(pd.to_datetime(target_ds)).to_numpy(dtype=float)


# ── TRACK 1: cross-validation on identical folds ──────────────────────────────

def candidate_cv_frames(train: pd.DataFrame, grid: pd.DataFrame,
                        cutoffs: list[pd.Timestamp], candidates: list[str]
                        ) -> tuple[dict[str, pd.DataFrame], dict[str, str], int]:
    """
    One `cross_validation`-shaped frame per candidate, on IDENTICAL folds.

    Same mechanism as `erratic_naive_experiment.variant_cv_frames()`, extended
    to the fitted candidates: Prophet is cross-validated at `cutoffs`, and every
    other candidate's frame is Prophet's own (cutoff, ds, y) rows with `yhat`
    overwritten. There is one set of rows and all candidates share it, so they
    cannot end up scored on different months, different fold counts or different
    actuals. If `cross_validation` drops a cutoff it is dropped for everyone.

    Returns (frames, failures, n_fits). `failures` names any candidate whose fit
    raised, per item — it is reported, never swallowed, and a candidate that
    failed on an item is absent from that item's tables rather than present with
    a fabricated number.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = Prophet(**config.PROPHET_PARAMS)
        model.fit(train)
        cv = cross_validation(model, horizon=CV_HORIZON, cutoffs=cutoffs,
                              disable_tqdm=True)

    cv = cv[["ds", "cutoff", "y", "yhat"]].copy()
    frames: dict[str, pd.DataFrame] = {CAND_PROPHET: cv}
    failures: dict[str, str] = {}
    n_fits = 1 + len(cutoffs)

    for name in NAIVE_VARIANTS:
        months = config.NAIVE_WINDOW_MONTHS[name]
        levels = {c: naive_level(grid, c, months) for c in cv["cutoff"].unique()}
        frame = cv.copy()
        frame["yhat"] = frame["cutoff"].map(levels).astype(float)
        frames[name] = frame

    for name in [c for c in candidates if c in ETS_VARIANTS + [CAND_SARIMA]]:
        frame = cv.copy()
        yhat = np.full(len(frame), np.nan)
        try:
            for cutoff, idx in frame.groupby("cutoff").groups.items():
                sub = frame.loc[idx]
                yhat[frame.index.get_indexer(idx)] = _statsmodels_yhat(
                    name, grid, pd.Timestamp(cutoff), sub["ds"])
                n_fits += 1
        except Exception as exc:
            failures[name] = f"{type(exc).__name__}: {exc}"
            continue
        if not np.isfinite(yhat).any():
            failures[name] = "fit produced no finite forecast"
            continue
        frame["yhat"] = yhat
        frames[name] = frame

    return frames, failures, n_fits


def fold_rows(frames: dict[str, pd.DataFrame], train: pd.DataFrame,
              item: str) -> list[dict]:
    """
    Every (cutoff x candidate) fold for one item, scored.

    `erratic_naive_experiment.fold_rows()`'s arithmetic, with `n_clipped` added:
    `yhat` is clipped at zero because production clips it, and a fitted trend —
    unlike a naive level — can genuinely go negative, so the count of clipped
    months is the thing that separates "predicted nothing" from "predicted less
    than nothing" in the shrinkage reading. Rows whose yhat is NaN (a candidate
    that could not forecast that month) are dropped from that candidate's fold
    only, never from anyone else's.
    """
    rows: list[dict] = []
    for name, cv in frames.items():
        cv = cv.dropna(subset=["yhat"]).copy()
        if cv.empty:
            continue
        n_clip = int((cv["yhat"] < 0).sum())
        cv["yhat"] = cv["yhat"].clip(lower=0)
        cv["abs_err"] = (cv["y"] - cv["yhat"]).abs()

        for cutoff, fold in cv.groupby("cutoff"):
            denom = float(fold["y"].abs().sum())
            nz = fold[fold["y"] != 0]
            rows.append({
                "item_code": item,
                "candidate": name,
                "cutoff": pd.Timestamp(cutoff),
                "n_train_months_at_cutoff": int((train["ds"] <= cutoff).sum()),
                "yhat_level_or_forecast": float(fold["yhat"].mean()),
                "actual_sum": denom,
                "wape": (float(fold["abs_err"].sum() / denom * 100)
                         if denom > 0 else np.nan),
                "mape": (float((nz["abs_err"] / nz["y"].abs()).mean() * 100)
                         if len(nz) else np.nan),
                "fold_note": "zero_demand_fold" if denom == 0 else "",
                "n_horizon_months": int(len(fold)),
                "abs_err_sum": float(fold["abs_err"].sum()),
                "n_clipped_months": int((fold["yhat"] <= 0).sum()) if n_clip else 0,
            })
    return rows


def item_scores(folds: pd.DataFrame, frames_by_item: dict[str, dict]
                ) -> pd.DataFrame:
    """
    Collapse folds to one row per (item, candidate).

    `wape` and `mape` come from `changepoint_experiment._fold_scores()` called
    on the candidate's own cv frame — the imported scorer is the authority for
    the headline numbers, not a convention this file re-implements. The
    per-fold decomposition is checked against it and any disagreement is carried
    in `scorer_delta` rather than silently reconciled.

    `wape_pooled` sums every fold-month's error over every fold-month's actual,
    which KEEPS zero-demand folds that the per-fold mean must drop for want of a
    denominator. Both are carried for the reason `croston_experiment` carries
    both: the per-fold mean lets one quiet half-year weigh as much as a busy
    one, the pooled figure lets a single large fold carry the item, and where
    they disagree it is the folds that disagree.
    """
    out = []
    for (item, cand), grp in folds.groupby(["item_code", "candidate"]):
        scored = grp.dropna(subset=["wape"])
        pooled_den = float(grp["actual_sum"].sum())
        pooled_err = float(grp["abs_err_sum"].sum(skipna=True))

        ref = frames_by_item[item][cand].dropna(subset=["yhat"])
        ref_mape, ref_wape, _ = _fold_scores(ref)
        own_wape = float(scored["wape"].mean()) if len(scored) else np.nan
        delta = (abs(own_wape - ref_wape)
                 if pd.notna(own_wape) and pd.notna(ref_wape) else np.nan)

        out.append({
            "item_code": item,
            "candidate": cand,
            "n_folds": int(len(grp)),
            "n_folds_scored": int(len(scored)),
            "n_folds_zero_demand": int((grp["actual_sum"] == 0).sum()),
            "wape": ref_wape,
            "wape_pooled": (pooled_err / pooled_den * 100) if pooled_den > 0 else np.nan,
            "mape": ref_mape,
            "mean_yhat_level": float(grp["yhat_level_or_forecast"].mean(skipna=True)),
            "n_clipped_months": int(grp["n_clipped_months"].sum()),
            "scorer_delta": delta,
        })
    return pd.DataFrame(out)


def shrinkage_table(folds: pd.DataFrame, label: str = "",
                    candidates: list[str] | None = None) -> pd.DataFrame:
    """
    Per candidate: how big is its forecast against the demand that arrived.

    The mechanism check on the headline ranking, and the reason nothing here is
    reported as a win without looking. `bias_ratio` is the mean forecast level
    over the mean actual monthly rate on the same folds, so 1.0 is an unbiased
    rate estimate, 0.5 a forecast of half the demand, 2.0 twice.

    The three fingerprint columns are `erratic_naive_experiment`'s, with its
    constants imported so the columns mean the same thing in both files.
    `pct_folds_clipped` is the fourth and is new here for the reason
    `fold_rows()` gives: a naive level cannot be negative, a fitted trend can,
    and the two produce the same 100% WAPE by two different mechanisms.
    """
    f = folds.copy()
    f["actual_rate"] = f["actual_sum"] / f["n_horizon_months"]
    rows = []
    # `candidates` is the shortlist's OWN candidate set. Shortlist B was never
    # given SARIMA, but E35500 is on both lists and is fitted once with
    # Shortlist A's superset, so its SARIMA folds would otherwise appear in
    # Shortlist B's shrinkage table as a one-item row wearing the same heading
    # as the five real ones.
    allowed = candidates or ALL_CANDIDATES
    for name in [c for c in allowed if c in set(f["candidate"])]:
        sub = f[f["candidate"] == name]
        scored = sub.dropna(subset=["wape"])
        rate = float(sub["actual_rate"].mean())
        level = float(sub["yhat_level_or_forecast"].mean(skipna=True))
        rel = sub[sub["actual_rate"] > 0]
        rows.append({
            "population": label,
            "candidate": name,
            "n_folds": int(len(sub)),
            "mean_yhat_level": level,
            "mean_actual_rate": rate,
            "bias_ratio": (level / rate) if rate else np.nan,
            "pct_folds_near_zero": float(
                (sub["yhat_level_or_forecast"] < NEAR_ZERO_LEVEL).mean() * 100),
            "pct_folds_below_10pct_rate": (float(
                (rel["yhat_level_or_forecast"]
                 < NEAR_ZERO_RATE_FRACTION * rel["actual_rate"]).mean() * 100)
                if len(rel) else np.nan),
            "pct_folds_wape_100": (
                float(((scored["wape"] - 100).abs() < 0.01).mean() * 100)
                if len(scored) else np.nan),
            "pct_folds_clipped": float((sub["n_clipped_months"] > 0).mean() * 100),
        })
    return pd.DataFrame(rows)


def shrinkage_verdict(bias_ratio: float) -> str:
    """
    The written reading of one bias ratio, in `erratic_naive_experiment`'s bands.

    Three bands and not a single cut, for that file's reason: a single threshold
    between two indistinguishable ratios prints a warning against one and an
    all-clear against the other, which is the threshold talking rather than the
    data. The middle band deliberately declines to separate skill from metric
    asymmetry instead of pretending it can.
    """
    if pd.isna(bias_ratio):
        return "no bias ratio"
    if bias_ratio < SHRINKAGE_SEVERE:
        return (f"SEVERE SHRINKAGE (bias ratio {bias_ratio:.2f}) — this is a "
                f"forecast of a fraction of the demand, and WAPE rewards it")
    if bias_ratio < SHRINKAGE_MATERIAL:
        return (f"material under-forecast (bias ratio {bias_ratio:.2f}) — skill "
                f"and metric asymmetry cannot be separated at this level")
    if bias_ratio > 1 / SHRINKAGE_MATERIAL:
        return (f"over-forecast (bias ratio {bias_ratio:.2f}) — not a shrinkage "
                f"win; WAPE is unbounded above and penalised this")
    return (f"level is in the right neighbourhood (bias ratio {bias_ratio:.2f}) "
            f"— not winning by predicting small")


# ── TRACK 2: the held-out window, where BDM lives ─────────────────────────────

#: The production forecast as the pipeline actually shipped it, read from
#: `test_validation.csv` rather than refitted.
#:
#: A separate candidate from `CAND_PROPHET` on purpose. Track 2 refits Prophet
#: for EVERY tested item so that every item has a Prophet column — but the
#: pipeline did not necessarily ship Prophet for every item: `model_routing`
#: sends some of them to a naive window and blocks others entirely. "Beat the
#: incumbent" has to mean beating what was actually delivered, so the delivered
#: forecast is carried under its own name and the refit is carried under
#: Prophet's. Where routing chose Prophet the two are the same model; where it
#: did not, the gap between the columns is the routing decision, not a model
#: difference, and the summary says which.
CAND_ROUTED = "Routed(production)"


def load_bdm_window(out_dir: Path) -> pd.DataFrame:
    """
    Actuals and BDM's manual forecast per item-month over the benchmark window.

    Straight from `benchmark_comparison.csv`, which is `evaluation/benchmark.py`'s
    own output — the BDM figure here is therefore literally the one `main()`
    reports, already summed across bdm_codes to item grain, and not a second
    derivation of it that happens to agree today.

    `actual` is NaN wherever `prepare()` emitted no row for that item-month,
    which means the item did not sell: a real zero, not a missing observation.
    It is filled with 0.0, because for WAPE a zero-demand month is a month every
    candidate is accountable for over-forecasting — dropping it would quietly
    excuse exactly the months a fitted trend is most likely to get wrong.
    """
    bc = pd.read_csv(out_dir / "benchmark_comparison.csv")
    bc["item_code"] = bc["item_code"].astype(str).str.strip()
    bc["ds"] = pd.to_datetime(bc["month"].astype(str) + "-01")
    bc["actual"] = bc["actual"].fillna(0.0).astype(float)
    return bc[["item_code", "ds", "month", "model", "actual", "model_forecast",
               "bdm_forecast"]]


def bdm_window_rows(item: str, grid: pd.DataFrame, train: pd.DataFrame,
                    window: pd.DataFrame, candidates: list[str]
                    ) -> tuple[list[dict], dict[str, str]]:
    """
    Every candidate's forecast for one item over the held-out window, plus BDM's.

    One fit per candidate on history <= TRAIN_END, forecast across the window,
    then scored on the months BDM also covers. Restricting to BDM-covered months
    is what makes the comparison a comparison — a candidate scored on six months
    against a BDM scored on five would differ by the month, not the method.

    Returns (rows, failures). Rows are per item-month and carry every
    candidate's yhat plus `bdm_forecast`, so the CSV can be re-scored by anyone
    who disagrees with the aggregation.
    """
    win = window[window["bdm_forecast"].notna()].sort_values("ds")
    failures: dict[str, str] = {}
    if win.empty:
        return [], {"__window__": "no BDM-covered month"}

    target = win["ds"]
    preds: dict[str, np.ndarray] = {}

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            model = Prophet(**config.PROPHET_PARAMS)
            model.fit(train)
            fc = model.predict(pd.DataFrame({"ds": target}))
            preds[CAND_PROPHET] = fc["yhat"].to_numpy(dtype=float)
        except Exception as exc:
            failures[CAND_PROPHET] = f"{type(exc).__name__}: {exc}"

    for name in NAIVE_VARIANTS:
        level = naive_level(grid, TRAIN_END_TS, config.NAIVE_WINDOW_MONTHS[name])
        preds[name] = np.full(len(win), float(level))

    for name in [c for c in candidates if c in ETS_VARIANTS + [CAND_SARIMA]]:
        try:
            preds[name] = _statsmodels_yhat(name, grid, TRAIN_END_TS, target)
        except Exception as exc:
            failures[name] = f"{type(exc).__name__}: {exc}"

    # What the pipeline actually shipped for these months, where it shipped one.
    if win["model_forecast"].notna().any():
        preds[CAND_ROUTED] = win["model_forecast"].to_numpy(dtype=float)

    rows = []
    for name, yhat in preds.items():
        for ds, month, actual, bdm, y in zip(
                win["ds"], win["month"], win["actual"], win["bdm_forecast"], yhat):
            rows.append({
                "item_code": item, "candidate": name, "ds": ds, "month": month,
                "actual": float(actual),
                "yhat_raw": float(y) if pd.notna(y) else np.nan,
                "yhat": float(max(y, 0.0)) if pd.notna(y) else np.nan,
                "bdm_forecast": float(bdm),
                "routed_model": str(win["model"].iloc[0]),
            })
    # BDM as a candidate row of its own, on the identical months.
    for ds, month, actual, bdm in zip(win["ds"], win["month"], win["actual"],
                                      win["bdm_forecast"]):
        rows.append({
            "item_code": item, "candidate": BDM, "ds": ds, "month": month,
            "actual": float(actual), "yhat_raw": float(bdm),
            "yhat": float(max(bdm, 0.0)), "bdm_forecast": float(bdm),
            "routed_model": str(win["model"].iloc[0]),
        })
    return rows, failures


def window_scores(rows: pd.DataFrame) -> pd.DataFrame:
    """
    One WAPE per (item, candidate) over the held-out window.

    A single six-month fold, and named as one: `n_months` is on every row so a
    reader can see that this is one window and not a fold structure. MAPE is
    carried as the secondary metric and skips zero-actual months, which on this
    window is a real exclusion rather than a formality — see `load_bdm_window()`.
    """
    out = []
    for (item, cand), g in rows.dropna(subset=["yhat"]).groupby(
            ["item_code", "candidate"]):
        err = (g["actual"] - g["yhat"]).abs()
        den = float(g["actual"].abs().sum())
        nz = g[g["actual"] != 0]
        out.append({
            "item_code": item,
            "candidate": cand,
            "n_months": int(len(g)),
            "actual_sum": den,
            "yhat_sum": float(g["yhat"].sum()),
            "wape": float(err.sum() / den * 100) if den > 0 else np.nan,
            "mape": (float(((nz["actual"] - nz["yhat"]).abs()
                            / nz["actual"].abs()).mean() * 100)
                     if len(nz) else np.nan),
            "bias_ratio": (float(g["yhat"].mean() / g["actual"].mean())
                           if g["actual"].mean() else np.nan),
            "n_clipped_months": int((g["yhat_raw"] < 0).sum()),
        })
    return pd.DataFrame(out)


def window_summary(scores: pd.DataFrame, population: pd.DataFrame) -> pd.DataFrame:
    """
    Per item: who won the held-out window, and did anything beat BDM.

    `best_ex_bdm` is the best MODEL, BDM excluded, because "which model would we
    have wanted" and "did it beat the manual forecast" are two questions and
    collapsing them loses the second. `gap_to_bdm_pp` is the signed distance
    from that best model to BDM in WAPE points — NEGATIVE means the model is
    better — and `closes_ground` is the comparison the brief actually asked for:
    whether the new candidate narrows the gap the INCUMBENT has to BDM, which is
    a different and much lower bar than beating BDM outright, and is reported as
    such.
    """
    meta = population.set_index("item_code")
    rows = []
    for item, g in scores.groupby("item_code"):
        by = g.set_index("candidate")["wape"]
        bdm_w = float(by.get(BDM, np.nan))
        models = g[g["candidate"] != BDM].dropna(subset=["wape"]).sort_values("wape")
        inc = g[g["candidate"].isin(INCUMBENTS + [CAND_ROUTED])].dropna(
            subset=["wape"]).sort_values("wape")

        best = str(models["candidate"].iloc[0]) if len(models) else ""
        best_w = float(models["wape"].iloc[0]) if len(models) else np.nan
        best_inc = str(inc["candidate"].iloc[0]) if len(inc) else ""
        best_inc_w = float(inc["wape"].iloc[0]) if len(inc) else np.nan
        new = g[g["candidate"].isin(ETS_VARIANTS + [CAND_SARIMA])].dropna(
            subset=["wape"]).sort_values("wape")
        best_new = str(new["candidate"].iloc[0]) if len(new) else ""
        best_new_w = float(new["wape"].iloc[0]) if len(new) else np.nan

        m = meta.loc[item] if item in meta.index else {}
        # An item can sell nothing at all across the whole window. WAPE has no
        # denominator there and every candidate scores NaN — that is a fact
        # about the item, not a failure of any model, and it is labelled rather
        # than left as an unexplained row of blanks.
        actual_sum = float(g["actual_sum"].max())
        rows.append({
            "item_code": item,
            "shortlist": m.get("shortlist", ""),
            "selection_basis": m.get("selection_basis", ""),
            "region": m.get("region", ""),
            "category": m.get("category", ""),
            "n_months": int(g["n_months"].max()),
            "actual_sum": actual_sum,
            "window_note": ("zero demand in the whole window — WAPE undefined "
                            "for every candidate" if actual_sum == 0 else ""),
            **{f"wape_{c}": float(by.get(c, np.nan)) for c in
               ALL_CANDIDATES + [CAND_ROUTED, BDM]},
            "best_ex_bdm": best,
            "best_ex_bdm_wape": best_w,
            "best_incumbent": best_inc,
            "best_incumbent_wape": best_inc_w,
            "best_new_model": best_new,
            "best_new_model_wape": best_new_w,
            "bdm_wape": bdm_w,
            "gap_to_bdm_pp": best_w - bdm_w,
            "incumbent_gap_to_bdm_pp": best_inc_w - bdm_w,
            "beats_bdm": bool(pd.notna(best_w) and pd.notna(bdm_w)
                              and best_w < bdm_w - TIE_TOLERANCE_PP),
            "new_beats_incumbent": bool(
                pd.notna(best_new_w) and pd.notna(best_inc_w)
                and best_new_w < best_inc_w - TIE_TOLERANCE_PP),
            "closes_ground": bool(
                pd.notna(best_new_w) and pd.notna(best_inc_w) and pd.notna(bdm_w)
                and abs(best_new_w - bdm_w) < abs(best_inc_w - bdm_w) - TIE_TOLERANCE_PP
                and best_new_w < best_inc_w),
        })
    return pd.DataFrame(rows).sort_values(["shortlist", "item_code"]).reset_index(
        drop=True)


# ── Per-item CV verdict ───────────────────────────────────────────────────────

def build_summary(scores: pd.DataFrame, population: pd.DataFrame) -> pd.DataFrame:
    """
    One row per item: every candidate's CV WAPE, which won, and by how much.

    `erratic_naive_experiment.build_summary()`'s shape and its two guards, both
    imported rather than re-chosen: a verdict is withheld below
    MIN_FOLDS_FOR_VERDICT scored folds, and a margin under TIE_TOLERANCE_PP is
    reported as a tie. The numbers still print in both cases — items below the
    bar are excluded from the verdict column, not from the evidence.

    `best_is_new` answers the experiment's question directly: did ETS or SARIMA
    win, as opposed to one of the three things the pipeline already has.
    """
    meta = population.set_index("item_code")
    rows = []
    for item, grp in scores.groupby("item_code"):
        by = grp.set_index("candidate")
        ranked = grp.dropna(subset=["wape"]).sort_values("wape")
        m = meta.loc[item] if item in meta.index else {}

        best = runner = ""
        best_w = runner_w = margin = np.nan
        if len(ranked):
            best, best_w = str(ranked["candidate"].iloc[0]), float(ranked["wape"].iloc[0])
        if len(ranked) > 1:
            runner = str(ranked["candidate"].iloc[1])
            runner_w = float(ranked["wape"].iloc[1])
            margin = runner_w - best_w

        n_scored = int(by["n_folds_scored"].max()) if len(by) else 0
        if n_scored < MIN_FOLDS_FOR_VERDICT:
            verdict = f"no verdict (<{MIN_FOLDS_FOR_VERDICT} scored folds)"
        elif not len(ranked):
            verdict = "no verdict (nothing scorable)"
        elif pd.notna(margin) and margin < TIE_TOLERANCE_PP:
            verdict = f"tie ({best} / {runner}, <{TIE_TOLERANCE_PP:.0f}pp apart)"
        else:
            verdict = best

        inc = ranked[ranked["candidate"].isin(INCUMBENTS)]
        best_inc_w = float(inc["wape"].iloc[0]) if len(inc) else np.nan
        rows.append({
            "item_code": item,
            "shortlist": m.get("shortlist", ""),
            "arm": m.get("arm", ""),
            "selection_basis": m.get("selection_basis", ""),
            "category": m.get("category", ""),
            "region": m.get("region", ""),
            "n_months_grid": int(m.get("n_months_grid", 0) or 0),
            "n_folds": int(by["n_folds"].max()) if len(by) else 0,
            "n_folds_scored": n_scored,
            "n_folds_zero_demand": (int(by["n_folds_zero_demand"].max())
                                    if len(by) else 0),
            **{f"wape_{c}": float(by["wape"].get(c, np.nan)) for c in ALL_CANDIDATES},
            **{f"mape_{c}": float(by["mape"].get(c, np.nan)) for c in ALL_CANDIDATES},
            "best_candidate": best,
            "best_wape": best_w,
            "runner_up": runner,
            "runner_up_wape": runner_w,
            "margin_pp": margin,
            "best_incumbent_wape": best_inc_w,
            "best_is_new": best in ETS_VARIANTS + [CAND_SARIMA],
            "beats_all_incumbents": bool(
                best in ETS_VARIANTS + [CAND_SARIMA] and pd.notna(best_inc_w)
                and best_w < best_inc_w - TIE_TOLERANCE_PP),
            "verdict": verdict,
        })
    return pd.DataFrame(rows).sort_values(["shortlist", "item_code"]).reset_index(
        drop=True)


# ── Orchestration for the shortlist track ─────────────────────────────────────

def run_shortlists(population: pd.DataFrame, pooled_data: pd.DataFrame,
                   window_all: pd.DataFrame
                   ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame,
                              pd.DataFrame, dict, float, int]:
    """
    Score every candidate on every shortlisted item, both tracks.

    Returns (cv_folds, window_rows, skipped, failures, frames, elapsed, n_fits).
    An item that cannot produce a fold is recorded in `skipped` with its reason
    and never dropped quietly — a shortlist of 22 that silently became 14 would
    make every median below it a different statistic from the one the header
    claims. A candidate that fails on an item is recorded in `failures` for the
    same reason, per item and per candidate.
    """
    df = pooled_data.copy()
    df["item_code"] = df["item_code"].astype(str).str.strip()
    by_key = {str(k): g for k, g in df.groupby("item_code")}

    all_folds: list[dict] = []
    all_window: list[dict] = []
    skipped: list[dict] = []
    fails: list[dict] = []
    frames_by_item: dict[str, dict] = {}
    n_fits = 0
    t0 = time.perf_counter()

    for row in population.itertuples(index=False):
        item = str(row.item_code).strip()
        key = str(row.family_key).strip()
        cands = str(row.candidates).split(",")
        series = by_key.get(key)
        if series is None or series.empty:
            skipped.append({"item_code": item, "shortlist": row.shortlist,
                            "reason": "no rows in the prepared frame"})
            continue

        train = (series[(series["ds"] >= TRAIN_START_TS)
                        & (series["ds"] <= TRAIN_END_TS)]
                 [["ds", "y"]].dropna().sort_values("ds").reset_index(drop=True))
        if len(train) < MIN_CV_TRAIN_MONTHS:
            skipped.append({
                "item_code": item, "shortlist": row.shortlist,
                "reason": (f"{len(train)} observed training months, below "
                           f"MIN_CV_TRAIN_MONTHS ({MIN_CV_TRAIN_MONTHS})")})
            continue

        grid = monthly_grid(train, TRAIN_END_TS)
        cutoffs = make_cutoffs(train)
        if not cutoffs:
            skipped.append({
                "item_code": item, "shortlist": row.shortlist,
                "reason": (f"no valid CV fold: {len(train)} observed months, needs "
                           f"{MIN_CV_TRAIN_MONTHS} before a cutoff with demand in "
                           f"the {CV_HORIZON} after it")})
            continue

        try:
            frames, cand_fails, fits = candidate_cv_frames(
                train, grid, cutoffs, cands)
        except Exception as exc:
            skipped.append({"item_code": item, "shortlist": row.shortlist,
                            "reason": f"cross_validation failed: "
                                      f"{type(exc).__name__}: {exc}"})
            continue

        n_fits += fits
        frames_by_item[item] = frames
        all_folds.extend(fold_rows(frames, train, item))
        for cand, why in cand_fails.items():
            fails.append({"item_code": item, "track": "cv", "candidate": cand,
                          "reason": why})

        # Track 2, same item, same history bound.
        win = window_all[window_all["item_code"] == item] if len(window_all) else \
            pd.DataFrame()
        if len(win):
            wrows, wfails = bdm_window_rows(item, grid, train, win, cands)
            all_window.extend(wrows)
            n_fits += 1
            for cand, why in wfails.items():
                fails.append({"item_code": item, "track": "bdm_window",
                              "candidate": cand, "reason": why})
        else:
            fails.append({"item_code": item, "track": "bdm_window",
                          "candidate": "__all__",
                          "reason": "item absent from benchmark_comparison.csv"})

        print(f"  {item:<10} [{row.shortlist}] {len(cutoffs)} cutoff(s), "
              f"{len(train)} train months, {len(cands)} candidates")

    elapsed = time.perf_counter() - t0
    return (pd.DataFrame(all_folds), pd.DataFrame(all_window),
            pd.DataFrame(skipped), pd.DataFrame(fails), frames_by_item,
            elapsed, n_fits)


# ══════════════════════════════════════════════════════════════════════════════
# SEPARATE TRACK: the book-wide 50/50 blend
# ══════════════════════════════════════════════════════════════════════════════
"""
Reported separately from the shortlists, and separate for a reason: it is a
different population (the whole current book, not 22 nominated items), a
different question (does averaging two forecasts help, not does a new model
help), and it involves no fitting at all.

WHAT IS BLENDED, AND WHY IT IS NOT WHAT THE BRIEF FIRST DESCRIBED
─────────────────────────────────────────────────────────────────
The brief offered two branches: blend Prophet with Naive "for items where both
exist", else the routed model with a 12-month trailing mean. In this pipeline the
FIRST BRANCH IS EMPTY. `model_routing` is exclusive — every item is routed to
exactly one of Prophet / Naive-3mo / Naive-12mo, and `test_validation.csv` carries
exactly one forecast per item-month under a `model` column naming which. There is
no item for which both a Prophet and a Naive forecast were produced. So every
item takes the second branch, and the blend is uniformly

    0.5 * (routed model's forecast)  +  0.5 * (12-month trailing mean at TRAIN_END)

The trailing mean is `erratic_naive_experiment.naive_level()` at
`config.NAIVE_WINDOW_MONTHS['Naive-12mo']`, i.e. the same zero-inclusive
arithmetic `models/naive_model.py` ships, computed on history <= TRAIN_END so it
cannot see the window it is scored on.

THE IDENTITY CASE, WHICH IS NOT A RESULT
────────────────────────────────────────
For an item ALREADY routed to Naive-12mo, this blend averages a number with
itself and returns it unchanged. Those items are not evidence for or against
blending and are counted and excluded from the blend's own win/loss tallies —
reported under `blend_is_identity`. Leaving them in would dilute every aggregate
towards "no change" by construction and would make the blend look safer than the
evidence supports.

Items routed to Naive-3mo are a milder version of the same objection — the blend
there is a 3mo/12mo average, two arithmetic levels rather than a model and a
level — and are flagged `blend_of_two_naives` so the aggregate can be read with
and without them.

No fitting happens here: both inputs already exist post-window-roll.
"""

BLEND_WEIGHT = 0.5
CAND_BLEND = "Blend-50/50"
CAND_NAIVE12 = "Naive-12mo"


def blend_track(out_dir: Path, pooled_data: pd.DataFrame
                ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Score the 50/50 blend across the whole current book. Returns (rows, scores).

    `rows` is per item-month and carries every component, so the blend weight can
    be re-chosen by anyone reading the CSV without re-running anything.
    """
    tv = pd.read_csv(out_dir / "test_validation.csv")
    tv["item_code"] = tv["item_code"].astype(str).str.strip()
    tv["family_key"] = tv["family_key"].astype(str).str.strip()
    tv["ds"] = pd.to_datetime(tv["ds"].astype(str) + "-01")
    tv["actual"] = tv["actual"].fillna(0.0).astype(float)

    bdm = load_bdm_window(out_dir)[["item_code", "ds", "bdm_forecast"]]
    df = tv.merge(bdm, on=["item_code", "ds"], how="inner")
    df = df[df["bdm_forecast"].notna()]

    # The trailing mean, one level per item, from history <= TRAIN_END.
    pd_ = pooled_data.copy()
    pd_["item_code"] = pd_["item_code"].astype(str).str.strip()
    by_key = {str(k): g for k, g in pd_.groupby("item_code")}
    months = config.NAIVE_WINDOW_MONTHS[CAND_NAIVE12]

    levels: dict[str, float] = {}
    for item, key in df[["item_code", "family_key"]].drop_duplicates().itertuples(
            index=False):
        series = by_key.get(str(key))
        if series is None or series.empty:
            continue
        train = (series[(series["ds"] >= TRAIN_START_TS)
                        & (series["ds"] <= TRAIN_END_TS)][["ds", "y"]]
                 .dropna().sort_values("ds"))
        if train.empty:
            continue
        levels[str(item)] = naive_level(monthly_grid(train, TRAIN_END_TS),
                                        TRAIN_END_TS, months)

    # THE SPLIT RATIO IS NOT OPTIONAL HERE, and leaving it out was a real bug
    # this file caught with the identity check below rather than by reading the
    # code. `pooled_data` is at FAMILY grain: for a pooled successor family the
    # trailing mean computed on it is the FAMILY's monthly rate, while the
    # routed forecast in `test_validation.csv` has already been split back out
    # to the item by `split_ratio`. Averaging the two without the ratio blends
    # an item-level forecast with a family-level level — on U35309
    # (split_ratio 0.116) that turned a 36.0 WAPE into 420.5 and the identity
    # check below is what surfaced it. Singletons carry split_ratio 1.0, so this
    # multiplication is a no-op everywhere it should be one.
    df["split_ratio"] = pd.to_numeric(df["split_ratio"], errors="coerce").fillna(1.0)
    df["naive12_level"] = df["item_code"].map(levels) * df["split_ratio"]
    df = df[df["naive12_level"].notna()].copy()
    df["routed_yhat"] = df["yhat"].clip(lower=0).astype(float)
    df["blend_yhat"] = (BLEND_WEIGHT * df["routed_yhat"]
                        + (1 - BLEND_WEIGHT) * df["naive12_level"]).clip(lower=0)

    rows = []
    for name, col in ((CAND_ROUTED, "routed_yhat"),
                      (CAND_NAIVE12, "naive12_level"),
                      (CAND_BLEND, "blend_yhat"),
                      (BDM, "bdm_forecast")):
        r = df[["item_code", "ds", "actual", "model"]].copy()
        r["candidate"] = name
        r["yhat"] = df[col].clip(lower=0).astype(float)
        r["routed_model"] = df["model"]
        rows.append(r)
    rows = pd.concat(rows, ignore_index=True)
    rows["month"] = rows["ds"].dt.strftime("%Y-%m")

    scores = []
    for (item, cand), g in rows.groupby(["item_code", "candidate"]):
        den = float(g["actual"].abs().sum())
        err = (g["actual"] - g["yhat"]).abs()
        nz = g[g["actual"] != 0]
        scores.append({
            "item_code": item,
            "candidate": cand,
            "routed_model": str(g["routed_model"].iloc[0]),
            "n_months": int(len(g)),
            "actual_sum": den,
            "yhat_sum": float(g["yhat"].sum()),
            "wape": float(err.sum() / den * 100) if den > 0 else np.nan,
            "mape": (float(((nz["actual"] - nz["yhat"]).abs()
                            / nz["actual"].abs()).mean() * 100)
                     if len(nz) else np.nan),
            "bias_ratio": (float(g["yhat"].mean() / g["actual"].mean())
                           if g["actual"].mean() else np.nan),
        })
    return rows, pd.DataFrame(scores)


def identity_check(scores: pd.DataFrame) -> pd.DataFrame:
    """
    For an item already routed to Naive-12mo the blend MUST return the routed
    forecast unchanged. This is the check that says so, per item.

    It is a self-test on the reconstruction, not a result: the trailing mean
    here is recomputed from `pooled_data` while the routed forecast is read from
    `test_validation.csv`, so agreement means the two paths really do produce
    the same number and disagreement means one of them is wrong. It caught the
    missing `split_ratio` above. Residual disagreement of a fraction of a point
    is expected — `test_validation.csv` stores `yhat` rounded to two decimals
    and a small-volume item turns that rounding into a visible WAPE difference.
    """
    rows = []
    for item, g in scores.groupby("item_code"):
        if str(g["routed_model"].iloc[0]) != CAND_NAIVE12:
            continue
        by = g.set_index("candidate")["wape"]
        r, b = float(by.get(CAND_ROUTED, np.nan)), float(by.get(CAND_BLEND, np.nan))
        rows.append({"item_code": item, "wape_routed": r, "wape_blend": b,
                     "abs_diff_pp": abs(b - r)})
    return pd.DataFrame(rows).sort_values("abs_diff_pp", ascending=False)


def blend_summary(scores: pd.DataFrame) -> pd.DataFrame:
    """
    Per item: routed vs blend vs BDM, and whether the blend is an identity.

    `blend_is_identity` is the column that has to be read before any aggregate
    in this table: for an item already routed to Naive-12mo the blend cannot
    differ from the routed forecast, so its row is real but carries no
    information about blending.
    """
    rows = []
    for item, g in scores.groupby("item_code"):
        by = g.set_index("candidate")["wape"]
        routed_m = str(g["routed_model"].iloc[0])
        r, b, n, d = (float(by.get(CAND_ROUTED, np.nan)),
                      float(by.get(CAND_BLEND, np.nan)),
                      float(by.get(CAND_NAIVE12, np.nan)),
                      float(by.get(BDM, np.nan)))
        bias = g.set_index("candidate")["bias_ratio"]
        rows.append({
            "item_code": item,
            "routed_model": routed_m,
            "n_months": int(g["n_months"].max()),
            "wape_routed": r,
            "wape_naive12": n,
            "wape_blend": b,
            "wape_bdm": d,
            "blend_bias_ratio": float(bias.get(CAND_BLEND, np.nan)),
            "routed_bias_ratio": float(bias.get(CAND_ROUTED, np.nan)),
            "blend_minus_routed_pp": b - r,
            "blend_minus_bdm_pp": b - d,
            "routed_minus_bdm_pp": r - d,
            "blend_is_identity": routed_m == CAND_NAIVE12,
            "blend_of_two_naives": routed_m in NAIVE_VARIANTS,
            "blend_beats_routed": bool(pd.notna(b) and pd.notna(r)
                                       and b < r - TIE_TOLERANCE_PP),
            "blend_beats_bdm": bool(pd.notna(b) and pd.notna(d)
                                    and b < d - TIE_TOLERANCE_PP),
        })
    return pd.DataFrame(rows).sort_values("item_code").reset_index(drop=True)


# ── Reporting ─────────────────────────────────────────────────────────────────

SEP = "─" * 78


def _funnel(title: str, funnel: dict) -> None:
    print(f"\n{title}")
    for label, n in funnel.items():
        print(f"  {label:<52}: {n:>5}")


def print_selection(shortlists: pd.DataFrame, population: pd.DataFrame,
                    funnel_a: dict, funnel_b: dict, cutoff: float,
                    anchor: pd.Timestamp, mismatches: pd.DataFrame) -> None:
    """
    The selection rules, their exact thresholds, and the resulting lists —
    printed BEFORE anything is fitted, the way population selection has been
    reported all week.
    """
    print(f"\n{SEP}\nSELECTION — rules and thresholds, stated before any fit\n{SEP}")
    print(f"  item_features.csv anchor (from the file itself): "
          f"{anchor:%Y-%m}")
    print(f"  grid reproduction check vs n_months_grid: "
          f"{len(mismatches)} mismatch(es)")
    if len(mismatches):
        print(mismatches.head(10).to_string(index=False))

    print(f"\n  SHORTLIST A — short-cycle. An item qualifies on EITHER arm:")
    print(f"    ARM 1 (same-sign): sign(acf2) == sign(acf3)")
    print(f"        AND |acf2| >= max({ACF_MIN}, 1.96/sqrt(n_months_grid))")
    print(f"        AND |acf3| >= max({ACF_MIN}, 1.96/sqrt(n_months_grid))")
    print(f"        AND min(|acf2|,|acf3|) / max(|acf2|,|acf3|) >= "
          f"{ACF_COMPARABLE_RATIO}")
    print(f"    ARM 2 (alternating, period-2): acf1 <= -{ALT_ACF_MIN} "
          f"AND acf2 >= +{ALT_ACF_MIN}")
    print(f"        AND both >= max({ALT_ACF_MIN}, 1.96/sqrt(n_months_grid))")
    print(f"        AND |acf2|/|acf1| >= {ACF_COMPARABLE_RATIO}")
    print(f"    BOTH arms also require n_months_grid >= {MIN_GRID_MONTHS}.")
    print(f"    |acf1| ALONE IS NOT A CRITERION — that is the UN1050 lesson: "
          f"acf1 -0.374 with")
    print(f"    acf2 +0.016 is a damping flip, not a cycle, and arm 2's "
          f"|acf2|/|acf1| >= {ACF_COMPARABLE_RATIO} clause")
    print(f"    is what rejects it.")
    _funnel("  Shortlist A funnel:", funnel_a)

    a = shortlists[(shortlists["shortlist"] == "A")]
    cols = ["item_code", "category", "region", "arm", "selection_basis",
            "acf1", "acf2", "acf3", "acf_band", "n_months_grid"]
    print(f"\n  Shortlist A items ({len(a)}):")
    print(a[cols].round(3).to_string(index=False))

    print(f"\n  SHORTLIST B — trend divergence. An item qualifies when ALL hold:")
    print(f"    |trend_divergence_pp_per_month| >= p{DIVERGENCE_PERCENTILE} "
          f"of the non-null population = {cutoff:.2f} pp/month")
    print(f"    AND n_months_grid >= {MIN_GRID_MONTHS}")
    print(f"    AND raw_data.csv confirms it: each of the last {CONFIRM_MONTHS} "
          f"months sits on the")
    print(f"        divergence's own side of the mean of the "
          f"{CONFIRM_MONTHS} months before them.")
    print(f"    Ranked on pp/month and NOT on trend_divergence_pct — "
          f"feature_extraction.py's own")
    print(f"    instruction; that ratio's denominator varies ~250x across "
          f"this population.")
    _funnel("  Shortlist B funnel:", funnel_b)

    b = shortlists[shortlists["shortlist"] == "B"]
    bcols = ["item_code", "category", "region", "selection_basis",
             "trend_divergence_pp_per_month", "n_months_on_side", "last_6",
             "blip_note", "n_months_grid"]
    bcols = [c for c in bcols if c in b.columns]
    print(f"\n  Shortlist B candidates and vetoes ({len(b)}):")
    print(b[bcols].round(2).to_string(index=False))

    print(f"\n  NAMED ITEMS — asked for by name, confirmed rather than assumed.")
    print(f"  'Clears' is judged against the rule of the shortlist the item was "
          f"named FOR, never")
    print(f"  against the other one: E35500 and EN1230 are rule-selected into A "
          f"on their ACF")
    print(f"  ladder and that says nothing about whether they clear B's "
          f"divergence rule.")
    for lst, names in (("A", NAMED_A), ("B", NAMED_B)):
        for item in names:
            row = shortlists[(shortlists["shortlist"] == lst)
                             & (shortlists["item_code"] == item)]
            if row.empty:
                print(f"    {item:<9} [{lst}] not present in item_features.csv")
                continue
            r = row.iloc[0]
            clears = (r["selection_basis"] == "rule")
            if lst == "A":
                why = (f"acf2 {r['acf2']:+.3f} / acf3 {r['acf3']:+.3f}, own band "
                       f"{r['acf_band']:.3f}, grid {int(r['n_months_grid'])}mo")
            else:
                why = (f"{r['trend_divergence_pp_per_month']:+.2f} pp/month vs "
                       f"cut {cutoff:.2f}; raw {r['confirm_reason']}"
                       + (f"; {r['blip_note']}" if r["blip_note"] else ""))
            print(f"    {item:<9} [{lst}] "
                  f"{'CLEARS the rule' if clears else 'DOES NOT clear the rule'}"
                  f" — {why}")

    print(f"\n  EXCLUDED severe-bias cluster (validated naive fix already exists):")
    print(f"    {', '.join(BIAS_CLUSTER)}")
    print(f"\n  TO BE FITTED: {len(population)} item(s)")
    print(f"    Shortlist A gets: {', '.join(CANDIDATES_A)}")
    print(f"    Shortlist B gets: {', '.join(CANDIDATES_B)}")


def print_results(population: pd.DataFrame, skipped: pd.DataFrame,
                  failures: pd.DataFrame, folds: pd.DataFrame,
                  summary: pd.DataFrame, shrink: pd.DataFrame,
                  wsummary: pd.DataFrame, wscores: pd.DataFrame,
                  bsummary: pd.DataFrame, brows: pd.DataFrame,
                  icheck: pd.DataFrame) -> None:
    """The plain summary. No adoption language anywhere in it."""
    print(f"\n{SEP}\nTRACK 1 — CROSS-VALIDATION (folds inside the training "
          f"window, no BDM)\n{SEP}")
    print(f"  horizon / period : {CV_HORIZON} ({CV_HORIZON_MONTHS} monthly "
          f"points) / {CV_PERIOD_MONTHS} months, max {MAX_CUTOFFS} folds")
    print(f"  fold minimum     : {MIN_CV_TRAIN_MONTHS} observed months before "
          f"a cutoff")
    print(f"  items scored     : {folds['item_code'].nunique()} of "
          f"{len(population)}")
    if len(skipped):
        print(f"\n  Skipped ({len(skipped)}):")
        print(skipped.to_string(index=False))
    if len(failures):
        print(f"\n  Candidate fit failures ({len(failures)}):")
        print(failures.to_string(index=False))

    # Each shortlist's medians are taken over ITS OWN candidate set only.
    # E35500 is on both lists and is fitted once with Shortlist A's superset, so
    # it carries a SARIMA score; reporting that under Shortlist B would print a
    # "median" of one item for a candidate Shortlist B was never given.
    for lst, cands in (("A", CANDIDATES_A), ("B", CANDIDATES_B)):
        sub = summary[summary["shortlist"].str.contains(lst, na=False)]
        rule = sub[~sub["selection_basis"].str.contains("bias_cluster", na=False)]
        if rule.empty:
            continue
        print(f"\n  Shortlist {lst} — median CV WAPE by candidate "
              f"({len(rule)} rule/named items, {len(cands)} candidates):")
        for c in cands:
            col = f"wape_{c}"
            if col in rule and rule[col].notna().any():
                print(f"    {c:<12}: {rule[col].median():>8.2f}   "
                      f"(n={int(rule[col].notna().sum())})")

    print(f"\n  Per-item CV verdicts:")
    scols = ["item_code", "shortlist", "arm", "n_folds_scored"] + \
            [f"wape_{c}" for c in ALL_CANDIDATES] + \
            ["best_candidate", "margin_pp", "best_is_new", "verdict"]
    scols = [c for c in scols if c in summary.columns]
    print(summary[scols].round(2).to_string(index=False))

    print(f"\n{SEP}\nSHRINKAGE / BIAS CHECK — is any win a win by predicting "
          f"small?\n{SEP}")
    print(shrink.round(3).to_string(index=False))
    print()
    for r in shrink.itertuples():
        print(f"  [{r.population}] {r.candidate:<12}: {shrinkage_verdict(r.bias_ratio)}")
        if pd.notna(r.pct_folds_clipped) and r.pct_folds_clipped > 0:
            print(f"    {r.pct_folds_clipped:.0f}% of its folds contain a month "
                  f"the fitted trend drove NEGATIVE and production would clip "
                  f"to zero.")

    print(f"\n{SEP}\nTRACK 2 — HELD-OUT WINDOW {config.BENCHMARK_START}.."
          f"{config.BENCHMARK_END}, against BDM\n{SEP}")
    print(f"  ONE six-month window per item, not a fold structure. Every "
          f"candidate and BDM")
    print(f"  are scored on the identical item-months (those BDM covers).")
    if wsummary.empty:
        print("  Nothing scorable.")
    else:
        print(f"\n  Median WAPE across {len(wsummary)} item(s):")
        for c in ALL_CANDIDATES + [CAND_ROUTED, BDM]:
            col = f"wape_{c}"
            if col in wsummary and wsummary[col].notna().any():
                print(f"    {c:<18}: {wsummary[col].median():>8.2f}   "
                      f"(n={int(wsummary[col].notna().sum())})")
        wcols = ["item_code", "shortlist"] + \
                [f"wape_{c}" for c in ALL_CANDIDATES + [CAND_ROUTED, BDM]] + \
                ["best_ex_bdm", "gap_to_bdm_pp", "best_new_model",
                 "new_beats_incumbent", "closes_ground"]
        wcols = [c for c in wcols if c in wsummary.columns]
        print(f"\n  Per item:")
        print(wsummary[wcols].round(2).to_string(index=False))
        print(f"\n  Items where a NEW model beats the best incumbent by "
              f">{TIE_TOLERANCE_PP:.0f}pp : "
              f"{int(wsummary['new_beats_incumbent'].sum())} of {len(wsummary)}")
        print(f"  Items where a NEW model closes ground on BDM             : "
              f"{int(wsummary['closes_ground'].sum())} of {len(wsummary)}")
        print(f"  Items where the best model beats BDM outright            : "
              f"{int(wsummary['beats_bdm'].sum())} of {len(wsummary)}")

    if len(wsummary) and wsummary["window_note"].astype(bool).any():
        print(f"\n  Items with no demand at all in the window (WAPE undefined "
              f"for everything):")
        for r in wsummary[wsummary["window_note"].astype(bool)].itertuples():
            print(f"    {r.item_code:<9} — {r.window_note}")

    print_blend(bsummary, brows, icheck)


def print_blend(bsummary: pd.DataFrame, brows: pd.DataFrame,
                icheck: pd.DataFrame) -> None:
    """
    The book-wide blend, reported on its own.

    Split out of `print_results()` so `--blend-only` can print it without
    manufacturing empty frames for the two shortlist tracks — a shape that
    only ever existed to satisfy a signature.
    """
    print(f"\n{SEP}\nSEPARATE TRACK — 50/50 BLEND, WHOLE BOOK\n{SEP}")
    if bsummary.empty:
        print("  Nothing scorable.")
        return
    real = bsummary[~bsummary["blend_is_identity"]]
    print(f"  Items scored              : {len(bsummary)}")
    print(f"  Identity blends (routed = Naive-12mo, excluded from tallies): "
          f"{int(bsummary['blend_is_identity'].sum())}")
    print(f"  Blend of two naive levels (routed = Naive-3mo)             : "
          f"{int((bsummary['routed_model'] == 'Naive-3mo').sum())}")
    if len(icheck):
        worst = float(icheck["abs_diff_pp"].max())
        print(f"  Identity self-check — max |blend - routed| WAPE over those "
              f"{len(icheck)} items: {worst:.3f} pp")
        print(f"    (must be ~0: the blend of a Naive-12mo item with its own "
              f"trailing mean is itself.")
        print(f"     A non-trivial number here means the reconstruction "
              f"disagrees with the shipped")
        print(f"     forecast, which is how the missing split_ratio was "
              f"found.)")
        if worst > 1.0:
            print(f"    WARNING: above 1pp. Read blend_ensemble_summary.csv "
                  f"before trusting the tallies.")
    print(f"\n  Median WAPE over the {len(real)} non-identity items:")
    for label, col in (("Routed (production)", "wape_routed"),
                       ("Naive-12mo", "wape_naive12"),
                       ("Blend-50/50", "wape_blend"),
                       ("BDM", "wape_bdm")):
        print(f"    {label:<20}: {real[col].median():>8.2f}")
    # The median is one item one vote; the pooled figure is one unit one vote.
    # They disagree here and the disagreement IS the result — see the reading
    # printed underneath.
    if len(brows):
        pooled = brows[brows["item_code"].isin(real["item_code"])]
        print(f"\n  POOLED (volume-weighted) WAPE over the same items — "
              f"one unit one vote,")
        print(f"  where the median above is one item one vote:")
        for label, cand in (("Routed (production)", CAND_ROUTED),
                            ("Naive-12mo", CAND_NAIVE12),
                            ("Blend-50/50", CAND_BLEND), ("BDM", BDM)):
            g = pooled[pooled["candidate"] == cand]
            den = float(g["actual"].abs().sum())
            if den > 0:
                print(f"    {label:<20}: "
                      f"{(g['actual'] - g['yhat']).abs().sum() / den * 100:>8.2f}")

    print(f"\n  Blend beats routed by >{TIE_TOLERANCE_PP:.0f}pp : "
          f"{int(real['blend_beats_routed'].sum())} of {len(real)}")
    print(f"  Blend beats routed at all      : "
          f"{int((real['wape_blend'] < real['wape_routed']).sum())} of {len(real)}")
    print(f"  Blend beats BDM by >{TIE_TOLERANCE_PP:.0f}pp    : "
          f"{int(real['blend_beats_bdm'].sum())} of {len(real)}")
    print(f"  Median blend - routed      : "
          f"{real['blend_minus_routed_pp'].median():+.2f} pp")
    print(f"  Median blend - BDM         : "
          f"{real['blend_minus_bdm_pp'].median():+.2f} pp")
    print(f"  Median routed - BDM        : "
          f"{real['routed_minus_bdm_pp'].median():+.2f} pp")
    br = float(real["blend_bias_ratio"].median())
    rr = float(real["routed_bias_ratio"].median())
    print(f"\n  Shrinkage check on the blend (median bias ratio):")
    print(f"    blend  : {shrinkage_verdict(br)}")
    print(f"    routed : {shrinkage_verdict(rr)}")


def print_caveats(population: pd.DataFrame, folds: pd.DataFrame) -> None:
    """What this run does NOT say. Nothing here is a recommendation."""
    print(f"\n{SEP}\nWHAT THIS DOES NOT SAY\n{SEP}")
    print(f"  NOTHING IS ADOPTED. config.py is unchanged, "
          f"preprocessing/model_routing.py is unchanged,")
    print(f"  main.py is unchanged, the fitting path is unchanged, and no "
          f"existing output CSV is")
    print(f"  rewritten. This is discovery for the demo, not a production "
          f"change.")
    print(f"  TRACK 1 AND TRACK 2 ARE NOT COMPARABLE and are never averaged "
          f"together. Track 1's")
    print(f"  folds end at TRAIN_END ({config.TRAIN_END}); track 2 is the "
          f"single held-out window")
    print(f"  {config.BENCHMARK_START}..{config.BENCHMARK_END}. BDM exists "
          f"only in the second. A candidate that wins one")
    print(f"  and loses the other has not been contradicted — it has been "
          f"measured twice.")
    print(f"  TRACK 2 IS ONE FOLD PER ITEM. Six months, one origin. It is the "
          f"only place BDM")
    print(f"  can appear and it is the weaker of the two designs; read it "
          f"with track 1, not instead.")
    print(f"  THE SHORTLISTS WERE NOMINATED FROM A FEATURE FILE ANCHORED "
          f"INSIDE TRACK 2's WINDOW.")
    print(f"  item_features.csv's grid runs to the latest complete month, "
          f"which is inside")
    print(f"  {config.BENCHMARK_START}..{config.BENCHMARK_END}. So a track-2 "
          f"win is a measurement of the MODEL and is not")
    print(f"  evidence that the selection rule predicted anything. Only "
          f"track 1 tests the rule at")
    print(f"  arm's length.")
    print(f"  TWO ETS VARIANTS WERE RUN. Reporting the better of them as "
          f"'ETS' would be two shots")
    print(f"  at one target scored as one; they carry separate names "
          f"everywhere for that reason.")
    print(f"  SARIMA'S SEASONAL PERIOD IS FIXED AT {SARIMA_SEASONAL_PERIOD} "
          f"and its order is fixed. It is not tuned")
    print(f"  and a poor result is a result about THIS spec, not about "
          f"SARIMA.")
    print(f"  SAMPLE SIZE IS THE BINDING CONSTRAINT. {len(population)} items, "
          f"each resting on at most")
    print(f"  {MAX_CUTOFFS} folds of {CV_HORIZON_MONTHS} months in track 1 "
          f"and exactly one window in track 2.")
    print(f"  THE SEVERE-BIAS CLUSTER IS EXCLUDED "
          f"({', '.join(BIAS_CLUSTER)}) because")
    print(f"  erratic_naive_experiment.py already has a validated answer for "
          f"it and a result here")
    print(f"  could not be separated from that one. E35152 is carried in "
          f"Shortlist A by name only,")
    print(f"  for the cycle question, and is excluded from every aggregate.")
    print(f"  THE BLEND TRACK IS A DIFFERENT POPULATION AND A DIFFERENT "
          f"QUESTION. It is reported")
    print(f"  separately and its medians are not comparable to the "
          f"shortlists'.")


# ── Output schemas ────────────────────────────────────────────────────────────

FOLD_COLS = ["item_code", "candidate", "cutoff", "n_train_months_at_cutoff",
             "yhat_level_or_forecast", "actual_sum", "wape", "mape",
             "fold_note", "n_horizon_months", "abs_err_sum", "n_clipped_months"]

RESULT_COLS = ["item_code", "shortlist", "arm", "selection_basis", "category",
               "region", "candidate", "n_folds", "n_folds_scored",
               "n_folds_zero_demand", "wape", "wape_pooled", "mape",
               "mean_yhat_level", "n_clipped_months", "scorer_delta"]

WINDOW_ROW_COLS = ["item_code", "candidate", "month", "actual", "yhat_raw",
                   "yhat", "bdm_forecast", "routed_model"]

BLEND_ROW_COLS = ["item_code", "candidate", "month", "actual", "yhat",
                  "routed_model"]


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--db", action="store_true",
                    help="Reload raw inputs from Azure SQL instead of the cached CSVs")
    ap.add_argument("--input-dir", default=None,
                    help="Directory holding the cached input CSVs and receiving "
                         "this experiment's output (default: config.OUTPUT_DIR, "
                         "which is the post-window-roll vintage — see the module "
                         "docstring before pointing this at output_phase1)")
    ap.add_argument("--shortlist-only", action="store_true",
                    help="Print and write the selection, fit nothing")
    ap.add_argument("--blend-only", action="store_true",
                    help="Run the book-wide blend track only")
    ap.add_argument("--sample", type=int, default=0, metavar="N",
                    help="Fit a sample of N shortlisted items and stop without "
                         "writing the CSVs")
    args = ap.parse_args()

    if args.input_dir:
        config.OUTPUT_DIR = Path(args.input_dir).resolve()
    out_dir = config.OUTPUT_DIR
    print(f"Input/output directory: {out_dir}")
    if not out_dir.exists():
        print(f"  ERROR: {out_dir} does not exist. Pass --input-dir, or --db to "
              f"reload from the database.")
        return 1
    for required in ("item_features.csv", "benchmark_comparison.csv",
                     "test_validation.csv", "raw_data.csv"):
        if not (out_dir / required).exists():
            print(f"  ERROR: {out_dir / required} is missing. This experiment "
                  f"needs the post-window-roll vintage; see the module docstring.")
            return 1

    cached = not args.db
    raw = pd.read_csv(out_dir / "raw_data.csv")
    feat = pd.read_csv(out_dir / "item_features.csv")
    feat["item_code"] = feat["item_code"].astype(str).str.strip()
    feat["family_key"] = feat["family_key"].astype(str).str.strip()

    anchor = resolve_feature_anchor(feat)
    grids = anchor_grid(raw, anchor)
    mismatches = check_grid_reproduction(feat, grids)

    shortlists, funnel_a, funnel_b, cutoff = build_shortlists(feat, grids)
    population = tested_population(shortlists)

    pooled, _families, _cm, _fitted = build_scope_input(cached=cached)

    if not args.blend_only:
        print_selection(shortlists, population, funnel_a, funnel_b, cutoff,
                        anchor, mismatches)
        shortlists.round(4).to_csv(out_dir / "ets_sarima_shortlist.csv",
                                   index=False)
        print(f"\nWrote {out_dir / 'ets_sarima_shortlist.csv'}  "
              f"({len(shortlists):,} rows)")
        if args.shortlist_only:
            return 0
        if args.sample:
            population = population.head(args.sample)
            print(f"\nSAMPLE MODE — fitting {len(population)} item(s)\n")

    # ── Blend track (no fitting) ──────────────────────────────────────────────
    blend_rows, blend_scores = blend_track(out_dir, pooled.data)
    bsummary = blend_summary(blend_scores)
    icheck = identity_check(blend_scores)

    if args.blend_only:
        print_blend(bsummary, blend_rows, icheck)
        for path, frame in ((out_dir / "blend_ensemble_results.csv",
                             blend_rows.reindex(columns=BLEND_ROW_COLS).round(3)),
                            (out_dir / "blend_ensemble_summary.csv",
                             bsummary.round(3))):
            frame.to_csv(path, index=False)
            print(f"\nWrote {path}  ({len(frame):,} rows)")
        return 0

    # ── Shortlist tracks ──────────────────────────────────────────────────────
    window_all = load_bdm_window(out_dir)
    print(f"\nFitting {len(population)} shortlisted item(s)...")
    folds, wrows, skipped, failures, frames, elapsed, n_fits = run_shortlists(
        population, pooled.data, window_all)

    if folds.empty:
        print("Nothing could be evaluated.")
        return 1

    scores = item_scores(folds, frames)
    results = (scores.merge(
        population[["item_code", "shortlist", "arm", "selection_basis",
                    "category", "region"]], on="item_code", how="left")
        .reindex(columns=RESULT_COLS).sort_values(["item_code", "candidate"]))
    summary = build_summary(scores, population)

    rule_items = population.loc[
        ~population["selection_basis"].str.contains("bias_cluster"), "item_code"]
    shrink = pd.concat([
        shrinkage_table(folds[folds["item_code"].isin(
            summary.loc[summary["shortlist"].str.contains("A", na=False)
                        & summary["item_code"].isin(rule_items), "item_code"])],
            "Shortlist A", CANDIDATES_A),
        shrinkage_table(folds[folds["item_code"].isin(
            summary.loc[summary["shortlist"].str.contains("B", na=False)
                        & summary["item_code"].isin(rule_items), "item_code"])],
            "Shortlist B", CANDIDATES_B),
    ], ignore_index=True)

    wscores = window_scores(wrows) if len(wrows) else pd.DataFrame()
    wsummary = (window_summary(wscores, population) if len(wscores)
                else pd.DataFrame())

    if args.sample:
        print(f"\nSample mode: no CSV written. {n_fits} fits in "
              f"{elapsed / 60:.1f} min.")
        return 0

    for path, frame in [
        (out_dir / "ets_sarima_folds.csv",
         folds.reindex(columns=FOLD_COLS).round(4)),
        (out_dir / "ets_sarima_results.csv", results.round(3)),
        (out_dir / "ets_sarima_summary.csv", summary.round(3)),
        (out_dir / "ets_sarima_shrinkage.csv", shrink.round(4)),
        (out_dir / "ets_sarima_bdm_window_results.csv",
         wrows.reindex(columns=WINDOW_ROW_COLS).round(3) if len(wrows)
         else pd.DataFrame(columns=WINDOW_ROW_COLS)),
        (out_dir / "ets_sarima_bdm_window_summary.csv",
         wsummary.round(3) if len(wsummary) else pd.DataFrame()),
        (out_dir / "ets_sarima_skipped.csv", skipped),
        (out_dir / "ets_sarima_fit_failures.csv", failures),
        (out_dir / "blend_ensemble_results.csv",
         blend_rows.reindex(columns=BLEND_ROW_COLS).round(3)),
        (out_dir / "blend_ensemble_summary.csv", bsummary.round(3)),
    ]:
        frame.to_csv(path, index=False)
        print(f"Wrote {path}  ({len(frame):,} rows)")

    print_results(population, skipped, failures, folds, summary, shrink,
                  wsummary, wscores, bsummary, blend_rows, icheck)
    print_caveats(population, folds)
    print(f"\nTotal wall clock: {elapsed / 60:.1f} min over {n_fits} fits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
