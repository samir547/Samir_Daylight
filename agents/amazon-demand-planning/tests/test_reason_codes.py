"""Every reason code can actually occur, and each scenario gets the code it should. Offline fixtures only."""
import numpy as np
import pandas as pd
import pytest

from order_forecast import adjustments as adj
from order_forecast.replenish import PRIMARY_CODES, REASON_CODES, build_replenish_forecast
from order_forecast.series import month_range
from order_forecast.snapshot import Snapshot
from test_replenish import FUTURE, SETTINGS, make_snap, weekly
from test_young import EMPTY_INV, am_for, young_snap


def _adj(tmp_path, body, market="UK"):
    p = tmp_path / f"adj_{abs(hash(body))}.csv"
    p.write_text("marketplace,asin,month_start,type,value,note,entered_by,valid_until\n" + body)
    return adj.load_adjustments(p, market, FUTURE[0])


def _codes(t: pd.DataFrame) -> set:
    return {c for d in t["reason_detail"] for c in d.split("|")}


def _row(t, asin, month):
    return t[(t.asin == asin) & (t.month_start == pd.Timestamp(month))].iloc[0]


def short_market():
    """18 months of history (under 24: flat seasonality). No receipts before the last month, so the market has
    no habits of its own (hard defaults) and no open-PO history (default share). B1 has two past orders."""
    months = month_range("2025-04-01", "2026-09-01")
    rows, inv = [], []
    for a, lvl in (("F1", 30.0), ("F2", 60.0), ("B1", 40.0)):
        for i, m in enumerate(months):
            rows.append(dict(asin=a, month_start=m, ordered_units=lvl, shipped_units=lvl))
            rec = 120.0 if (a == "B1" and i in (5, 11)) else 0.0
            last = i == len(months) - 1
            inv.append(dict(asin=a, month_start=m, net_received_units=rec, sellable_units=lvl * 1.5,
                            open_po_units=(25.0 if last else 0.0)))
    return Snapshot("DE", pd.DataFrame(rows), pd.DataFrame(inv), EMPTY_INV, {"marketplace": "DE"})


@pytest.fixture(scope="module")
def scenarios(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("adj")
    out = {}
    out["plain"] = build_replenish_forecast(make_snap(), SETTINGS, weekly=weekly("R1"), adjustments=adj.empty()).table
    out["client"] = build_replenish_forecast(make_snap(), SETTINGS, weekly=None, adjustments=_adj(tmp,
        "UK,R1,2026-11-01,pinned_order,900,confirmed,x,\n"
        "UK,R1,,stock_override,400,counted,x,\n"
        "UK,R1,,open_po_override,50,seen in VC,x,\n"
        "UK,R1,,cover_override,1.0,agreed,x,\n"
        "UK,R2,2026-12-01,discontinue,,delisted,x,\n"
        "UK,S1,2026-11-01,demand_override,50,promo ends,x,\n"
        "UK,S1,2026-12-01,demand_multiplier,1.5,deal,x,\n")).table
    out["delay"] = build_replenish_forecast(make_snap(), SETTINGS, weekly=None,
                                            adjustments=_adj(tmp, "UK,R1,,delay_orders,2,late,x,\n")).table
    out["amazon_off"] = build_replenish_forecast(make_snap(), {**SETTINGS, "amazon_weight": 0.0}, weekly=weekly("R1"),
                                                 adjustments=adj.empty()).table
    out["short"] = build_replenish_forecast(short_market(), SETTINGS, weekly=None, adjustments=adj.empty()).table
    out["young"] = build_replenish_forecast(young_snap(), SETTINGS, weekly=am_for("Y1"), adjustments=adj.empty()).table
    return out


def test_every_reason_code_occurs_somewhere(scenarios):
    seen = set().union(*(_codes(t) for t in scenarios.values()))
    assert set(REASON_CODES) - seen == set()
    assert seen <= set(REASON_CODES)
    primaries = set().union(*(set(t.reason_code) for t in scenarios.values()))
    assert primaries == set(PRIMARY_CODES)


def test_client_adjustment_codes_land_on_the_right_rows(scenarios):
    t = scenarios["client"]
    oct_, nov = _row(t, "R1", "2026-10-01"), _row(t, "R1", "2026-11-01")
    assert oct_.reason_code == "OPEN_PO_DELIVERY" and oct_.units_forecast == pytest.approx(50.0)   # 400 in stock: no order
    assert nov.reason_code == "PINNED_ORDER" and nov.units_forecast >= 900
    assert {"STOCK_OVERRIDE", "OPEN_PO_OVERRIDE", "HABIT_OVERRIDE", "CLIENT_ADJUSTED"} <= set(oct_.reason_detail.split("|"))
    r2 = t[(t.asin == "R2") & (t.month_start >= pd.Timestamp("2026-12-01"))]
    assert (r2.units_forecast == 0).all() and (r2.reason_code == "NO_ORDER_DISCONTINUED").all()
    assert r2.reason_detail.str.contains("DISCONTINUED").all()
    assert not t[(t.asin == "R2") & (t.month_start < pd.Timestamp("2026-12-01"))].reason_detail.str.contains("|DISCONTINUED", regex=False).any()
    assert "DEMAND_OVERRIDE" in _row(t, "S1", "2026-11-01").reason_detail
    assert "DEMAND_MULTIPLIER" in _row(t, "S1", "2026-12-01").reason_detail
    assert "DEMAND_OVERRIDE" not in _row(t, "S1", "2026-12-01").reason_detail


def test_delayed_orders_are_explained_where_they_were_due_and_where_they_land(scenarios):
    base = build_replenish_forecast(make_snap(), SETTINGS, weekly=None, adjustments=adj.empty()).table
    late = scenarios["delay"]
    b = base[base.asin == "R1"].reset_index(drop=True)
    d = late[late.asin == "R1"].reset_index(drop=True)
    # an order that was due in month h (base) and is no longer there (delayed) is explained as delayed
    due = [h for h in range(6) if b.units_forecast[h] > 0 and d.units_forecast[h] == 0]
    assert due and all(d.reason_code[h] == "NO_ORDER_DELAYED" for h in due)
    assert d.reason_detail.str.contains("ORDERS_DELAYED").all()
    # where it lands (two months later) it keeps the reason it was placed for
    landed = [h for h in range(2, 6) if d.units_forecast[h] > 0]
    assert landed and all(d.units_forecast[h] == b.units_forecast[h - 2] for h in landed)
    assert all(d.reason_code[h] == b.reason_code[h - 2] for h in landed)


def test_market_level_defaults_are_flagged(scenarios):
    t = scenarios["short"]
    codes = _codes(t)
    assert {"SEASONALITY_FLAT", "HABIT_MARKET_DEFAULT", "AMAZON_RATIOS_DEFAULT", "PO_SHARE_DEFAULT"} <= codes
    assert t[t.asin == "B1"].reason_detail.str.contains("HABIT_BLENDED").all()
    assert t[t.asin == "F1"].reason_detail.str.contains("HABIT_POOLED").all()
    assert (t.confidence == "LOW").all() or set(t.confidence) <= {"LOW", "MEDIUM"}
    # an established market with its own history flags none of them
    assert not {"SEASONALITY_FLAT", "HABIT_MARKET_DEFAULT", "PO_SHARE_DEFAULT"} & _codes(scenarios["plain"])


def test_amazon_weight_zero_is_its_own_demand_code(scenarios):
    t = scenarios["amazon_off"]
    assert t.reason_detail.str.contains("DEMAND_OWN_AMAZON_OFF").all()
    assert not t.reason_detail.str.contains("DEMAND_AMAZON_BLEND").any()


def test_partly_missing_stock_falls_back_per_asin_only():
    from order_forecast.replenish import build_replenish_forecast as brf
    full = brf(make_snap(), SETTINGS, weekly=None, adjustments=adj.empty())
    part = brf(make_snap(stock_for_r2=False), SETTINGS, weekly=None, adjustments=adj.empty())
    basis = part.table.groupby("asin")["basis"].first()
    assert basis["R1"] == "replay" and basis["R2"] == "sales_only" and basis["S1"] == "sales_only"
    r2 = part.table[part.table.asin == "R2"]
    assert (r2.reason_code == "SALES_ONLY_NO_STOCK").all() and r2.order_chance.isna().all()
    d = part.extras["decisions"].set_index("asin")
    assert not d.loc["R2", "has_stock_data"] and d.loc["R1", "has_stock_data"]
    assert part.extras["summary"]["sales_only_asins"] == 2 and full.extras["summary"]["sales_only_asins"] == 1
    # R1 is replayed the same way whether or not R2 has stock readings, except where R2's stock fed the
    # market's typical cover: R1 has its own habits, so it is unchanged
    a = full.table[full.table.asin == "R1"].units_forecast.to_numpy()
    b = part.table[part.table.asin == "R1"].units_forecast.to_numpy()
    assert np.allclose(a, b)


def test_adjustments_with_eu_codes_and_blank_marketplace(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("marketplace,asin,month_start,type,value,note,entered_by,valid_until\n"
                 "DE,R1,,demand_multiplier,2,DE deal,x,\n"
                 "FR,R1,,demand_multiplier,5,FR deal,x,\n"
                 ",R2,,discontinue,,everywhere,x,\n")
    de = adj.load_adjustments(p, "DE", FUTURE[0])
    assert sorted(de["note"]) == ["DE deal", "everywhere"]                 # its own row plus the blank-market row
    es = adj.load_adjustments(p, "ES", FUTURE[0])
    assert list(es["note"]) == ["everywhere"]                              # a blank marketplace applies to every market
