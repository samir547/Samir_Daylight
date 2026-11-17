"""Young products (under min_history months of sales) and the sanity check's base."""
import numpy as np
import pandas as pd
import pytest

from order_forecast import adjustments as adj
from order_forecast.live import build_live_forecast, sanity_check, settings_for_pool
from order_forecast.replenish import amazon_monthly, build_replenish_forecast
from order_forecast.series import month_range
from order_forecast.snapshot import Snapshot
from test_replenish import FUTURE, MONTHS, SETTINGS, make_snap, weekly

EMPTY_INV = pd.DataFrame(columns=["marketplace", "product_code", "month_start", "gross_units", "credit_units"])


def with_extra(snap, asin, months, units, stock=50.0, po=0.0):
    """Add an ASIN that started selling `months` months before the end of the snapshot."""
    ms = MONTHS[-months:] if months else []
    sales = pd.concat([snap.sales, pd.DataFrame([dict(asin=asin, month_start=m, ordered_units=u, shipped_units=u)
                                                 for m, u in zip(ms, units)])], ignore_index=True)
    inv = pd.concat([snap.inventory, pd.DataFrame([dict(asin=asin, month_start=m, net_received_units=0.0,
                                                        sellable_units=stock, open_po_units=po) for m in ms])],
                    ignore_index=True)
    return Snapshot(snap.pool, sales, inv, snap.invoices, snap.manifest)


def young_snap():
    s = make_snap()
    s = with_extra(s, "Y1", 6, [10, 20, 30, 40, 50, 60])                  # young, stock known
    s = with_extra(s, "Y2", 5, [30, 30, 30, 30, 30], stock=np.nan, po=np.nan)   # young, stock unknown
    s = with_extra(s, "Z0", 6, [0, 0, 0, 0, 0, 0])                        # no sales at all
    s = with_extra(s, "T2", 2, [40, 40])                                  # under young_min_months
    return s


def am_for(*asins, level=30.0):
    return pd.concat([weekly(a, level=level) for a in asins], ignore_index=True)


def test_young_asins_are_forecast_and_marked_and_short_ones_are_skipped():
    f = build_replenish_forecast(young_snap(), SETTINGS, weekly=None, adjustments=adj.empty())
    t = f.table
    assert set(t.asin) == {"R1", "R2", "S1", "Y1", "Y2"}
    y = t[t.asin.isin(["Y1", "Y2"])]
    assert (y.reason_code == "YOUNG_PRODUCT_BACKUP").all() and (y.confidence == "LOW").all()
    assert f.skipped_short == 2 and sorted(f.extras["young"]) == ["Y1", "Y2"]
    assert set(f.extras["habits"]["asin"]) == {"R1", "R2", "S1", "Y1", "Y2"}
    assert (t.groupby("asin").size() == 6).all()


def test_young_with_stock_replays_amazon_demand_but_never_copies_it():
    base = build_replenish_forecast(young_snap(), SETTINGS, weekly=None, adjustments=adj.empty()).table
    f = build_replenish_forecast(young_snap(), SETTINGS, weekly=am_for("Y1", level=60.0), adjustments=adj.empty())
    y1 = f.table[f.table.asin == "Y1"].reset_index(drop=True)
    am = amazon_monthly(am_for("Y1", level=60.0))
    am = am[am.asin == "Y1"].set_index("month_start")["am_mean"]
    covered = y1.month_start.isin(am.index)
    assert covered.any()
    assert y1.loc[covered, "reason_detail"].str.contains("DEMAND_AMAZON_FORECAST").all()
    # the replay turns demand into orders: not equal to Amazon's monthly forecast
    assert not np.allclose(y1.loc[covered, "units_forecast"].to_numpy(), am.loc[y1.loc[covered, "month_start"]].to_numpy())
    # a higher demand signal means more orders than on the 3-month average alone
    assert y1.units_forecast.sum() > base[base.asin == "Y1"].units_forecast.sum()
    # the young ASIN does not move the established ones
    for a in ("R1", "R2", "S1"):
        assert np.allclose(f.table[f.table.asin == a].units_forecast, base[base.asin == a].units_forecast)


def test_young_without_stock_follows_its_3_month_average_not_amazon():
    f = build_replenish_forecast(young_snap(), SETTINGS, weekly=am_for("Y2", level=500.0), adjustments=adj.empty())
    y2 = f.table[f.table.asin == "Y2"]
    assert np.allclose(y2.units_forecast, 30.0)                      # last 3 months: 30, 30, 30
    assert (y2.basis == "sales_only").all() and y2.order_chance.isna().all()
    assert y2.reason_detail.str.contains("SALES_ONLY_NO_STOCK").all()
    assert y2.reason_detail.str.contains("DEMAND_RECENT_AVERAGE").all()
    assert not y2.reason_detail.str.contains("DEMAND_AMAZON").any()


def test_young_products_take_client_adjustments(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("marketplace,asin,month_start,type,value,note,entered_by,valid_until\nUK,Y2,,demand_multiplier,2,deal,x,\n")
    f = build_replenish_forecast(young_snap(), SETTINGS, weekly=None, adjustments=adj.load_adjustments(p, "UK", FUTURE[0]))
    y2 = f.table[f.table.asin == "Y2"]
    assert np.allclose(y2.units_forecast, 60.0) and y2.reason_detail.str.contains("DEMAND_MULTIPLIER").all()
    u = f.extras["unadjusted"].table
    assert np.allclose(u[u.asin == "Y2"].units_forecast, 30.0)


def test_backups_leave_the_mature_values_on_the_same_data_unchanged():
    # Same snapshot, backup off (young_min_months above min_history: the old behaviour) versus on. Market-wide
    # values (seasonality, pooled habits, P80/P90 uplift) are learned from all sales ASINs either way.
    w = am_for("R1", "Y1")
    off = build_replenish_forecast(young_snap(), {**SETTINGS, "young_min_months": 99}, weekly=w, adjustments=adj.empty())
    on = build_replenish_forecast(young_snap(), SETTINGS, weekly=w, adjustments=adj.empty())
    assert off.extras["young"] == [] and sorted(on.extras["young"]) == ["Y1", "Y2"]
    cols = ["units_forecast", "units_p80", "units_upper", "order_chance"]
    for a in ("R1", "R2", "S1"):
        x, y = on.table[on.table.asin == a], off.table[off.table.asin == a]
        assert np.array_equal(x[cols].to_numpy(float), y[cols].to_numpy(float), equal_nan=True), a
        assert (x["basis"].to_numpy() == y["basis"].to_numpy()).all()


def test_young_min_months_can_be_set_per_pool():
    s = settings_for_pool({**SETTINGS, "pools": {"UK": {"young_min_months": 7}}}, "UK")
    f = build_replenish_forecast(young_snap(), s, weekly=None, adjustments=adj.empty())
    assert not set(f.table.asin) & {"Y1", "Y2"} and f.extras["young"] == []


def test_a_whole_young_market_is_forecast_with_flat_seasonality_and_default_habits():
    months = month_range("2026-04-01", "2026-09-01")                    # 6 months: every ASIN is young
    rows, inv = [], []
    for a, lvl in (("N1", 20.0), ("N2", 40.0), ("N3", 10.0)):
        for m in months:
            rows.append(dict(asin=a, month_start=m, ordered_units=lvl, shipped_units=lvl))
            inv.append(dict(asin=a, month_start=m, net_received_units=0.0, sellable_units=2 * lvl, open_po_units=0.0))
    snap = Snapshot("ES", pd.DataFrame(rows), pd.DataFrame(inv), EMPTY_INV, {"marketplace": "ES"})
    f = build_live_forecast(snap, {**SETTINGS, "adjustments_file": None})
    t = f.table
    assert set(t.asin) == {"N1", "N2", "N3"} and len(t) == 18
    assert (t.reason_code == "YOUNG_PRODUCT_BACKUP").all()
    assert t.reason_detail.str.contains("HABIT_POOLED").all() and t.reason_detail.str.contains("HABIT_MARKET_DEFAULT").all()
    assert np.isfinite(f.sanity_ratio) and sanity_check(f, {**SETTINGS}) is None
    assert f.table.month_start.min() == pd.Timestamp("2026-10-01")


def test_sanity_ratio_uses_the_forecast_asins_on_both_sides():
    # a big product with only 2 months of sales is skipped; the old ratio counted its sales in the base
    s = with_extra(make_snap(), "BIG", 2, [3000.0, 3000.0])
    f = build_replenish_forecast(s, SETTINGS, weekly=None, adjustments=adj.empty())
    sn = f.extras["sanity"]
    assert "BIG" not in set(f.table.asin)
    assert sn["skipped_sales_share"] == pytest.approx(6000 / (3 * 1200 + 6000))
    assert sn["ratio_all_asins"] < 0.5 <= sn["ratio"] <= 1.6          # old base would refuse, the matched one does not
    assert f.sanity_ratio == sn["ratio"] and sanity_check(f, SETTINGS) is None
    # on a market where every ASIN is forecast and mature, the matched ratio equals the old one
    g = build_replenish_forecast(make_snap(), SETTINGS, weekly=None, adjustments=adj.empty())
    assert g.extras["sanity"]["ratio"] == pytest.approx(g.extras["sanity"]["ratio_all_asins"])


# ---- per-ASIN decisions and the run summary ------------------------------------------------------

def explained():
    s = young_snap()
    inv = pd.concat([s.inventory, pd.DataFrame([dict(asin="I1", month_start=MONTHS[-1], net_received_units=0.0,
                                                     sellable_units=5.0, open_po_units=0.0)])], ignore_index=True)
    s = Snapshot(s.pool, s.sales, inv, s.invoices, s.manifest)
    return build_replenish_forecast(s, SETTINGS, weekly=am_for("R1", "Y1", "AM"), adjustments=adj.empty())


def test_every_asin_seen_gets_a_status_and_reason():
    from order_forecast.replenish import ASIN_STATUSES, DECISION_COLUMNS
    f = explained()
    d = f.extras["decisions"].set_index("asin")
    assert list(f.extras["decisions"].columns) == DECISION_COLUMNS
    assert d["status"].isin(ASIN_STATUSES).all()
    assert d.loc[["R1", "R2", "S1", "Y1", "Y2"], "status"].eq("FORECAST").all()
    assert d.loc["Y1", "reason_code"] == "YOUNG_PRODUCT_BACKUP" and d.loc["Y1", "confidence"] == "LOW"
    assert d.loc["S1", "reason_code"] == "SALES_ONLY_NO_STOCK" and d.loc["R1", "reason_code"] == "REPLAY"
    assert d.loc["T2", "status"] == "SHORT_HISTORY" and d.loc["T2", "months_of_history"] == 2
    assert d.loc["Z0", "status"] == "NO_SALES"
    assert d.loc["AM", "status"] == "AMAZON_FORECAST_ONLY" and d.loc["AM", "has_amazon_forecast"]
    assert d.loc["I1", "status"] == "INVENTORY_ONLY" and d.loc["I1", "has_stock_data"]
    assert d.loc["R1", "has_amazon_forecast"] and not d.loc["R2", "has_amazon_forecast"]
    assert not d.loc["S1", "has_stock_data"] and d.loc["R1", "has_stock_data"]
    assert d.loc["R1", "months_of_history"] == 36 and d.loc["Y1", "months_of_history"] == 6
    assert d.loc["R1", "forecast_units"] == pytest.approx(f.table[f.table.asin == "R1"].units_forecast.sum())


def test_run_summary_counts_add_up_and_are_json_ready():
    import json
    s = explained().extras["summary"]
    json.dumps(s)                                                         # plain JSON, no NaN
    assert s["asins_in_sales"] == 7 and s["asins_forecast"] == 5 and s["young_product_backups"] == 2
    assert s["asins_forecast_standard"] + s["young_product_backups"] == s["asins_forecast"]
    assert s["asins_forecast"] + s["skipped_short_history"] + s["skipped_no_sales"] == s["asins_in_sales"]
    assert (s["skipped_short_history"], s["skipped_no_sales"]) == (1, 1)
    assert (s["amazon_only_dropped"], s["inventory_only_dropped"]) == (1, 1)
    assert s["asins_without_amazon_forecast"] == 3                         # R2, S1, Y2
    assert s["sales_only_asins"] == 2                                      # S1, Y2
    assert s["seasonality_mode"] == "learned" and s["market_months"] == 36
    assert s["sanity_ratio"] > 0 and s["sanity_ratio_all_asins"] > 0 and s["skipped_sales_share"] > 0
    assert sum(s["reason_codes"].values()) == 5 * 6


def test_forecast_command_writes_decisions_and_summary(tmp_path, capsys):
    import json
    from order_forecast.cli import main
    from order_forecast.snapshot import save_snapshot
    s = young_snap()
    d = save_snapshot(tmp_path / "snap", "UK", s.sales, s.inventory, s.invoices, {"marketplace": "UK"})
    cfg = tmp_path / "s.json"
    cfg.write_text(json.dumps({"method": "replenish", "adjustments_file": None}))
    assert main(["forecast", "--snapshot", str(d), "--settings", str(cfg), "--out", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    assert "Explain: sales ASINs 7, forecast 5 (3 standard + 2 young-product backups)" in out
    run = next((tmp_path / "out" / "UK").iterdir())
    assert {p.name for p in run.iterdir()} >= {"forecast.csv", "habits.csv", "asin_decisions.csv", "run_summary.json"}
    summ = json.loads((run / "run_summary.json").read_text())
    assert summ["pool"] == "UK" and summ["snapshot_ref"] == "snap" and summ["young_product_backups"] == 2
    fc = pd.read_csv(run / "forecast.csv")
    assert {"reason_code", "reason_detail", "confidence", "basis"} <= set(fc.columns)
