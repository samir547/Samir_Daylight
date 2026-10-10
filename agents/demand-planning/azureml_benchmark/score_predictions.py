"""
Score a predictions file against the snapshot's held-out actuals.

    python -m azureml_benchmark.score_predictions <snapshot_dir> \
        --predictions <parquet|csv with series_id, ds, yhat, model> [--out-dir results/x]

What it does, in order:
  1. split fit-key predictions back to ITEM grain with the production code
     (family_pool.split_forecast, test-basis ratios — never ratios that have
     seen the test window), so every arm is scored at the grain arm A is;
  2. attach each item's actual -- its own sales row, or where it has none the
     family's actual x split ratio (production's fallback; `actual_own` keeps the
     strict view) -- and its prior-year baseline (same month, one year earlier);
  3. report, per model and per slice (overall / route / demand class / region /
     pooled-vs-single):
        * WAPE and bias on months with a real actual  -> comparable with the
          production benchmark, which treats a no-sale month as unscorable;
        * the like-for-like view: only months that ALSO have a prior-year
          baseline, with the baseline's WAPE alongside (the headline the
          production run quotes);
        * a zero-inclusive WAPE (missing actual counted as 0), reported
          separately and labelled — never mixed with the first.
  4. coverage: how many items had actuals in the test window but got no
     forecast from that model.

WAPE = sum|forecast - actual| / sum|actual|   (evaluation/metrics.py's definition)
Bias = sum(forecast - actual) / sum(actual)    (positive = over-forecast)
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from azureml_benchmark import common
from azureml_benchmark.common import config  # noqa: F401  (puts agent root on sys.path)
from preprocessing import family_pool

logger = logging.getLogger("azureml_benchmark.score")


# ── Snapshot reference tables ─────────────────────────────────────────────────

def _load_test_actuals(snapshot_dir: Path) -> pd.DataFrame:
    t = pd.read_parquet(snapshot_dir / "test_actuals" / "test_actuals.parquet")
    t["series_id"] = t["series_id"].astype(str).str.strip()
    t["ds"] = pd.to_datetime(t["ds"])
    return t


def load_reference(snapshot_dir: Path) -> dict:
    ref = Path(snapshot_dir) / "reference"
    item_monthly = pd.read_parquet(ref / "item_monthly.parquet")
    item_monthly["item_code"] = item_monthly["item_code"].astype(str).str.strip()
    item_monthly["ds"] = pd.to_datetime(item_monthly["ds"])
    return {
        "manifest": common.read_manifest(Path(snapshot_dir)),
        "series_index": pd.read_csv(ref / "series_index.csv", dtype={"series_id": str}),
        "routing": pd.read_csv(ref / "routing_items.csv", dtype={"item_code": str, "family_key": str}),
        "split_ratios": pd.read_csv(ref / "split_ratios.csv", dtype={"family_key": str, "item_code": str}),
        "successor": pd.read_csv(ref / "successor_map.csv", dtype=str),
        "item_monthly": item_monthly,
        "test_actuals": _load_test_actuals(Path(snapshot_dir)),
    }


def to_item_grain(pred: pd.DataFrame, ref: dict) -> pd.DataFrame:
    """Fit-key predictions -> item-grain rows (item_code, ds, yhat, model)."""
    need = {"series_id", "ds", "yhat", "model"}
    if not need.issubset(pred.columns):
        raise ValueError(f"predictions need columns {sorted(need)}, got {list(pred.columns)}")
    df = pred.rename(columns={"series_id": "item_code"}).copy()
    df["item_code"] = df["item_code"].astype(str).str.strip()
    df["ds"] = pd.to_datetime(df["ds"])
    families = family_pool.build_successor_families(ref["successor"])
    parts = []
    for model, g in df.groupby("model", sort=True):
        s = family_pool.split_forecast(g.drop(columns=["model"]), families, ref["split_ratios"],
                                       basis=family_pool.BASIS_TEST)
        s["model"] = model
        parts.append(s[["item_code", "ds", "yhat", "model", "family_key", "split_ratio"]])
    return pd.concat(parts, ignore_index=True)


def attach_actuals(item_pred: pd.DataFrame, ref: dict) -> pd.DataFrame:
    """Add actual, prior_year and routing attributes.

    `actual` follows the production benchmark exactly: the item's OWN sales row
    for the month, and -- only where the item has no row at all -- the family's
    actual scaled by the item's split ratio (family_pool.split_forecast's
    fallback). That fallback is what lets a successor code be scored for a month
    in which only its predecessor sold. `actual_own` is the strict view (NaN when
    the item has no row) and `backfilled` marks the rows where the two differ.
    """
    im = ref["item_monthly"]
    act = im.rename(columns={"y": "actual_own"})[["item_code", "ds", "actual_own"]]
    out = item_pred.merge(act, on=["item_code", "ds"], how="left")
    if "family_key" not in out.columns:
        fk = ref["routing"][["item_code", "family_key"]].drop_duplicates("item_code")
        out = out.merge(fk, on="item_code", how="left")
    if "split_ratio" not in out.columns:
        out["split_ratio"] = 1.0
    fam = ref["test_actuals"][["series_id", "ds", "actual"]].rename(
        columns={"series_id": "family_key", "actual": "_fam_actual"})
    out = out.merge(fam, on=["family_key", "ds"], how="left")
    out["actual"] = out["actual_own"].fillna((out["_fam_actual"] * out["split_ratio"]).round(2))
    out["backfilled"] = out["actual_own"].isna() & out["actual"].notna()
    out = out.drop(columns=["_fam_actual"])
    py = im.assign(ds=im["ds"] + pd.DateOffset(months=12)).rename(columns={"y": "prior_year"})
    out = out.merge(py[["item_code", "ds", "prior_year"]], on=["item_code", "ds"], how="left")
    r = ref["routing"][["item_code", "route", "category", "region", "is_pooled"]]
    return out.merge(r, on="item_code", how="left")


# ── Metrics ───────────────────────────────────────────────────────────────────

def _wape(a: pd.Series, f: pd.Series) -> float:
    d = float(np.abs(a).sum())
    return float(np.abs(f - a).sum() / d * 100) if d else float("nan")


def _bias(a: pd.Series, f: pd.Series) -> float:
    d = float(a.sum())
    return float((f - a).sum() / d * 100) if d else float("nan")


def _row(g: pd.DataFrame) -> dict:
    v = g.dropna(subset=["actual", "yhat"])
    lfl = v.dropna(subset=["prior_year"])
    z = g.assign(actual=g["actual"].fillna(0.0)).dropna(subset=["yhat"])
    return {
        "n_rows": len(v), "n_items": v["item_code"].nunique(),
        "wape": round(_wape(v["actual"], v["yhat"]), 1) if len(v) else np.nan,
        "bias": round(_bias(v["actual"], v["yhat"]), 1) if len(v) else np.nan,
        "n_lfl": len(lfl),
        "wape_lfl": round(_wape(lfl["actual"], lfl["yhat"]), 1) if len(lfl) else np.nan,
        "wape_prior_year_lfl": round(_wape(lfl["actual"], lfl["prior_year"]), 1) if len(lfl) else np.nan,
        "wape_zero_inclusive": round(_wape(z["actual"], z["yhat"]), 1) if len(z) else np.nan,
        "n_zero_inclusive": len(z),
    }


SLICES = (("overall", None), ("route", "route"), ("category", "category"),
          ("region", "region"), ("is_pooled", "is_pooled"))


def scorecard(scored: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for model, g in scored.groupby("model", sort=True):
        for label, col in SLICES:
            if col is None:
                rows.append({"model": model, "slice": "overall", "value": "all", **_row(g)})
            else:
                for val, gg in g.groupby(col, dropna=False, sort=True):
                    rows.append({"model": model, "slice": label, "value": str(val), **_row(gg)})
    return pd.DataFrame(rows)


def coverage(scored: pd.DataFrame, ref: dict) -> pd.DataFrame:
    """Per model: items that had real test-window actuals vs items the model forecast."""
    im = ref["item_monthly"]
    w = ref["manifest"]["windows"]
    in_test = im[(im["ds"] >= common.month_ts(w["TEST_START"])) & (im["ds"] <= common.month_ts(w["TEST_END"]))]
    with_actuals = set(in_test["item_code"])
    rows = []
    for model, g in scored.groupby("model", sort=True):
        forecast_items = set(g["item_code"])
        rows.append({"model": model,
                     "items_with_test_actuals": len(with_actuals),
                     "of_which_forecast": len(with_actuals & forecast_items),
                     "of_which_not_forecast": len(with_actuals - forecast_items)})
    return pd.DataFrame(rows)


def score_item_frame(df: pd.DataFrame, ref: dict) -> pd.DataFrame:
    """Score an already item-grain frame (item_code, ds, yhat, model) — e.g. arm A's
    own test_validation.csv — through the same metrics code."""
    keep = [c for c in ("item_code", "ds", "yhat", "model", "family_key", "split_ratio") if c in df.columns]
    d = df[keep].copy()
    d["item_code"] = d["item_code"].astype(str).str.strip()
    d["ds"] = pd.to_datetime(d["ds"].astype(str) + "-01") if d["ds"].astype(str).str.len().max() == 7 \
        else pd.to_datetime(d["ds"])
    return attach_actuals(d, ref)


def score_predictions(snapshot_dir: Path, pred: pd.DataFrame):
    ref = load_reference(snapshot_dir)
    scored = attach_actuals(to_item_grain(pred, ref), ref)
    return scored, scorecard(scored), coverage(scored, ref)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("snapshot_dir", type=Path)
    ap.add_argument("--predictions", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path)
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    p = pd.read_parquet(a.predictions) if a.predictions.suffix == ".parquet" else pd.read_csv(a.predictions)
    scored, card, cov = score_predictions(a.snapshot_dir, p)
    pd.set_option("display.width", 200, "display.max_columns", 20)
    print(card[card["slice"] == "overall"].drop(columns=["slice", "value"]).to_string(index=False))
    print(); print(cov.to_string(index=False))
    if a.out_dir:
        a.out_dir.mkdir(parents=True, exist_ok=True)
        scored.to_csv(a.out_dir / "scored_rows.csv", index=False)
        card.to_csv(a.out_dir / "scorecard.csv", index=False)
        cov.to_csv(a.out_dir / "coverage.csv", index=False)
        print(f"\nwritten to {a.out_dir}")
