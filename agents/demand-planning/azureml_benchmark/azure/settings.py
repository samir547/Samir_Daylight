"""Azure connection + job settings, read from environment variables (nothing secret in git).

    AZUREML_SUBSCRIPTION_ID   AZUREML_RESOURCE_GROUP   AZUREML_WORKSPACE (default Daylight-az-ml-workspace)
    AZUREML_COMPUTE           name of the CPU cluster (default 'automl-cpu'; created by create_compute.py)
"""
from __future__ import annotations

import os

WORKSPACE = os.environ.get("AZUREML_WORKSPACE", "Daylight-az-ml-workspace")
COMPUTE = os.environ.get("AZUREML_COMPUTE", "automl-cpu")
VM_SIZE = "Standard_D4ds_v5"          # matches the approved DDSv5 quota; ~USD 0.23 / node-hour
DATA_ASSET = "daylight-demand-train"  # versioned per anchor month, e.g. version "2026-09"
HORIZON_ASSET = "daylight-demand-horizon"
HORIZON = 6
FREQUENCY = "MS"


def ml_client():
    from azure.ai.ml import MLClient
    from azure.identity import DefaultAzureCredential
    sub, rg = os.environ.get("AZUREML_SUBSCRIPTION_ID"), os.environ.get("AZUREML_RESOURCE_GROUP")
    if not sub or not rg:
        raise SystemExit("Set AZUREML_SUBSCRIPTION_ID and AZUREML_RESOURCE_GROUP (and run `az login`).")
    return MLClient(DefaultAzureCredential(), sub, rg, WORKSPACE)
