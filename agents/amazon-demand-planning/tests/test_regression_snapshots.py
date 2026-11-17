"""Regression proof on the real local UK and US snapshots: every value for an ASIN with 12+ months of sales is
identical to the stored forecast made by the code before the EU / young-product / explanation changes.

The snapshots and results are git-ignored business data, so this skips wherever they are not present (CI, a
fresh clone). The stored CSVs were made on 2026-11-03 from these exact snapshots with that earlier code.
"""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from order_forecast.live import load_settings, settings_for_pool
from order_forecast.replenish import build_replenish_forecast
from order_forecast.series import to_wide
from order_forecast.snapshot import load_snapshot

ROOT = Path(__file__).resolve().parent.parent
CASES = [("UK", "snapshots/UK/20261103T172728Z", "results/live/UK/20261103T172729Z/forecast.csv"),
         ("US", "snapshots/US/20261103T180909Z", "results/live/US/20261103T180913Z/forecast.csv")]
NUMBERS = ["units_forecast", "units_p80", "units_upper", "order_chance"]


@pytest.mark.parametrize("pool,snap_dir,stored", CASES, ids=[c[0] for c in CASES])
def test_mature_asins_are_unchanged_on_the_real_snapshot(pool, snap_dir, stored):
    if not (ROOT / snap_dir / "manifest.json").exists() or not (ROOT / stored).exists():
        pytest.skip(f"local {pool} snapshot or stored result not present")
    snap = load_snapshot(ROOT / snap_dir)
    settings = settings_for_pool(load_settings(ROOT / "config" / "forecast_settings.replenish.json"), pool)
    settings["adjustments_file"] = None           # the stored run had no adjustments file
    f = build_replenish_forecast(snap, settings)
    w = to_wide(snap.sales, "asin", settings["target"], snap.months, mask_before_first=True)
    mature = set(w.index[(~w.isna()).sum(axis=1) >= 12])

    old = pd.read_csv(ROOT / stored, dtype={"adjustments": str}, keep_default_na=True)
    old = old[old.asin.isin(mature)].sort_values(["asin", "month_start"]).reset_index(drop=True)
    new = f.table.assign(month_start=f.table.month_start.dt.strftime("%Y-%m-%d"))
    new = new[new.asin.isin(mature)].sort_values(["asin", "month_start"]).reset_index(drop=True)

    assert len(mature) > 50 and len(new) == len(old) == 6 * len(mature)
    assert (new[["asin", "month_start"]].to_numpy() == old[["asin", "month_start"]].to_numpy()).all()
    assert np.array_equal(new[NUMBERS].to_numpy(float), old[NUMBERS].to_numpy(float), equal_nan=True)
    assert (new["basis"].to_numpy() == old["basis"].to_numpy()).all()
    # what is new is only additional: young-product backups (never a mature ASIN) and explanation columns
    young = set(f.extras["young"])
    assert not young & mature and set(f.table.asin) == mature | young
    assert f.table.loc[f.table.asin.isin(young), "reason_code"].eq("YOUNG_PRODUCT_BACKUP").all()
