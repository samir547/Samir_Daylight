"""Build (and optionally submit) the AutoML forecasting job -- arm B, one GLOBAL model across all series.

    python -m azureml_benchmark.azure.submit_automl <snapshot_dir> --dry-run            # offline: build + dump job YAML
    python -m azureml_benchmark.azure.submit_automl <snapshot_dir> --validate           # server-side validation, no compute used
    python -m azureml_benchmark.azure.submit_automl <snapshot_dir> --smoke 20 --submit  # 20-series cheap test run
    python -m azureml_benchmark.azure.submit_automl <snapshot_dir> --submit             # full run

Spend guards are always on: max_trials, a total timeout, and max_concurrent_trials <= cluster nodes.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from azure.ai.ml import Input, automl
from azure.ai.ml.constants import AssetTypes

from azureml_benchmark import common
from azureml_benchmark.azure import settings


def build_job(version: str, *, dnn: bool, trials: int, timeout_min: int, concurrent: int,
              exp: str, smoke: int | None):
    job = automl.forecasting(
        compute=settings.COMPUTE,
        experiment_name=exp,
        training_data=Input(type=AssetTypes.MLTABLE, path=f"azureml:{settings.DATA_ASSET}:{version}"),
        test_data=Input(type=AssetTypes.MLTABLE, path=f"azureml:{settings.HORIZON_ASSET}:{version}"),
        target_column_name="y",
        primary_metric="normalized_root_mean_squared_error",
        n_cross_validations=3,
        enable_model_explainability=False,
        forecasting_settings=dict(
            time_column_name="ds", time_series_id_column_names=["series_id"],
            forecast_horizon=settings.HORIZON, frequency=settings.FREQUENCY,
            short_series_handling_config="auto", target_aggregate_function="sum"),
    )
    job.set_limits(timeout_minutes=timeout_min, trial_timeout_minutes=max(10, timeout_min // 4),
                   max_trials=trials, max_concurrent_trials=concurrent, enable_early_termination=True)
    job.set_training(enable_dnn_training=dnn, enable_stack_ensemble=False, enable_vote_ensemble=True)
    job.display_name = f"automl-global-{version}" + ("-dnn" if dnn else "")
    return job


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("snapshot_dir", type=Path)
    ap.add_argument("--dnn", action="store_true", help="also try TCNForecaster (slower)")
    ap.add_argument("--trials", type=int, default=25)
    ap.add_argument("--timeout-min", type=int, default=90)
    ap.add_argument("--concurrent", type=int, default=4)
    ap.add_argument("--smoke", type=int, help="use only N series (separate small data asset must be registered)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--submit", action="store_true")
    a = ap.parse_args(argv)
    version = common.read_manifest(a.snapshot_dir.resolve())["anchor"]
    if a.smoke:
        version = f"{version}-smoke{a.smoke}"
    job = build_job(version, dnn=a.dnn, trials=a.trials, timeout_min=a.timeout_min,
                    concurrent=a.concurrent, exp="daylight-benchmark", smoke=a.smoke)
    if a.dry_run or not (a.validate or a.submit):
        out = Path("results") / f"{job.display_name}.job.yml"
        out.parent.mkdir(exist_ok=True)
        job.dump(str(out))
        print(out.read_text())
        return
    c = settings.ml_client()
    if a.validate:
        r = c.jobs.validate(job)
        print("validation:", "OK" if r.passed else r.error_messages)
    if a.submit:
        s = c.jobs.create_or_update(job)
        print("submitted", s.name, s.studio_url)


if __name__ == "__main__":
    main()
