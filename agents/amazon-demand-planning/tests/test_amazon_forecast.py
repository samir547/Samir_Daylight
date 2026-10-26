import numpy as np
import pandas as pd

from order_forecast.amazon_forecast import compare, monthly
from order_forecast.snapshot import Snapshot


def weekly(gen="2026-09-27", weeks=48, asin="A1", level=10.0):
    start = pd.Timestamp(gen)
    rows = [(gen, asin, start + pd.Timedelta(days=7 * i), start + pd.Timedelta(days=7 * i + 6), i, level, level, level, level)
            for i in range(weeks)]
    return pd.DataFrame(rows, columns=["forecast_generation_date", "asin", "week_start", "week_end", "weeks_ahead",
                                       "mean_units", "p70_units", "p80_units", "p90_units"])


def test_monthly_uses_saturday_month_and_drops_incomplete_months():
    m = monthly(weekly())
    assert m["month_start"].min() == pd.Timestamp("2026-10-01")
    assert m["month_start"].max() == pd.Timestamp("2027-08-01")     # 48 weeks end Sat 28 Aug 2027, the last Saturday of Aug
    short = monthly(weekly(weeks=46))                                # ends 14 Aug 2027: August is incomplete
    assert short["month_start"].max() == pd.Timestamp("2027-07-01")
    assert m.loc[m.month_start == "2026-10-01", "amazon_mean"].iloc[0] == 50.0   # five Saturdays


def test_compare_aligns_months_and_reports_ratio():
    months = pd.date_range("2023-10-01", "2026-09-01", freq="MS")
    sales = pd.DataFrame({"asin": "A1", "month_start": months, "ordered_units": 10.0, "shipped_units": 10.0})
    snap = Snapshot("US", sales, sales.assign(net_received_units=1.0)[["asin", "month_start", "net_received_units"]],
                    pd.DataFrame(columns=["marketplace", "product_code", "month_start", "gross_units", "credit_units"]), {})
    r = compare(snap, weekly(level=10.0 / 4.33))
    assert r["asins_matched"] == 1 and r["months"][0] == pd.Timestamp("2026-10-01")
    assert (r["monthly_total"]["ratio"] > 0).all()
