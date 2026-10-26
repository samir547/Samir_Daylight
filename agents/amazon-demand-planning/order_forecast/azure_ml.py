"""Azure ML side: register the exported data and build or submit the AutoML forecasting job.

Settings come from environment variables (nothing secret in git):
    AZUREML_SUBSCRIPTION_ID  (required)   AZUREML_RESOURCE_GROUP (default rg-daylight-ml)
    AZUREML_WORKSPACE (default Daylight-az-ml-workspace)
    AZUREML_COMPUTE   (default cpu-cluster)    AZUREML_MAX_NODES (default 2)
Sign-in is DefaultAzureCredential: `az login` on a laptop, the VM's managed identity on the VM.

Spend guards are always on: max_trials, a total timeout, and concurrency capped at the
cluster's node limit. The Azure SDK is imported lazily, so the rest of the package and the
tests work without it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

WORKSPACE = os.environ.get("AZUREML_WORKSPACE", "Daylight-az-ml-workspace")
RESOURCE_GROUP = os.environ.get("AZUREML_RESOURCE_GROUP", "rg-daylight-ml")
COMPUTE = os.environ.get("AZUREML_COMPUTE", "cpu-cluster")
MAX_NODES = int(os.environ.get("AZUREML_MAX_NODES", "2"))
FREQUENCY = "MS"

METRICS = ("normalized_mean_absolute_error", "normalized_root_mean_squared_error")
VALID_MODELS = ("AutoArima", "Prophet", "Naive", "SeasonalNaive", "Average", "SeasonalAverage", "ExponentialSmoothing",
                "Arimax", "TCNForecaster", "ElasticNet", "GradientBoosting", "DecisionTree", "KNN", "LassoLars", "SGD",
                "RandomForest", "ExtremeRandomTrees", "LightGBM", "XGBoostRegressor")


def parse_models(text: str) -> list[str]:
    names = [t.strip() for t in text.split(",") if t.strip()]
    bad = [n for n in names if n not in VALID_MODELS]
    if bad:
        raise SystemExit(f"Unknown AutoML model(s) {bad}. Valid: {', '.join(VALID_MODELS)}")
    return names


PRESETS = {
    # classical per-series models that carry a seasonal pattern
    "seasonal": ["SeasonalNaive", "SeasonalAverage", "ExponentialSmoothing", "AutoArima", "Prophet"],
    # models that learn across series from engineered features
    "learned": ["LightGBM", "XGBoostRegressor", "RandomForest", "ExtremeRandomTrees", "ElasticNet",
                "GradientBoosting", "LassoLars", "DecisionTree", "KNN", "SGD"],
    "prophet": ["Prophet"],
}


def read_manifest(export_dir: Path) -> dict:
    return json.loads((Path(export_dir) / "manifest.json").read_text())


def asset_names(pool: str) -> tuple[str, str]:
    p = pool.lower()
    return f"amazon-order-train-{p}", f"amazon-order-horizon-{p}"


def _subscription_from_vm() -> str | None:
    """On an Azure VM the instance metadata service knows the subscription; elsewhere this returns None."""
    import urllib.request
    try:
        req = urllib.request.Request(
            "http://169.254.169.254/metadata/instance/compute/subscriptionId?api-version=2021-02-01&format=text",
            headers={"Metadata": "true"})
        return urllib.request.urlopen(req, timeout=2).read().decode().strip() or None
    except Exception:
        return None


def ml_client():
    from azure.ai.ml import MLClient
    from azure.identity import DefaultAzureCredential
    sub = os.environ.get("AZUREML_SUBSCRIPTION_ID") or _subscription_from_vm()
    if not sub:
        raise SystemExit("Set AZUREML_SUBSCRIPTION_ID in .env (run `az account show --query id -o tsv`).")
    return MLClient(DefaultAzureCredential(), sub, RESOURCE_GROUP, WORKSPACE)


def register_assets(client, export_dir: Path) -> list[str]:
    from azure.ai.ml.constants import AssetTypes
    from azure.ai.ml.entities import Data
    man = read_manifest(export_dir)
    out = []
    for name, folder in zip(asset_names(man["pool"]), ("train", "horizon")):
        try:   # the version is a hash of the content, so an existing one is identical: reuse it
            a = client.data.get(name=name, version=man["version"])
            out.append(f"{a.name}:{a.version} (already registered)")
            continue
        except Exception:
            pass
        a = client.data.create_or_update(Data(
            name=name, version=man["version"], type=AssetTypes.MLTABLE, path=str(Path(export_dir) / folder),
            description=f"train_end {man['train_end']}, target {man['target']}"))
        out.append(f"{a.name}:{a.version}")
    return out


def build_job(manifest: dict, trials: int = 20, timeout_min: int = 60, concurrent: int = MAX_NODES,
              metric: str = METRICS[0], allow: list[str] | None = None, tag: str = "", dnn: bool = False):
    from azure.ai.ml import Input, automl
    from azure.ai.ml.automl import ForecastingSettings
    from azure.ai.ml.constants import AssetTypes
    if concurrent > MAX_NODES:
        raise SystemExit(f"concurrent={concurrent} exceeds the cluster limit of {MAX_NODES} nodes")
    train_name, horizon_name = asset_names(manifest["pool"])
    v = manifest["version"]
    job = automl.forecasting(
        compute=COMPUTE,
        experiment_name="daylight-amazon-order-forecast",
        training_data=Input(type=AssetTypes.MLTABLE, path=f"azureml:{train_name}:{v}"),
        test_data=Input(type=AssetTypes.MLTABLE, path=f"azureml:{horizon_name}:{v}"),
        target_column_name="y",
        primary_metric=metric,
        n_cross_validations=3,
        enable_model_explainability=False,
        forecasting_settings=ForecastingSettings(
            time_column_name="ds", time_series_id_column_names=["series_id"],
            forecast_horizon=manifest["horizon"], frequency=FREQUENCY,
            short_series_handling_config="auto", target_aggregate_function="sum"),
    )
    job.set_limits(timeout_minutes=timeout_min, trial_timeout_minutes=max(10, timeout_min // 4),
                   max_trials=trials, max_concurrent_trials=concurrent, enable_early_termination=True)
    job.set_training(enable_dnn_training=dnn, enable_stack_ensemble=False, enable_vote_ensemble=True,
                     **({"allowed_training_algorithms": allow} if allow else {}))
    job.display_name = f"amazon-order-{manifest['pool'].lower()}-{v}" + (f"-{tag}" if tag else "")
    return job


def fetch_predictions(client, job_name: str, dest: Path, best: str | None = None) -> Path | None:
    """Download the best trial of a finished AutoML job plus its child runs (the held-out test run
    writes the predictions there) and return the first CSV with series_id, ds and y_predicted.
    Only the best trial is fetched: the other trials have no artifacts and just raise errors."""
    import pandas as pd
    job = client.jobs.get(job_name)
    print(f"{job_name}: status={job.status}")
    if "COMPLETED" not in str(job.status).upper():
        raise SystemExit(f"Job is {job.status}, not Completed; nothing to fetch yet.")
    best = best or (getattr(job, "properties", None) or {}).get("best_child_run_id")
    if not best:
        raise SystemExit("Could not tell which trial was best; pass --best <name> (the one marked Best in the portal).")
    names = [best] + [j.name for j in client.jobs.list(parent_job_name=best)]
    print("Fetching:", ", ".join(names))
    for n in names:
        out = Path(dest) / n
        out.mkdir(parents=True, exist_ok=True)
        try:
            client.jobs.download(name=n, download_path=str(out), all=True)
        except Exception as e:
            print(f"  {n}: download skipped ({type(e).__name__}: {str(e)[:120]})")
    files = sorted(p for p in Path(dest).rglob("*") if p.is_file() and any(n in p.parts for n in names))
    for f in files:
        print(f"  {f.relative_to(dest)}  ({f.stat().st_size:,} bytes)")
    for f in files:
        if f.suffix.lower() != ".csv":
            continue
        try:
            cols = set(pd.read_csv(f, nrows=1).columns)
        except Exception:
            continue
        if {"series_id", "ds", "y_predicted"} <= cols:
            return f
    return None
