"""A pulled, frozen copy of the data a back-test needs (CSV files + manifest).

Everything downstream (back-test, later the AutoML export) reads a snapshot,
not the database, so a run can be repeated and compared exactly.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

FILES = {"sales": "sales_monthly.csv", "inventory": "inventory_monthly.csv", "invoices": "invoices_monthly.csv"}


@dataclass
class Snapshot:
    pool: str
    sales: pd.DataFrame        # asin, month_start, ordered_units, shipped_units
    inventory: pd.DataFrame    # asin, month_start, net_received_units, sellable_units, open_po_units
    invoices: pd.DataFrame     # marketplace, product_code, month_start, gross_units, credit_units
    manifest: dict

    @property
    def months(self) -> pd.DatetimeIndex:
        return pd.date_range(self.sales["month_start"].min(), self.sales["month_start"].max(), freq="MS")


def save_snapshot(directory: Path, pool: str, sales, inventory, invoices, manifest: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, df in (("sales", sales), ("inventory", inventory), ("invoices", invoices)):
        df.to_csv(directory / FILES[name], index=False)
    (directory / "manifest.json").write_text(json.dumps({"pool": pool, **manifest}, indent=2, default=str))
    return directory


def load_snapshot(directory: Path) -> Snapshot:
    directory = Path(directory)
    if not (directory / "manifest.json").exists():
        hint = " This looks like a results folder; use the snapshot folder under snapshots/." \
            if (directory / "scores.csv").exists() else ""
        raise SystemExit(f"No snapshot at {directory} (manifest.json missing).{hint}")
    manifest = json.loads((directory / "manifest.json").read_text())
    frames = {n: pd.read_csv(directory / f, parse_dates=["month_start"]) for n, f in FILES.items()}
    return Snapshot(manifest["pool"], frames["sales"], frames["inventory"], frames["invoices"], manifest)
