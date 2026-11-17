"""Every forecast row says why it has its value (reason_code, reason_detail, confidence)."""
import numpy as np
import pandas as pd

from order_forecast import adjustments as adj
from order_forecast.replenish import (PRIMARY_CODES, REASON_CODES, build_replenish_forecast, confidence, detail,
                                      replay)
from test_replenish import FUTURE, SETTINGS, make_snap, weekly


def _adj(tmp_path, body):
    p = tmp_path / "a.csv"
    p.write_text("marketplace,asin,month_start,type,value,note,entered_by,valid_until\n" + body)
    return adj.load_adjustments(p, "UK", FUTURE[0])


def test_explain_records_do_not_change_the_replay():
    args = (np.array([100.0, 50, 200, 80, 120, 90]), 130.0, 60.0, 1.2, 300.0)
    kw = dict(pinned=np.array([0, 0, 50.0, 0, 0, 0]), stop_from=4, min_order_share=0.25, po_share=0.6)
    k0, n0 = replay(*args, **kw)
    ex = []
    k1, n1 = replay(*args, **kw, explain=ex)
    assert np.array_equal(k0, k1) and np.array_equal(n0, n1) and len(ex) == 6
    assert ex[0]["po_in"] == 36.0 and ex[2]["pinned"] == 50.0


def test_every_row_has_a_known_primary_code_detail_and_confidence():
    f = build_replenish_forecast(make_snap(), SETTINGS, weekly=weekly("R1"), adjustments=adj.empty())
    t = f.table
    assert t["reason_code"].isin(PRIMARY_CODES).all()
    assert t["confidence"].isin(["HIGH", "MEDIUM", "LOW"]).all()
    for _, r in t.iterrows():
        codes = r["reason_detail"].split("|")
        assert codes[0] == r["reason_code"] and set(codes) <= set(REASON_CODES) and len(codes) == len(set(codes))
        assert confidence(codes) == r["confidence"]
    s1 = t[t.asin == "S1"]
    assert (s1["reason_code"] == "SALES_ONLY_NO_STOCK").all() and (s1["confidence"] == "LOW").all()
    # a zero row of a replayed ASIN is explained as covered, and an order row as a reorder
    r1 = t[t.asin == "R1"]
    assert set(r1.loc[r1.units_forecast == 0, "reason_code"]) <= {"NO_ORDER_STOCK_COVERS"}
    assert set(r1.loc[r1.units_forecast > 0, "reason_code"]) <= {"REORDER_TRIGGERED", "REORDER_MIN_ORDER_SIZE",
                                                                  "OPEN_PO_DELIVERY"}


def test_amazon_missing_for_one_asin_is_flagged_on_that_asin_only():
    f = build_replenish_forecast(make_snap(), SETTINGS, weekly=weekly("R1"), adjustments=adj.empty())
    d = f.table.set_index("asin")["reason_detail"]
    assert d.loc["R1"].str.contains("DEMAND_AMAZON_BLEND").any()
    assert d.loc["R2"].str.contains("DEMAND_OWN_NO_AMAZON").all()
    assert not d.loc["R2"].str.contains("DEMAND_AMAZON_BLEND").any()
    # Amazon's forecast for R1 does not change R2's likely case (R2's mean demand is our own either way)
    base = build_replenish_forecast(make_snap(), SETTINGS, weekly=None, adjustments=adj.empty()).table
    assert np.allclose(f.table[f.table.asin == "R2"]["units_forecast"], base[base.asin == "R2"]["units_forecast"])


def test_detail_puts_primary_first_and_orders_the_rest():
    assert detail("REORDER_TRIGGERED", {"HABIT_OWN", "DEMAND_AMAZON_BLEND", "REORDER_TRIGGERED"}) == \
        "REORDER_TRIGGERED|DEMAND_AMAZON_BLEND|HABIT_OWN"
    assert confidence({"REORDER_TRIGGERED", "HABIT_OWN"}) == "HIGH"
    assert confidence({"REORDER_TRIGGERED", "HABIT_BLENDED"}) == "MEDIUM"
    assert confidence({"REORDER_TRIGGERED", "HABIT_POOLED"}) == "LOW"


def test_client_adjustments_show_in_the_codes(tmp_path):
    a = _adj(tmp_path, "UK,R1,2026-12-01,pinned_order,900,bulk,x,\nUK,R2,,discontinue,,delisted,x,\n"
                       "UK,R1,,demand_multiplier,1.5,deal,x,\n")
    t = build_replenish_forecast(make_snap(), SETTINGS, weekly=None, adjustments=a).table.set_index(["asin", "month_start"])
    dec = t.loc[("R1", pd.Timestamp("2026-12-01"))]
    assert "PINNED_ORDER" in dec["reason_detail"] and "CLIENT_ADJUSTED" in dec["reason_detail"]
    assert t.loc["R1", "reason_detail"].str.contains("DEMAND_MULTIPLIER").all()
    assert t.loc["R2", "reason_detail"].str.contains("DISCONTINUED").all()
