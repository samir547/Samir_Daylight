"""Register the snapshot's training MLTable (and the date-only horizon table) as versioned Azure ML data assets.

    python -m azureml_benchmark.azure.register_data <snapshot_dir> [--dry-run]

The horizon table holds series_id, ds and a placeholder y=0 for the 6 test months -- DATES ONLY.
Real test actuals never leave this machine (checked by contract_checks #7).
--dry-run builds and prints everything locally, uploads nothing, needs no Azure login.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from azureml_benchmark import common
from azureml_benchmark.make_mltable import write_mltable
from azureml_benchmark.azure import settings


def pick_smoke(snap: Path, n: int) -> list[str]:
    """Deterministic n-series sample, spread across routes (largest-volume first within each)."""
    idx = pd.read_csv(snap / "reference" / "series_index.csv", dtype={"series_id": str})
    idx = idx[idx["n_train_months"] > 0] if "n_train_months" in idx.columns else idx
    train = pd.read_parquet(snap / "train" / "train.parquet")
    vol = train.groupby("series_id")["y"].sum()
    idx = idx[idx["series_id"].isin(vol.index)].assign(vol=lambda d: d["series_id"].map(vol))
    key = "route" if "route" in idx.columns else None
    groups = [g.sort_values("vol", ascending=False)["series_id"].tolist()
              for _, g in (idx.groupby(key) if key else [(0, idx)])]
    out = []
    while len(out) < n and any(groups):
        for g in groups:
            if g and len(out) < n:
                out.append(g.pop(0))
    return out


def build_horizon(snap: Path, series: list[str] | None = None, name: str = "horizon") -> Path:
    test = pd.read_parquet(snap / "test_actuals" / "test_actuals.parquet")
    train = pd.read_parquet(snap / "train" / "train.parquet")
    if series is not None:
        train = train[train["series_id"].isin(series)]
    keep = test[test["series_id"].isin(train["series_id"].unique())]   # series with no training history can't be forecast
    h = keep[["series_id", "ds"]].assign(y=0.0)
    out = snap / name
    out.mkdir(exist_ok=True)
    h.to_parquet(out / "horizon.parquet", index=False)
    write_mltable(out, "horizon.parquet")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("snapshot_dir", type=Path)
    ap.add_argument("--smoke", type=int, help="register a small N-series copy (version <anchor>-smokeN)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    snap = a.snapshot_dir.resolve()
    man = common.read_manifest(snap)
    version = man["anchor"]
    train_dir = snap / "train"
    if a.smoke:
        ser = pick_smoke(snap, a.smoke)
        version = f"{version}-smoke{a.smoke}"
        train_dir = snap / f"train_smoke{a.smoke}"
        train_dir.mkdir(exist_ok=True)
        t = pd.read_parquet(snap / "train" / "train.parquet")
        t[t["series_id"].isin(ser)].to_parquet(train_dir / "train.parquet", index=False)
        write_mltable(train_dir)
        horizon_dir = build_horizon(snap, ser, f"horizon_smoke{a.smoke}")
    else:
        horizon_dir = build_horizon(snap)
    h = pd.read_parquet(horizon_dir / "horizon.parquet")
    print(f"horizon table: {len(h)} rows, {h['series_id'].nunique()} series, {h['ds'].min()}..{h['ds'].max()}")
    if a.dry_run:
        print(f"[dry run] would register {settings.DATA_ASSET}:{version} <- {train_dir}")
        print(f"[dry run] would register {settings.HORIZON_ASSET}:{version} <- {horizon_dir}")
        return
    from azure.ai.ml.entities import Data
    from azure.ai.ml.constants import AssetTypes
    c = settings.ml_client()
    for name, path in ((settings.DATA_ASSET, train_dir), (settings.HORIZON_ASSET, horizon_dir)):
        asset = c.data.create_or_update(Data(name=name, version=version, type=AssetTypes.MLTABLE,
                                             path=str(path), description=f"anchor {version}; sha in manifest"))
        print("registered", asset.name, asset.version)


if __name__ == "__main__":
    main()
