"""
Does training to the anchor actually beat training six months earlier?

Read-only analysis, never part of the cycle. For each case it runs main.py twice
on the same cached inputs, once in development mode and once with --production,
then scores both forecasts of the same months against the newest extract. Both
forecast anchor+1..anchor+6, so the only difference is the training cut-off:
horizon 7-12 against horizon 1-6.

Two kinds of case:

  real       An extract as it was pulled, with its own active list and successor
             map. Honest, but the recent ones have few months of actuals to
             score, and the newest has none. The BDM sheet comes from the newest
             extract, because the older files predate the multi-year layout and
             --cached would try to re-pull them. It does not affect the forecast:
             the active list has its own file, and BDM only supplies ratings and
             the benchmark.
  truncated  The newest extract cut off at the anchor. Covers peak season and all
             six months, but the active list and successor map are today's, so
             both modes benefit from hindsight about which items matter. That
             affects scope, not the comparison between the two modes.

Run:  python -m analysis.dev_vs_production_backtest --cache-root <prophet_forecast
      folder> --work-dir <scratch folder>

--cache-root holds output/runs/2026-09 and the dated output_* extracts. Each run
takes about three minutes; --workers sets how many run at once. Finished runs are
reused, so an interrupted back-test can be restarted.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from evaluation.delivered_accuracy import item_actuals  # noqa: E402

INPUTS = ["raw_data.csv", "bdm_forecasts.csv", "active_products.csv",
          "master_product.csv", "successor_map.csv"]
NEWEST = "output/runs/2026-09"
LAST_ACTUAL_MONTH = "2026-09"     # the newest extract's latest complete month

# label, anchor, source folder, truncate the extract at the anchor?
CASES = [
    ("truncated 2025-09", "2025-09", NEWEST, True),
    ("truncated 2025-12", "2025-12", NEWEST, True),
    ("truncated 2026-03", "2026-03", NEWEST, True),
    ("real 2026-06", "2026-06", "output_family_grouping", False),
    ("real 2026-07", "2026-07", "output_20082026", False),
    ("real 2026-08", "2026-08", "output_04092026", False),
    ("real 2026-09", "2026-09", NEWEST, False),
]

# The development window only rolls past config.TEST_END, so set it one month
# behind the anchor in memory. Nothing on disk changes.
DEV_RUNNER = (
    "import sys, config, main; "
    "config.TEST_END = config.shift_month(sys.argv[1], -1); "
    "sys.argv = ['main.py', '--cached', '--auto-window']; "
    "sys.exit(main.main())"
)


def stage(case: tuple, mode: str, cache_root: Path, work_dir: Path) -> Path:
    """Copy one case's inputs into its own folder and return the folder."""
    label, anchor, source, truncate = case
    folder = work_dir / f"{label.replace(' ', '_')}_{mode}"
    folder.mkdir(parents=True, exist_ok=True)
    for name in INPUTS:
        origin = NEWEST if name == "bdm_forecasts.csv" else source
        shutil.copy(cache_root / origin / name, folder / name)
    if truncate:
        raw = pd.read_csv(folder / "raw_data.csv", dtype=str)
        raw[raw["year_month"] <= anchor].to_csv(folder / "raw_data.csv", index=False)
    return folder


def forecast_path(folder: Path, anchor: str) -> Path:
    return folder / (f"forecast_{config.shift_month(anchor, 1)}_"
                     f"{config.shift_month(anchor, 6)}.csv")


def run_case(case: tuple, mode: str, cache_root: Path, work_dir: Path) -> None:
    anchor = case[1]
    folder = work_dir / f"{case[0].replace(' ', '_')}_{mode}"
    if forecast_path(folder, anchor).exists():
        return
    folder = stage(case, mode, cache_root, work_dir)
    repo = Path(__file__).resolve().parent.parent
    cmd = ([sys.executable, "-c", DEV_RUNNER, anchor] if mode == "development"
           else [sys.executable, "main.py", "--cached", "--production"])
    env = {**os.environ, "FORECAST_OUTPUT_DIR": str(folder), "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONIOENCODING": "utf-8"}
    with open(folder / "run.log", "w", encoding="utf-8") as log:
        result = subprocess.run(cmd, cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        raise RuntimeError(f"{case[0]} {mode} exited {result.returncode}; see {folder}/run.log")
    if not forecast_path(folder, anchor).exists():
        raise RuntimeError(f"{case[0]} {mode} did not forecast from anchor {anchor}; "
                           f"see {folder}/run.log")


def score(forecast: pd.DataFrame, actuals: pd.DataFrame) -> dict:
    df = forecast.merge(actuals, on=["item_code", "ds"], how="left")
    df["actual"] = df["actual"].fillna(0.0)
    total = df["actual"].sum()
    return {
        "wape_pct": round((df["yhat"] - df["actual"]).abs().sum() / total * 100, 1),
        "bias_pct": round((df["yhat"].sum() - total) / total * 100, 1),
        "actual": round(total),
    }


def compare(case: tuple, work_dir: Path, actuals: pd.DataFrame) -> dict | None:
    """Both modes on the items they share, over months that have actuals."""
    label, anchor = case[0], case[1]
    frames = {}
    for mode in ("development", "production"):
        folder = work_dir / f"{label.replace(' ', '_')}_{mode}"
        frame = pd.read_csv(forecast_path(folder, anchor))
        frame["item_code"] = frame["item_code"].astype(str).str.strip()
        frame["ds"] = frame["ds"].astype(str).str[:7]
        frames[mode] = frame[["item_code", "ds", "yhat"]]

    dev, prod = frames["development"], frames["production"]
    items_dev, items_prod = dev["item_code"].nunique(), prod["item_code"].nunique()
    common = set(dev["item_code"]) & set(prod["item_code"])
    months = sorted(m for m in dev["ds"].unique() if m <= LAST_ACTUAL_MONTH)
    if not months:
        return {"case": label, "months": 0, "items_dev": items_dev, "items_prod": items_prod}

    dev = dev[dev["item_code"].isin(common) & dev["ds"].isin(months)]
    prod = prod[prod["item_code"].isin(common) & prod["ds"].isin(months)]
    a, b = score(dev, actuals), score(prod, actuals)

    def zero_items(frame):
        return int((frame.groupby("item_code")["yhat"].sum() == 0).sum())

    return {
        "case": label, "months": len(months), "items_dev": items_dev, "items_prod": items_prod,
        "items_scored": len(common), "actual": a["actual"],
        "wape_dev": a["wape_pct"], "wape_prod": b["wape_pct"],
        "wape_gain": round(a["wape_pct"] - b["wape_pct"], 1),
        "bias_dev": a["bias_pct"], "bias_prod": b["bias_pct"],
        "zero_items_dev": zero_items(dev), "zero_items_prod": zero_items(prod),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Development vs production accuracy")
    ap.add_argument("--cache-root", type=Path, required=True)
    ap.add_argument("--work-dir", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()

    jobs = [(case, mode) for case in CASES for mode in ("development", "production")]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for future in [pool.submit(run_case, c, m, args.cache_root, args.work_dir)
                       for c, m in jobs]:
            future.result()

    raw = pd.read_csv(args.cache_root / NEWEST / "raw_data.csv", dtype=str)
    actuals = item_actuals(raw).rename(columns={"year_month": "ds"})
    table = pd.DataFrame([compare(c, args.work_dir, actuals) for c in CASES])
    path = args.work_dir / "dev_vs_production.csv"
    table.to_csv(path, index=False)

    pd.set_option("display.width", 200, "display.max_columns", 30)
    print(table.to_string(index=False))
    scored = table[table["months"] > 0].copy()
    weights = scored["actual"]
    print(f"\nUnit-weighted WAPE: development "
          f"{np.average(scored['wape_dev'], weights=weights):.1f}%, production "
          f"{np.average(scored['wape_prod'], weights=weights):.1f}%")
    print(f"Unit-weighted bias: development "
          f"{np.average(scored['bias_dev'], weights=weights):.1f}%, production "
          f"{np.average(scored['bias_prod'], weights=weights):.1f}%")
    print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
