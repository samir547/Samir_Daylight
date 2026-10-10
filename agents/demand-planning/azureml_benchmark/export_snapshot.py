"""
Export a frozen run folder as an Azure ML benchmark snapshot.

    cd agents/demand-planning
    python -m azureml_benchmark.export_snapshot \
        --run-dir ../../../../prophet_forecast/output/runs/2026-09

Output (azureml_benchmark/snapshots/<anchor>/):
    manifest.json                  anchor, windows, counts, file hashes, code commit
    train/                         MLTable + train.parquet   (series_id, ds, y)  <= TRAIN_END
    test_actuals/                  parquet (series_id, ds, actual, has_sale)    TEST window
    reference/
        series_index.csv           one row per FIT KEY: route, class, members, counts
        routing_items.csv          one row per ITEM as routed (route, block_reason, family_key)
        split_ratios.csv           pooled-family split ratios (test + forward basis)
        item_monthly.parquet       item-grain non-Amazon monthly actuals (scorer input)
        successor_map.csv          copy, needed to rebuild the family structure when scoring
        arm_a/                     the production hybrid's own results for the same windows

Training data rules (agreed 2026-10-10):
  * every routed fit key is exported — Prophet, Naive and Blocked routes alike —
    with its route and block_reason in the reference tables, NOT as a feature;
  * gap months inside a series' history are filled with 0 (no sale = zero
    demand); a series starts at its first sale month, so nothing is invented
    before a product launched;
  * test actuals keep NaN for a month with no sales row (the production
    benchmark treats that as unscorable, not zero) plus a has_sale flag, so
    scoring can be done both ways.
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from azureml_benchmark import common
from azureml_benchmark.common import config
from azureml_benchmark.make_mltable import write_mltable

logger = logging.getLogger("azureml_benchmark.export")


def build_series_tables(st: common.PipelineState) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(train, test_actuals, series_index) for every routed fit key."""
    fam = st.families
    routing = st.routing
    train_end = common.month_ts(config.TRAIN_END)
    test_months = pd.date_range(common.month_ts(config.TEST_START),
                                common.month_ts(config.TEST_END), freq="MS")

    data = st.pooled.data.copy()
    data["item_code"] = data["item_code"].astype(str).str.strip()
    by_key = {k: g for k, g in data.groupby("item_code")}

    # One index row per fit key; routes are per-ITEM, so collect the items of each key.
    keys = sorted(routing["family_key"].astype(str).str.strip().unique())
    train_parts, test_parts, index_rows = [], [], []

    region_by_item = st.series_meta.set_index("item_code")["region"].to_dict()

    for key in keys:
        items = routing[routing["family_key"].astype(str).str.strip() == key]
        member_items = sorted(items["item_code"].astype(str).str.strip())
        is_pooled = bool(items["is_pooled"].iloc[0])
        all_members = list(fam.members_by_family.get(key, (key,))) if is_pooled else [key]
        routes = sorted(items["route"].unique())
        reasons = sorted({str(r) for r in items["block_reason"].dropna()})
        cats = sorted(items["category"].dropna().unique())
        regions = [region_by_item.get(c) for c in member_items + all_members]
        region = next((r for r in regions if isinstance(r, str) and r), "")
        fams = [st.family_map.get(c) for c in member_items if c in st.family_map.index]
        fam_tag = pd.Series(fams).mode().iloc[0] if fams else config.FAMILY_UNKNOWN

        n_span = n_sales = 0
        first_sale = ""
        has_train = False
        n_test_sales = 0

        g = by_key.get(key)
        if g is not None and not g.empty:
            s = g.groupby("ds", as_index=True)["y"].sum().sort_index()
            first = s.index.min()
            first_sale = first.strftime("%Y-%m")
            # ── training series: zero-filled grid, first sale -> TRAIN_END ──
            if first <= train_end:
                grid = pd.date_range(first, train_end, freq="MS")
                y = s.reindex(grid).fillna(0.0).astype("float64")
                has_train = True
                n_span = len(grid)
                n_sales = int(s.reindex(grid).notna().sum())
                train_parts.append(pd.DataFrame({"series_id": key, "ds": grid, "y": y.values}))
            # ── test actuals: NaN where no sales row ──
            act = s.reindex(test_months)
            n_test_sales = int(act.notna().sum())
            test_parts.append(pd.DataFrame({
                "series_id": key, "ds": test_months,
                "actual": act.values.astype("float64"),
                "has_sale": act.notna().values,
            }))
        else:
            test_parts.append(pd.DataFrame({
                "series_id": key, "ds": test_months,
                "actual": np.full(len(test_months), np.nan), "has_sale": False,
            }))

        index_rows.append({
            "series_id": key, "is_pooled": is_pooled,
            "n_items": len(member_items), "current_items": "|".join(member_items),
            "members": "|".join(all_members),
            "route": "|".join(routes), "block_reason": "|".join(reasons),
            "category": "|".join(cats), "region": region, "family": fam_tag,
            "has_train_data": has_train, "first_sale": first_sale,
            "n_train_months": n_span, "n_train_months_with_sales": n_sales,
            "n_test_months_with_sales": n_test_sales,
        })

    train = pd.concat(train_parts, ignore_index=True) if train_parts else pd.DataFrame(
        columns=["series_id", "ds", "y"])
    test = pd.concat(test_parts, ignore_index=True)
    index = pd.DataFrame(index_rows)
    train["ds"] = pd.to_datetime(train["ds"])
    test["ds"] = pd.to_datetime(test["ds"])
    return train, test, index


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", type=Path, required=True,
                    help="frozen run folder holding the five cached CSVs")
    ap.add_argument("--anchor", help="expected anchor month (YYYY-MM); fails if the data disagrees")
    ap.add_argument("--out-root", type=Path,
                    default=Path(__file__).resolve().parent / "snapshots")
    ap.add_argument("--force", action="store_true", help="overwrite an existing snapshot folder")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    run_dir = args.run_dir.resolve()
    st = common.build_state(run_dir, args.anchor)
    out = args.out_root / st.anchor
    if out.exists():
        if not args.force:
            logger.error("%s already exists — pass --force to overwrite.", out)
            return 2
        shutil.rmtree(out)
    (out / "train").mkdir(parents=True)
    (out / "test_actuals").mkdir()
    (out / "reference" / "arm_a").mkdir(parents=True)

    train, test, index = build_series_tables(st)

    train.to_parquet(out / "train" / "train.parquet", index=False)
    write_mltable(out / "train")
    test.to_parquet(out / "test_actuals" / "test_actuals.parquet", index=False)

    ref = out / "reference"
    index.to_csv(ref / "series_index.csv", index=False)
    st.routing.assign(
        region=st.routing["item_code"].map(st.series_meta.set_index("item_code")["region"]),
        family=st.routing["item_code"].map(st.family_map).fillna(config.FAMILY_UNKNOWN),
    ).to_csv(ref / "routing_items.csv", index=False)
    st.split_ratios.to_csv(ref / "split_ratios.csv", index=False)
    st.prepared.to_parquet(ref / "item_monthly.parquet", index=False)
    st.successor.to_csv(ref / "successor_map.csv", index=False)
    for name in common.ARM_A_FILES:
        if (run_dir / name).exists():
            shutil.copy2(run_dir / name, ref / "arm_a" / name)
    fwd = common.find_forward_forecast(run_dir)
    if fwd:
        shutil.copy2(fwd, ref / "arm_a" / fwd.name)

    files = sorted(p for p in out.rglob("*") if p.is_file() and p.name != "manifest.json")
    manifest = {
        "schema": 1,
        "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "anchor": st.anchor,
        "windows": {k: getattr(config, k) for k in (
            "TRAIN_START", "TRAIN_END", "TEST_START", "TEST_END",
            "FORECAST_START", "FORECAST_END")},
        "source_run_dir": run_dir.name,
        "source_inputs_sha256": {n: common.sha256_of(run_dir / n) for n in common.RUN_INPUTS},
        "code": {"git_commit": common.git_commit(), "git_dirty_in_agent_dir": common.git_dirty()},
        "rules": {
            "gap_months": "filled with 0 from each series' first sale month to TRAIN_END",
            "test_actual_missing_month": "NaN + has_sale=False (production benchmark: unscorable)",
            "amazon": "rows with channel in config.AMAZON_CHANNELS removed before aggregation",
            "series_key": "fit key (family_key for pooled successor families, else item_code)",
        },
        "counts": {
            "routed_items": int(len(st.routing)),
            "fit_keys": int(len(index)),
            "fit_keys_with_train_data": int(index["has_train_data"].sum()),
            "fit_keys_without_train_data": int((~index["has_train_data"]).sum()),
            "train_rows": int(len(train)),
            "test_rows": int(len(test)),
            "pooled_fit_keys": int(index["is_pooled"].sum()),
            "routes_items": {k: int(v) for k, v in st.routing["route"].value_counts().items()},
            "excluded_amazon_rows": int(len(st.excluded_amazon)),
            "channel_mismatch_items": sorted(st.channel_mismatch),
        },
        "files": {str(p.relative_to(out)).replace("\\", "/"): {
            "sha256": common.sha256_of(p), "bytes": p.stat().st_size} for p in files},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    c = manifest["counts"]
    print(f"\nSnapshot written: {out}")
    print(f"  anchor {st.anchor} | train to {config.TRAIN_END} | test {config.TEST_START}..{config.TEST_END}")
    print(f"  {c['routed_items']} routed items -> {c['fit_keys']} fit keys "
          f"({c['pooled_fit_keys']} pooled) | with train data {c['fit_keys_with_train_data']}, "
          f"without {c['fit_keys_without_train_data']}")
    print(f"  train rows {c['train_rows']:,} | routes (items): {c['routes_items']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
