"""Export a snapshot as Azure ML AutoML forecasting input.

Same contract as the non-Amazon benchmark (agents/demand-planning/azureml_benchmark):

  train/    MLTable + train.parquet     series_id, ds, y        months up to train_end ONLY
  horizon/  MLTable + horizon.parquet   series_id, ds, y = 0.0  DATES ONLY for the holdout months
  holdout_actuals.csv                   stays on this machine; never uploaded
  manifest.json

series_id is the ASIN, so the SKU map plays no part. The training grid is zero-filled from
each ASIN's first appearance, so nothing is invented before a product existed. The last
`horizon` months of the snapshot are the holdout: AutoML never sees them, and the baselines
are scored on exactly the same window.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .series import to_wide
from .snapshot import Snapshot

MLTABLE_PARQUET = """paths:
- file: {name}
transformations:
- read_parquet:
    include_path_column: false
    path_column: Path
type: mltable
"""


@dataclass
class ExportTables:
    train: pd.DataFrame
    horizon: pd.DataFrame
    actuals: pd.DataFrame
    train_end: pd.Timestamp
    horizon_months: pd.DatetimeIndex
    too_short: list


def build_tables(snap: Snapshot, target: str = "ordered_units", horizon: int = 6,
                 min_history: int = 12) -> ExportTables:
    months = snap.months
    n = len(months)
    if n < horizon + min_history + 1:
        raise ValueError(f"{n} months is too few for horizon={horizon} and min_history={min_history}")
    t = n - horizon - 1                                   # index of the last training month
    wide = to_wide(snap.sales, "asin", target, months, mask_before_first=True)
    hist = wide.iloc[:, : t + 1]
    ok = hist.notna().sum(axis=1) >= min_history
    too_short = sorted(hist.index[~ok])
    hist = hist[ok]
    train = (hist.rename_axis("series_id").reset_index()
             .melt(id_vars="series_id", var_name="ds", value_name="y").dropna())
    train["ds"] = pd.to_datetime(train["ds"])
    train = train.sort_values(["series_id", "ds"]).reset_index(drop=True)
    hmonths = months[t + 1:]
    series = list(hist.index)
    horizon_df = pd.DataFrame({"series_id": [s for s in series for _ in hmonths],
                               "ds": list(hmonths) * len(series), "y": 0.0})
    act = (wide.loc[series, hmonths].fillna(0.0).rename_axis("series_id").reset_index()
           .melt(id_vars="series_id", var_name="ds", value_name="actual"))
    act["ds"] = pd.to_datetime(act["ds"])
    return ExportTables(train, horizon_df, act.sort_values(["series_id", "ds"]).reset_index(drop=True),
                        months[t], hmonths, too_short)


def write_export(tables: ExportTables, pool: str, out_root: Path, target: str, horizon: int,
                 min_history: int, snapshot_ref: str = "") -> Path:
    tr, hz = tables.train, tables.horizon
    # leakage guards: the training table stops at train_end, the horizon table is dates only
    assert tr["ds"].max() <= tables.train_end, "training data runs past train_end"
    assert (hz["ds"] > tables.train_end).all(), "horizon table contains training months"
    assert (hz["y"] == 0.0).all(), "horizon table must carry only the y=0 placeholder"
    assert set(hz["series_id"]) == set(tr["series_id"]), "series differ between train and horizon"
    digest = hashlib.sha256(tr.to_csv(index=False).encode()).hexdigest()[:8]
    version = f"{tables.train_end:%Y%m}-{digest}"
    out = Path(out_root) / pool / version
    for name, df in (("train", tr), ("horizon", hz)):
        d = out / name
        d.mkdir(parents=True, exist_ok=True)
        df.to_parquet(d / f"{name}.parquet", index=False)
        (d / "MLTable").write_text(MLTABLE_PARQUET.format(name=f"{name}.parquet"), encoding="utf-8")
    tables.actuals.to_csv(out / "holdout_actuals.csv", index=False)
    (out / "manifest.json").write_text(json.dumps({
        "pool": pool, "version": version, "target": target, "horizon": horizon, "min_history": min_history,
        "train_end": str(tables.train_end.date()),
        "holdout_months": [str(m.date()) for m in tables.horizon_months],
        "series": int(tr["series_id"].nunique()), "train_rows": len(tr),
        "series_too_short": tables.too_short, "snapshot_ref": snapshot_ref,
    }, indent=2))
    return out
