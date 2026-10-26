import numpy as np
import pandas as pd
import pytest

from order_forecast.automl_export import build_tables, write_export
from order_forecast.cli import main
from order_forecast.score_external import compare, external_predictions
from order_forecast.snapshot import Snapshot, save_snapshot


def make_snap(n=24):
    months = pd.date_range("2024-01-01", periods=n, freq="MS")
    rows, inv = [], []
    for asin, start in (("A1", 0), ("A2", 0), ("SHORT", n - 5)):
        for i, m in enumerate(months):
            if i >= start:
                rows.append(dict(asin=asin, month_start=m, ordered_units=10 + i, shipped_units=9 + i))
    sales = pd.DataFrame(rows)
    inventory = sales.assign(net_received_units=8.0)[["asin", "month_start", "net_received_units"]]
    invoices = pd.DataFrame({"marketplace": "US", "product_code": "D1", "month_start": months, "gross_units": 30.0, "credit_units": 0.0})
    return Snapshot("US", sales, inventory, invoices, {})


def test_export_never_contains_holdout_actuals():
    snap = make_snap()
    t = build_tables(snap, horizon=6)
    assert t.train["ds"].max() == t.train_end == pd.Timestamp("2025-06-01")
    assert "SHORT" in t.too_short and set(t.train.series_id) == {"A1", "A2"}
    assert (t.horizon["y"] == 0).all() and t.horizon["ds"].min() > t.train_end
    assert len(t.actuals) == 2 * 6 and t.actuals.actual.min() > 0     # real values kept locally only


def test_write_export_files(tmp_path):
    t = build_tables(make_snap(), horizon=6)
    d = write_export(t, "US", tmp_path, "ordered_units", 6, 12)
    for f in ("train/train.parquet", "train/MLTable", "horizon/horizon.parquet", "horizon/MLTable", "holdout_actuals.csv", "manifest.json"):
        assert (d / f).exists()
    assert "read_parquet" in (d / "train/MLTable").read_text()
    assert pd.read_parquet(d / "horizon/horizon.parquet")["y"].eq(0).all()


def test_too_short_history_raises():
    with pytest.raises(ValueError):
        build_tables(make_snap(10), horizon=6)


def test_score_external_matches_baselines_on_same_series():
    snap = make_snap()
    t = build_tables(snap, horizon=6)
    raw = t.horizon.rename(columns={"y": "y_predicted"}).assign(y_predicted=20.0)
    ext = external_predictions(raw, snap, 6, "automl")
    r = compare(snap, ext, "ordered_units", 6, 12)
    assert r["series_compared"] == 2 and "automl" in set(r["scores"]["method"])
    assert "mean12" in set(r["scores"]["method"])


def test_job_dry_run_and_concurrency_guard(tmp_path):
    pytest.importorskip("azure.ai.ml")
    snap = make_snap()
    d = write_export(build_tables(snap, horizon=6), "US", tmp_path, "ordered_units", 6, 12)
    assert main(["automl-job", "--export", str(d), "--dry-run"]) == 0
    with pytest.raises(SystemExit):
        main(["automl-job", "--export", str(d), "--dry-run", "--concurrent", "9"])
