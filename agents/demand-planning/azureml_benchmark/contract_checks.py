"""
Data-contract checks for a snapshot. Every check must pass before anything is
uploaded to Azure ML.

    python -m azureml_benchmark.contract_checks <snapshot_dir> --run-dir <frozen run folder>

Checks (FAIL exits non-zero):
  1  windows              manifest windows follow the standard offsets from the anchor,
                          and match the production run's own test/forecast windows
  2  grid                 (series_id, ds) unique; month-start dates; contiguous monthly
                          grid from first sale to TRAIN_END for every series
  3  values               y numeric, finite, non-negative, no nulls
  4  coverage             routed items reconcile to fit keys; every fit key accounted for
  5  pooling              pooled series equal the sum of their member items; split
                          ratios sum to 1 per family (test and forward basis)
  6  amazon               no Amazon rows leaked in; quantity conserved vs the raw file
  7  leakage              nothing from the test window in train/; train/ holds only
                          MLTable + train.parquet
  8  routing              route mix and item sets reproduce the production run's output
  9  mltable round trip   MLTable loads to the same rows/types; file hashes match manifest
  10a stand-in vs naive   independent trailing-mean re-implementation equals the production
                          Naive-3mo / Naive-12mo rows, item by item
  10b scorer vs benchmark the scorer reproduces the production benchmark's per-model WAPE
                          from benchmark_comparison.csv
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from azureml_benchmark import common, score_predictions as sp, standin_forecast as sf
from azureml_benchmark.common import config

results: list[tuple[str, str, str, str]] = []


def record(cid: str, name: str, ok: bool | None, detail: str) -> None:
    status = "PASS" if ok is True else "FAIL" if ok is False else "INFO"
    results.append((cid, name, status, detail))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("snapshot_dir", type=Path)
    ap.add_argument("--run-dir", type=Path, required=True)
    ap.add_argument("--json-out", type=Path)
    a = ap.parse_args(argv)
    snap, run = a.snapshot_dir.resolve(), a.run_dir.resolve()
    man = common.read_manifest(snap)
    w = man["windows"]
    train = pd.read_parquet(snap / "train" / "train.parquet")
    test = pd.read_parquet(snap / "test_actuals" / "test_actuals.parquet")
    ref = sp.load_reference(snap)
    idx, routing, im = ref["series_index"], ref["routing"], ref["item_monthly"]
    arm_a = snap / "reference" / "arm_a"
    tv = pd.read_csv(arm_a / "test_validation.csv", dtype={"item_code": str})

    # 1 ── windows ────────────────────────────────────────────────────────────
    exp = {k: config.shift_month(man["anchor"], off) for k, off in config.OFFSETS_FROM_TEST_END.items()}
    ok = all(w[k] == v for k, v in exp.items())
    ok_tv = (tv["ds"].min() == w["TEST_START"] and tv["ds"].max() == w["TEST_END"])
    fwd = sorted(arm_a.glob("forecast_*.csv"))
    ok_fwd = bool(fwd) and (lambda f: f["ds"].min() == w["FORECAST_START"] and f["ds"].max() == w["FORECAST_END"])(pd.read_csv(fwd[-1]))
    record("1", "windows", ok and ok_tv and ok_fwd,
           f"anchor {man['anchor']}; train<= {w['TRAIN_END']}, test {w['TEST_START']}..{w['TEST_END']}, "
           f"forecast {w['FORECAST_START']}..{w['FORECAST_END']}; offsets ok={ok}, "
           f"production test window ok={ok_tv}, production forecast window ok={ok_fwd}")

    # 2 ── grid ───────────────────────────────────────────────────────────────
    dup = int(train.duplicated(["series_id", "ds"]).sum())
    not_ms = int((pd.to_datetime(train["ds"]).dt.day != 1).sum())
    te = common.month_ts(w["TRAIN_END"])
    bad_grid, bad_end, bad_start = [], [], []
    first_sale = idx.set_index("series_id")["first_sale"].to_dict()
    for key, g in train.groupby("series_id"):
        d = pd.to_datetime(g["ds"]).sort_values()
        if len(d) > 1 and not (d.diff().dropna().dt.days.between(28, 31)).all():
            bad_grid.append(key)
        if d.iloc[-1] != te:
            bad_end.append(key)
        if d.iloc[0].strftime("%Y-%m") != first_sale.get(key):
            bad_start.append(key)
        expected = pd.date_range(d.iloc[0], te, freq="MS")
        if len(expected) != len(d):
            bad_grid.append(key)
    record("2", "grid", dup == 0 and not_ms == 0 and not bad_grid and not bad_end and not bad_start,
           f"{train['series_id'].nunique()} series, {len(train):,} rows | duplicate keys {dup} | "
           f"non-month-start {not_ms} | gaps {len(set(bad_grid))} | not ending at TRAIN_END {len(bad_end)} | "
           f"start != first sale {len(bad_start)}")

    # 3 ── values ─────────────────────────────────────────────────────────────
    y = train["y"]
    record("3", "values", bool(pd.api.types.is_float_dtype(y) and np.isfinite(y).all() and (y >= 0).all()
                               and train["series_id"].notna().all()),
           f"dtype {y.dtype}; nulls {int(y.isna().sum())}; negatives {int((y < 0).sum())}; "
           f"zero-filled months {int((y == 0).sum()):,} of {len(y):,} ({(y == 0).mean():.1%})")

    # 4 ── coverage ───────────────────────────────────────────────────────────
    n_items = len(routing)
    routed_keys = set(routing["family_key"].astype(str).str.strip())
    rec_ok = (n_items == man["counts"]["routed_items"] == int(idx["n_items"].sum())
              and routed_keys == set(idx["series_id"]) and idx["series_id"].is_unique
              and set(train["series_id"]) == set(idx.loc[idx["has_train_data"], "series_id"])
              and set(test["series_id"]) == set(idx["series_id"]))
    no_train = idx[~idx["has_train_data"]]
    reasons = no_train["block_reason"].replace("", "(none)").value_counts().to_dict()
    record("4", "coverage", rec_ok,
           f"{n_items} items -> {len(idx)} fit keys; with train data {int(idx['has_train_data'].sum())}, "
           f"without {len(no_train)} {reasons}")

    # 5 ── pooling ────────────────────────────────────────────────────────────
    pooled = idx[idx["is_pooled"] & idx["has_train_data"]]
    bad_pool = []
    grid_cache = {k: g.set_index(pd.to_datetime(g["ds"]))["y"] for k, g in train.groupby("series_id")}
    for _, r in pooled.iterrows():
        members = r["members"].split("|")
        s = grid_cache[r["series_id"]]
        m = im[im["item_code"].isin(members) & (im["ds"] <= te)].groupby("ds")["y"].sum()
        m = m.reindex(s.index).fillna(0.0)
        if not np.allclose(s.values, m.values, atol=1e-6):
            bad_pool.append(r["series_id"])
    sr = ref["split_ratios"]
    sums = sr.groupby("family_key")[["test_ratio", "forward_ratio"]].sum()
    bad_ratio = sums[~(np.isclose(sums["test_ratio"], 1.0, atol=1e-6) & np.isclose(sums["forward_ratio"], 1.0, atol=1e-6))]
    record("5", "pooling", not bad_pool and bad_ratio.empty,
           f"{len(pooled)} pooled series equal the sum of members (mismatches {len(bad_pool)}); "
           f"{len(sums)} families' split ratios sum to 1.0 on both bases (violations {len(bad_ratio)})")

    # 6 ── amazon / conservation ──────────────────────────────────────────────
    raw = pd.read_csv(run / "raw_data.csv", dtype=str)
    raw["monthly_qty"] = pd.to_numeric(raw["monthly_qty"])
    is_amz = raw["channel"].astype(str).str.strip().isin(set(config.AMAZON_CHANNELS))
    hash_ok = all(common.sha256_of(run / n) == h for n, h in man["source_inputs_sha256"].items())
    in_win = (raw["year_month"] >= config.TRAIN_START) & (raw["year_month"] <= w["TEST_END"])
    raw_total = float(raw.loc[~is_amz & in_win, "monthly_qty"].sum())
    snap_total = float(im["y"].sum())
    excl_rows = int(is_amz.sum())
    excl_file = len(pd.read_csv(arm_a / "excluded_amazon_rows.csv")) if (arm_a / "excluded_amazon_rows.csv").exists() else -1
    record("6", "amazon / conservation",
           hash_ok and abs(raw_total - snap_total) < 1e-6 and excl_rows == man["counts"]["excluded_amazon_rows"] == excl_file,
           f"inputs unchanged since export {hash_ok}; non-Amazon qty {snap_total:,.0f} vs raw {raw_total:,.0f}; "
           f"Amazon rows removed {excl_rows} (run file {excl_file}, manifest {man['counts']['excluded_amazon_rows']})")

    # 7 ── leakage ────────────────────────────────────────────────────────────
    ts = common.month_ts(w["TEST_START"])
    leak = int((pd.to_datetime(train["ds"]) >= ts).sum())
    t_ok = bool(pd.to_datetime(test["ds"]).between(ts, common.month_ts(w["TEST_END"])).all())
    files = sorted(p.name for p in (snap / "train").iterdir())
    record("7", "leakage", leak == 0 and t_ok and files == ["MLTable", "train.parquet"],
           f"train rows in/after TEST_START {leak}; test actuals confined to test window {t_ok}; train/ contains {files}")

    # 8 ── routing reproduction ───────────────────────────────────────────────
    prod = pd.read_csv(fwd[-1], dtype={"item_code": str}).drop_duplicates(["item_code", "model"])
    prod_mix = prod["model"].value_counts().to_dict()
    mine = routing[routing["route"] != "Blocked"]
    my_mix = mine["route"].value_counts().to_dict()
    sets_ok = set(mine["item_code"]) == set(prod["item_code"]) == set(tv["item_code"])
    by_model_ok = all(set(mine.loc[mine["route"] == m, "item_code"]) == set(prod.loc[prod["model"] == m, "item_code"])
                      for m in my_mix)
    record("8", "routing reproduces production", sets_ok and by_model_ok and prod_mix == my_mix,
           f"mine {my_mix} (+{int((routing['route'] == 'Blocked').sum())} blocked) vs production {prod_mix}; "
           f"item sets identical {sets_ok}, per-model sets identical {by_model_ok}")

    # 9 ── mltable round trip + hashes ────────────────────────────────────────
    try:
        import mltable
        back = mltable.load(str(snap / "train")).to_pandas_dataframe()
        a_ = back.sort_values(["series_id", "ds"]).reset_index(drop=True)
        b_ = train.sort_values(["series_id", "ds"]).reset_index(drop=True)
        rt_ok = (len(a_) == len(b_) and list(a_.columns) == list(b_.columns)
                 and a_["series_id"].equals(b_["series_id"]) and np.allclose(a_["y"], b_["y"])
                 and (pd.to_datetime(a_["ds"]).values == pd.to_datetime(b_["ds"]).values).all()
                 and str(a_["y"].dtype) == str(b_["y"].dtype))
        rt_detail = f"MLTable loads {len(back):,} rows, columns {list(back.columns)}, y dtype {back['y'].dtype}"
    except Exception as exc:  # noqa: BLE001
        rt_ok, rt_detail = False, f"MLTable load failed: {type(exc).__name__}: {str(exc)[:160]}"
    bad_hash = [n for n, m in man["files"].items() if common.sha256_of(snap / n) != m["sha256"]]
    record("9", "mltable round trip + hashes", rt_ok and not bad_hash,
           f"{rt_detail}; files whose hash differs from manifest: {bad_hash or 'none'}")

    # 10a ── stand-in naive == production naive ───────────────────────────────
    preds = sf.standin_predictions(snap)
    item_pred = sp.to_item_grain(preds, ref)
    item_pred["ds"] = pd.to_datetime(item_pred["ds"])
    tv2 = tv.copy(); tv2["ds"] = pd.to_datetime(tv2["ds"] + "-01")
    diffs, compared = [], 0
    for route, mname in (("Naive-3mo", "standin_naive3"), ("Naive-12mo", "standin_naive12")):
        prod_rows = tv2[tv2["model"] == route][["item_code", "ds", "yhat"]]
        mine_rows = item_pred[item_pred["model"] == mname][["item_code", "ds", "yhat"]]
        j = prod_rows.merge(mine_rows, on=["item_code", "ds"], how="left", suffixes=("_prod", "_mine"))
        compared += len(j)
        bad = j[(j["yhat_mine"].isna()) | ((j["yhat_prod"] - j["yhat_mine"]).abs() > 0.011)]
        diffs.append((route, len(j), len(bad), bad.head(3)))
    record("10a", "stand-in naive == production naive",
           all(d[2] == 0 for d in diffs),
           "; ".join(f"{r}: {n} item-months compared, {b} differ" for r, n, b, _ in diffs))

    # 10b ── scorer vs production benchmark ───────────────────────────────────
    bench = pd.read_csv(arm_a / "benchmark_comparison.csv", dtype={"item_code": str})
    scored_a = sp.score_item_frame(tv, ref)
    card_a = sp.scorecard(scored_a)
    cmp_rows, ok_b = [], True
    for model in sorted(bench["model"].unique()):
        b = bench[(bench["model"] == model)].dropna(subset=["actual", "model_forecast", "prior_year_forecast"])
        e = (b["model_forecast"] - b["actual"]).abs().sum() / b["actual"].abs().sum() * 100
        pe = (b["prior_year_forecast"] - b["actual"]).abs().sum() / b["actual"].abs().sum() * 100
        mine_row = card_a[(card_a["model"] == model) & (card_a["slice"] == "overall")].iloc[0]
        match = (len(b) == mine_row["n_lfl"] and abs(e - mine_row["wape_lfl"]) < 0.06
                 and abs(pe - mine_row["wape_prior_year_lfl"]) < 0.06)
        ok_b &= bool(match)
        cmp_rows.append(f"{model}: n {len(b)}/{int(mine_row['n_lfl'])}, WAPE {e:.1f}/{mine_row['wape_lfl']}, "
                        f"baseline {pe:.1f}/{mine_row['wape_prior_year_lfl']}")
    record("10b", "scorer == production benchmark (like-for-like)", ok_b,
           " | ".join(cmp_rows) + "   [benchmark file / my scorer]")

    # ── report ───────────────────────────────────────────────────────────────
    width = max(len(r[1]) for r in results)
    print()
    for cid, name, status, detail in results:
        print(f"[{status}] {cid:>3}  {name:<{width}}  {detail}")
    n_fail = sum(1 for r in results if r[2] == "FAIL")
    print(f"\n{len(results) - n_fail}/{len(results)} checks passed" + (f" — {n_fail} FAILED" if n_fail else ""))
    if a.json_out:
        a.json_out.parent.mkdir(parents=True, exist_ok=True)
        a.json_out.write_text(json.dumps([dict(id=r[0], name=r[1], status=r[2], detail=r[3]) for r in results], indent=2))
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
