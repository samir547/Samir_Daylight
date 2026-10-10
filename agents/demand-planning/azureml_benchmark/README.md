# Azure ML AutoML benchmark harness (non-Amazon items)

Local-only tooling: builds the frozen training/scoring snapshot from a production run folder,
and scores any model's predictions the same way the Prophet/Naive pipeline is scored. No cloud calls.

    python -m azureml_benchmark.export_snapshot --run-dir <runs/YYYY-MM> [--force]
    python -m azureml_benchmark.contract_checks azureml_benchmark/snapshots/<anchor> --run-dir <runs/YYYY-MM>
    python -m azureml_benchmark.score_predictions azureml_benchmark/snapshots/<anchor> --predictions p.parquet --out-dir results/x

- `snapshots/<anchor>/train/` is the MLTable (series_id, ds, y) to register as the Azure ML data asset.
- Predictions must have `series_id, ds, yhat, model` at fit-key grain; pooled families are split back to items with production code.
- Gap months are zero-filled from each series' first sale to TRAIN_END; test actuals keep NaN for no-sale months.
- `actual` follows production (own row, else family actual x split ratio); `actual_own` is the strict view.
- `contract_checks` must be 11/11 PASS before anything is uploaded.
- Run with proxy env vars unset if `mltable` fails (rslex proxy parsing bug).
