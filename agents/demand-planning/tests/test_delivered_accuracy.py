import pandas as pd
import pytest

from evaluation import delivered_accuracy as da
from tests.conftest import make_raw

AS_OF = pd.Timestamp("2026-11-05")
TODAY = pd.Timestamp("2026-11-05")
SUCCESSORS = pd.DataFrame({"old_item_code": ["D0"], "new_item_code": ["D1"],
                           "status": ["Replaced"], "estimated_changeover": ["2025-01-01"]})
NO_SUCCESSORS = SUCCESSORS.iloc[0:0]


def batch(run_date, first="2026-09", items=("D1",), yhat=8.0):
    months = pd.period_range(first, periods=2, freq="M").strftime("%Y-%m")
    return pd.DataFrame([
        {"item_code": item, "family": "F", "year_month": month, "yhat": yhat,
         "yhat_lower": 1.0, "yhat_upper": 20.0, "model": "Prophet",
         "run_date": pd.Timestamp(run_date)}
        for item in items for month in months
    ])


def score(forecast, raw=None, successors=NO_SUCCESSORS, dev_dates=()):
    raw = make_raw("2026-10", overrides={"2026-10": 20}) if raw is None else raw
    return da.score_batches(forecast, raw, successors, pd.Series(pd.to_datetime(list(dev_dates))),
                            AS_OF, today=TODAY)


def test_production_batch_has_horizon_from_one():
    out = score(batch("2026-10-08"))
    assert out["training_basis"].eq("production").all()
    assert out.sort_values("year_month")["horizon"].tolist() == [1, 2]


def test_development_batch_is_identified_by_its_accuracy_rows():
    out = score(batch("2026-10-08"), dev_dates=["2026-10-08"])
    assert out["training_basis"].eq("development").all()
    assert out.sort_values("year_month")["horizon"].tolist() == [7, 8]


def test_partial_month_is_kept_but_not_complete():
    out = score(batch("2026-10-08")).set_index("year_month")
    assert bool(out.loc["2026-09", "month_complete"]) is True
    assert bool(out.loc["2026-10", "month_complete"]) is False


def test_month_the_extract_has_not_reached_is_left_out():
    out = score(batch("2026-10-08", first="2026-10"))
    assert out["year_month"].tolist() == ["2026-10"]


def test_no_sale_scores_an_actual_of_zero():
    out = score(batch("2026-10-08", items=("Z9",)))
    assert out["actual"].eq(0).all()


def test_amazon_sales_do_not_count():
    raw = make_raw("2026-10", overrides={"2026-10": 20})
    extra = raw[(raw.item_code == "D1") & (raw.year_month == "2026-09")].assign(
        channel="Amazon", monthly_qty="500")
    out = score(batch("2026-10-08"), raw=pd.concat([raw, extra])).set_index("year_month")
    assert out.loc["2026-09", "actual"] == 10


def test_republished_month_scores_only_the_newest_batch():
    forecast = pd.concat([batch("2026-10-01", yhat=1.0), batch("2026-10-08", yhat=2.0)])
    out = score(forecast)
    assert out["run_date"].eq(pd.Timestamp("2026-10-08")).all()
    assert out["yhat"].eq(2.0).all()


def test_retired_code_sales_are_added_for_family_totals():
    raw = make_raw("2026-10", overrides={"2026-10": 20})
    retired = raw[raw.item_code == "D1"].assign(item_code="D0", monthly_qty="3")
    out = score(batch("2026-10-08"), raw=pd.concat([raw, retired]), successors=SUCCESSORS)

    row = out[out.is_retired_code & (out.year_month == "2026-09")].iloc[0]
    assert (row.item_code, row.family_key, row.model) == ("D0", "D0", da.RETIRED_MODEL)
    assert (row.yhat, row.actual) == (0.0, 3.0)
    assert out[out.year_month == "2026-09"].groupby("family_key").actual.sum()["D0"] == 13


def test_retired_rows_do_not_affect_the_item_summary():
    raw = make_raw("2026-10", overrides={"2026-10": 20})
    retired = raw[raw.item_code == "D1"].assign(item_code="D0", monthly_qty="3")
    out = score(batch("2026-10-08"), raw=pd.concat([raw, retired]), successors=SUCCESSORS)

    summary = da.summarise(out).iloc[0]
    assert summary["actual"] == 10
    assert summary["wape_pct"] == pytest.approx(20.0)
    assert summary["bias_pct"] == pytest.approx(-20.0)


def test_rows_are_unique_on_the_table_key():
    raw = make_raw("2026-10", overrides={"2026-10": 20})
    retired = raw[raw.item_code == "D1"].assign(item_code="D0", monthly_qty="3")
    forecast = pd.concat([batch("2026-09-05", first="2026-08"), batch("2026-10-08")])
    out = score(forecast, raw=pd.concat([raw, retired]), successors=SUCCESSORS)
    assert not out.duplicated(["run_date", "item_code", "year_month"]).any()
