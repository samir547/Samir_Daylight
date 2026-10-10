"""
Shared plumbing for the Azure ML benchmark harness.

NO MODELLING LOGIC LIVES HERE. Every step below is a call into the existing
pipeline modules (preprocessing.*, analysis.month_completeness), made in the
same order main.py makes them, so the snapshot a benchmark arm trains on is the
data the production hybrid trained on — by construction, not by re-implementation.

Input is a FROZEN RUN FOLDER: the five cached CSVs main.py writes
(raw_data.csv, bdm_forecasts.csv, active_products.csv, master_product.csv,
successor_map.csv), e.g. output/runs/2026-09/. Building from a frozen folder
needs no database credentials and cannot drift between re-runs.

Run scripts from the agent root so `import config` resolves:
    cd agents/demand-planning
    python -m azureml_benchmark.export_snapshot --run-dir <path-to-run-folder>
"""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

import config  # noqa: E402
from analysis.month_completeness import latest_complete_month  # noqa: E402
from preprocessing import (  # noqa: E402
    demand_classification, family, family_pool, model_routing, prepare, scope,
)

logger = logging.getLogger(__name__)

# The five inputs main.py caches; names are fixed by main.load_all().
RUN_INPUTS = ("raw_data.csv", "bdm_forecasts.csv", "active_products.csv",
              "master_product.csv", "successor_map.csv")

# Files copied verbatim from the run folder as "arm A" (the production hybrid's
# own results for the same windows). Forecast filename depends on the window, so
# it is located by pattern.
ARM_A_FILES = ("test_validation.csv", "benchmark_comparison.csv",
               "model_metrics.csv", "successor_split_ratios.csv",
               "excluded_amazon_rows.csv", "excluded_channel_mismatch.csv")


# ── Small utilities ───────────────────────────────────────────────────────────

def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=AGENT_ROOT,
                             capture_output=True, text=True, timeout=15)
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001 - provenance only, never fatal
        return "unknown"


def git_dirty() -> bool | None:
    try:
        out = subprocess.run(["git", "status", "--porcelain", "--", "."],
                             cwd=AGENT_ROOT, capture_output=True, text=True, timeout=15)
        return bool(out.stdout.strip())
    except Exception:  # noqa: BLE001
        return None


def find_forward_forecast(run_dir: Path) -> Path | None:
    hits = sorted(run_dir.glob("forecast_*.csv"))
    return hits[-1] if hits else None


# ── Frozen run → pipeline state ───────────────────────────────────────────────

@dataclass
class PipelineState:
    run_dir: Path
    anchor: str
    raw: pd.DataFrame                 # all channels, as cached
    raw_nonamazon: pd.DataFrame
    excluded_amazon: pd.DataFrame
    channel_mismatch: set
    prepared: pd.DataFrame            # item grain: item_code, ds, y (non-Amazon)
    successor: pd.DataFrame
    families: family_pool.SuccessorFamilies
    pooled: family_pool.PooledTrainingSet
    classes: pd.DataFrame
    routing: pd.DataFrame             # item grain, 266-ish rows
    series_meta: pd.DataFrame         # item_code -> region
    family_map: pd.Series             # item_code -> product family
    split_ratios: pd.DataFrame


def load_frozen_run(run_dir: Path):
    """Read the five cached inputs exactly the way main._load_one() does."""
    missing = [n for n in RUN_INPUTS if not (run_dir / n).exists()]
    if missing:
        raise FileNotFoundError(f"{run_dir} is missing {missing}")
    raw = pd.read_csv(run_dir / "raw_data.csv", dtype=str)
    bdm = pd.read_csv(run_dir / "bdm_forecasts.csv", dtype=str)
    active = pd.read_csv(run_dir / "active_products.csv", dtype=str)
    master = pd.read_csv(run_dir / "master_product.csv", dtype=str)
    successor = pd.read_csv(run_dir / "successor_map.csv", dtype=str)
    bdm["bdm_forecast_qty"] = pd.to_numeric(bdm["bdm_forecast_qty"], errors="coerce")
    return raw, bdm, active, master, successor


def apply_anchor(raw: pd.DataFrame, expected: str | None = None) -> str:
    """Set the five window constants in memory from the newest COMPLETE month,
    the same way main.apply_auto_window() does (config.py on disk is untouched)."""
    anchor, _ = latest_complete_month(raw)
    if expected and anchor != expected:
        raise ValueError(f"Newest complete month in the run's raw data is {anchor}, "
                         f"but --anchor {expected} was requested.")
    for name, offset in config.OFFSETS_FROM_TEST_END.items():
        setattr(config, name, config.shift_month(anchor, offset))
    logger.info("Window from anchor %s: TRAIN_END=%s TEST=%s..%s FORECAST=%s..%s",
                anchor, config.TRAIN_END, config.TEST_START, config.TEST_END,
                config.FORECAST_START, config.FORECAST_END)
    return anchor


def build_state(run_dir: Path, expected_anchor: str | None = None) -> PipelineState:
    raw, bdm, active, master, successor = load_frozen_run(run_dir)
    anchor = apply_anchor(raw, expected_anchor)

    family_map = family.build_family_map(master)
    series_meta = prepare.series_metadata(raw)          # unfiltered raw, as main.py

    mismatch, _diag = scope.detect_channel_mismatch(raw, config.TEST_END)
    raw_f, excluded_amazon = scope.filter_amazon_channel(raw)
    prepared = prepare.prepare(raw_f)

    families = family_pool.build_successor_families(successor)
    pooled = family_pool.pool_for_training(prepared, families, active, bdm)

    classes = demand_classification.classify_scope(
        pooled.data, pooled.active_products, families, pooled.eligible, mismatch)
    routing = model_routing.route_scope(
        classes, pooled.data, pooled.active_products, families, mismatch,
        fitted_items=model_routing.fitted_items_from_metrics(run_dir),
        successor_map=successor)

    split_ratios = family_pool.compute_split_ratios(families, pooled.eligible, prepared)

    return PipelineState(run_dir=run_dir, anchor=anchor, raw=raw, raw_nonamazon=raw_f,
                         excluded_amazon=excluded_amazon, channel_mismatch=set(mismatch),
                         prepared=prepared, successor=successor, families=families,
                         pooled=pooled, classes=classes, routing=routing,
                         series_meta=series_meta, family_map=family_map,
                         split_ratios=split_ratios)


# ── Snapshot reading (used by checks / stand-in / scorer) ─────────────────────

def read_manifest(snapshot_dir: Path) -> dict:
    return json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))


def month_ts(ym: str) -> pd.Timestamp:
    return pd.Timestamp(ym + "-01")
