import numpy as np
import pandas as pd
import pytest

from order_forecast import adjustments as adj
from order_forecast.backtest import run_backtest
from order_forecast.live import build_live_forecast, load_settings
from order_forecast.replenish import (amazon_monthly, build_replenish_forecast, demand_scenarios, learn_habits,
                                      learn_po_share, pooled_ratios, replay)
from order_forecast.series import month_range
from order_forecast.snapshot import Snapshot
from order_forecast.store import load_forecast

MONTHS = month_range("2023-10-01", "2026-09-01")      # 36 months
FUTURE = month_range("2026-10-01", "2027-03-01")
SETTINGS = {**load_settings(), "method": "replenish", "adjustments_file": None}


def make_snap(stock_for_r2=True):
    """R1: sells 100 a month, Amazon orders 300 every third month and reorders at about 1 month of cover.
    R2: same sales, steady small receipts. S1: sells but has no stock readings at all."""
    rows, inv = [], []
    for asin in ("R1", "R2", "S1"):
        stock = 250.0
        for i, m in enumerate(MONTHS):
            rows.append(dict(asin=asin, month_start=m, ordered_units=100.0, shipped_units=100.0))
            rec = 0.0
            if asin == "R1":
                stock -= 100
                if i % 3 == 2:
                    rec = 300.0
                    stock += rec
            else:
                rec = 100.0
                stock = 150.0
            inv.append(dict(asin=asin, month_start=m, net_received_units=rec,
                            sellable_units=(np.nan if asin == "S1" or (asin == "R2" and not stock_for_r2) else stock),
                            open_po_units=(np.nan if asin == "S1" else 0.0)))
    sales, inv = pd.DataFrame(rows), pd.DataFrame(inv)
    return Snapshot("UK", sales, inv, pd.DataFrame(columns=["marketplace", "product_code", "month_start", "gross_units", "credit_units"]),
                    {"marketplace": "UK"})


def weekly(asin="R1", gen="2026-10-11", level=25.0, weeks=48):
    start = pd.Timestamp(gen)
    rows = [(gen, asin, start + pd.Timedelta(days=7 * i), start + pd.Timedelta(days=7 * i + 6), i,
             level, level * 1.2, level * 1.4, level * 1.8) for i in range(weeks)]
    return pd.DataFrame(rows, columns=["forecast_generation_date", "asin", "week_start", "week_end", "weeks_ahead",
                                       "mean_units", "p70_units", "p80_units", "p90_units"])


# ---- the replay -----------------------------------------------------------------------------------

def test_replay_orders_when_stock_falls_below_reorder_level():
    demand = np.full(6, 100.0)
    known, new = replay(demand, stock0=250, open_po=0, cover=1.0, lot=300, min_order_share=0.0)
    # stock after sales: 150, 50 -> below 100 in month 2: order the gap (50) to get back to 100
    assert known.sum() == 0
    assert new[0] == 0 and new[1] == pytest.approx(50.0)


def test_replay_never_orders_less_than_the_minimum_share_of_the_usual_order():
    _, new = replay(np.full(6, 100.0), 250, 0, 1.0, 300, min_order_share=0.25)
    assert new[1] == pytest.approx(75.0)          # gap 50 < 25% of 300


def test_open_po_arrives_in_month_one_and_counts_towards_stock():
    known, new = replay(np.full(4, 100.0), stock0=120, open_po=500, cover=1.0, lot=300, min_order_share=0.0)
    assert known[0] == 500 and new[0] == 0         # 120 - 100 + 500 = 520, well above reorder level
    assert (known[1:] == 0).all()


def test_pinned_order_stops_a_second_order_and_discontinue_blocks_new_ones():
    pinned = np.array([0, 0, 600.0, 0, 0, 0])
    known, new = replay(np.full(6, 100.0), 250, 0, 1.0, 300, pinned=pinned, min_order_share=0.0)
    assert known[2] == 600 and new[2] == 0
    _, blocked = replay(np.full(6, 100.0), 250, 0, 1.0, 300, stop_from=0)
    assert blocked.sum() == 0


# ---- habits ---------------------------------------------------------------------------------------

def test_habits_learn_cover_and_lot_from_history():
    s = make_snap()
    sales = s.sales.pivot(index="asin", columns="month_start", values="ordered_units")
    rec = s.inventory.pivot(index="asin", columns="month_start", values="net_received_units")
    stock = s.inventory.pivot(index="asin", columns="month_start", values="sellable_units")
    h = learn_habits(stock, sales, rec, len(MONTHS) - 1)
    assert h["R1"].lot == pytest.approx(300.0) and h["R1"].events == 12 and h["R1"].source == "own"
    assert h["R1"].orders_per_year == pytest.approx(4.0)
    assert 0.25 <= h["R1"].cover <= 4.0


def test_few_orders_lean_on_the_pool():
    s = make_snap()
    inv = s.inventory.copy()
    inv.loc[(inv.asin == "R2") & (inv.month_start < "2026-08-01"), "net_received_units"] = 0.0   # one order only
    sales = s.sales.pivot(index="asin", columns="month_start", values="ordered_units")
    rec = inv.pivot(index="asin", columns="month_start", values="net_received_units")
    stock = inv.pivot(index="asin", columns="month_start", values="sellable_units")
    h = learn_habits(stock, sales, rec, len(MONTHS) - 1)
    assert h["R2"].events <= 2 and h["R2"].source in ("blended", "pooled")


# ---- Amazon's forecast ----------------------------------------------------------------------------

def test_amazon_monthly_scales_a_part_covered_first_month_and_drops_a_barely_covered_one():
    m = amazon_monthly(weekly(gen="2026-10-11", level=10.0))
    oct_ = m[m.month_start == "2026-10-01"].iloc[0]
    assert oct_.coverage == pytest.approx(3 / 5)               # Saturdays 17, 24, 31 of five
    assert oct_.am_mean == pytest.approx(50.0)                 # 3 weeks x 10, scaled to 5 Saturdays
    m2 = amazon_monthly(weekly(gen="2026-10-25", level=10.0))  # one Saturday of October left: dropped
    assert pd.Timestamp("2026-10-01") not in set(m2.month_start)


def test_percentile_shrink_pulls_the_high_case_towards_the_mean():
    full = amazon_monthly(weekly(level=10.0), shrink=1.0)
    half = amazon_monthly(weekly(level=10.0), shrink=0.5)
    r = lambda d: d[d.month_start == "2026-11-01"].iloc[0]
    assert r(full).am_p90 > r(half).am_p90 > r(full).am_mean
    assert r(half).am_p90 - r(half).am_mean == pytest.approx(0.5 * (r(full).am_p90 - r(full).am_mean))


def test_scenarios_blend_amazon_and_fall_back_to_ours_with_pooled_uplift():
    own = np.full(6, 100.0)
    am = {FUTURE[0]: (200.0, 260.0, 340.0)}
    d = demand_scenarios(own, am, FUTURE, 0.5, (1.25, 1.6))
    assert d[0, 0] == 150.0 and d[1, 0] == 150 + 0.5 * 60 and d[2, 0] == 150 + 0.5 * 140
    assert d[0, 1] == 100.0 and d[1, 1] == 125.0 and d[2, 1] == 160.0
    assert (d[2] >= d[1]).all() and (d[1] >= d[0]).all()
    assert pooled_ratios(None) == (1.25, 1.6)


# ---- the whole forecast ---------------------------------------------------------------------------

def test_forecast_shape_ordering_and_fallback_for_missing_stock():
    f = build_replenish_forecast(make_snap(), SETTINGS, weekly=None, adjustments=adj.empty())
    t = f.table
    assert set(t["asin"]) == {"R1", "R2", "S1"} and len(t) == 18
    assert t["month_start"].min() == FUTURE[0] and t["month_start"].max() == FUTURE[-1]
    assert (t["units_upper"] >= t["units_p80"]).all() and (t["units_p80"] >= t["units_forecast"]).all()
    assert t["order_chance"].dropna().between(0, 1).all()
    basis = t.groupby("asin")["basis"].first()
    assert basis["R1"] == "replay" and basis["S1"] == "sales_only"
    assert t[t.asin == "S1"]["order_chance"].isna().all()
    assert f.extras["stats"]["no_stock_reading"] == 1 and "habits" in f.extras
    # R1 ends September at 250 - ... the next order must come within a quarter, in lumps not a smooth stream
    r1 = t[t.asin == "R1"]["units_forecast"]
    assert (r1 > 0).sum() < 6 and r1.sum() > 0


def test_amazon_forecast_changes_the_demand_it_replays():
    base = build_replenish_forecast(make_snap(), SETTINGS, weekly=None, adjustments=adj.empty()).table
    hot = build_replenish_forecast(make_snap(), SETTINGS, weekly=weekly("R1", level=60.0), adjustments=adj.empty())
    assert hot.extras["amazon_used"]
    assert hot.table[hot.table.asin == "R1"]["units_forecast"].sum() > base[base.asin == "R1"]["units_forecast"].sum()



def test_adjustments_are_applied_and_the_unadjusted_run_is_kept(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("marketplace,asin,month_start,type,value,note,entered_by,valid_until\n"
                 "UK,R1,2026-12-01,pinned_order,900,confirmed bulk,x,\n"
                 "UK,R2,,discontinue,,delisted,x,\n"
                 "US,R1,2026-11-01,pinned_order,5000,other market,x,\n")
    a = adj.load_adjustments(p, "UK", FUTURE[0])
    assert len(a) == 2                                           # the US row is not ours
    f = build_replenish_forecast(make_snap(), SETTINGS, weekly=None, adjustments=a)
    t = f.table.set_index(["asin", "month_start"])
    assert t.loc[("R1", pd.Timestamp("2026-12-01")), "units_forecast"] >= 900
    assert "pinned_order" in t.loc[("R1", pd.Timestamp("2026-12-01")), "adjustments"]
    assert t.loc["R2", "units_forecast"].sum() == 0              # discontinued: no new orders (open PO was 0)
    u = f.extras["unadjusted"]
    assert u.method == "replenish_unadjusted" and (u.table["adjustments"] == "").all()
    assert u.table[u.table.asin == "R2"]["units_forecast"].sum() > 0


def test_demand_multiplier_and_delay_move_the_numbers(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("marketplace,asin,month_start,type,value,note,entered_by,valid_until\n"
                 "UK,R1,,demand_multiplier,2,deal,x,\n")
    base = build_replenish_forecast(make_snap(), SETTINGS, weekly=None, adjustments=adj.empty()).table
    hot = build_replenish_forecast(make_snap(), SETTINGS, weekly=None,
                                   adjustments=adj.load_adjustments(p, "UK", FUTURE[0])).table
    assert hot[hot.asin == "R1"]["units_forecast"].sum() > base[base.asin == "R1"]["units_forecast"].sum()
    p.write_text("marketplace,asin,month_start,type,value,note,entered_by,valid_until\nUK,R1,,delay_orders,2,late,x,\n")
    late = build_replenish_forecast(make_snap(), SETTINGS, weekly=None,
                                    adjustments=adj.load_adjustments(p, "UK", FUTURE[0])).table
    r = lambda d: d[d.asin == "R1"]["units_forecast"].to_numpy()
    assert r(late)[:2].sum() == 0 and r(base)[:2].sum() >= 0
    assert np.allclose(r(late)[2:], r(base)[: len(r(base)) - 2])


# ---- adjustments file ----------------------------------------------------------------------------

def test_adjustments_validation_names_the_bad_line(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("marketplace,asin,month_start,type,value,note,entered_by,valid_until\n"
                 "UK,R1,2026-12-01,demand_multiplier,abc,,,\n"
                 "UK,R1,2026-12-01,magic,1,,,\n"
                 "UK,R1,,pinned_order,100,,,\n")
    with pytest.raises(SystemExit) as e:
        adj.load_adjustments(p, "UK", FUTURE[0])
    msg = str(e.value)
    assert "line 2" in msg and "line 3" in msg and "line 4" in msg


def test_expired_rows_and_missing_file_are_ignored(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("marketplace,asin,month_start,type,value,note,entered_by,valid_until\n"
                 "UK,R1,2026-10-01,demand_multiplier,2,old,x,2026-09-30\n"
                 "UK,R1,2026-11-01,demand_multiplier,2,live,x,2026-12-31\n")
    a = adj.load_adjustments(p, "UK", FUTURE[0])
    assert list(a["note"]) == ["live"]
    assert adj.load_adjustments(tmp_path / "nope.csv", "UK", FUTURE[0]).empty


# ---- wiring ---------------------------------------------------------------------------------------

def test_method_replenish_is_reachable_from_settings_and_backtest():
    f = build_live_forecast(make_snap(), SETTINGS)
    assert f.method == "replenish" and "units_upper" in f.table.columns
    r = run_backtest(make_snap(), horizon=3, min_train=24, stride=3, replenish=True)
    assert "replenish" in set(r["predictions"]["method"]) and "replenish" in set(r["scores"]["method"])


class _Cur:
    def __init__(self, log):
        self.log, self.fast_executemany = log, False

    def execute(self, sql, params=()):
        self.log.append(("execute", " ".join(sql.split())[:60]))
        return self

    def fetchone(self):
        return (42,)

    def executemany(self, sql, rows):
        self.log.append(("many", " ".join(sql.split()), rows[0]))


class _Conn:
    def __init__(self):
        self.log, self.autocommit = [], True

    def cursor(self):
        return _Cur(self.log)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def test_loader_writes_high_case_and_chance_for_replenish_and_none_for_nan():
    f = build_live_forecast(make_snap(), SETTINGS)
    c = _Conn()
    load_forecast(f, "UK", conn=c)
    many = [x for x in c.log if x[0] == "many"][0]
    assert "units_upper" in many[1] and "order_chance" in many[1] and len(many[2]) == 6
    c2 = _Conn()
    s1 = f.table[f.table.asin == "S1"].iloc[0]
    assert np.isnan(s1["order_chance"])
    f1 = type(f)(f.table[f.table.asin == "S1"], f.method, f.target, f.train_end, f.horizon, 0, 1.0)
    load_forecast(f1, "UK", conn=c2)
    assert [x for x in c2.log if x[0] == "many"][0][2][-1] is None


def test_open_orders_arrive_partly_next_month_and_the_rest_the_month_after():
    known, _ = replay(np.full(4, 100.0), stock0=500, open_po=400, cover=0.5, lot=0, po_share=0.25)
    assert known[0] == pytest.approx(100.0) and known[1] == pytest.approx(300.0) and known[2:].sum() == 0


def test_po_share_is_learned_from_history_and_capped_per_asin():
    months = month_range("2025-01-01", "2025-04-01")
    po = pd.DataFrame({m: [100.0, 100.0, np.nan] for m in months}, index=["A", "B", "C"])
    rec = pd.DataFrame({m: [50.0, 400.0, 999.0] for m in months}, index=["A", "B", "C"])   # B over-receives: capped at its PO
    # A gets 50 of 100, B gets 100 of 100 (capped), C has no PO reading and is ignored: (50+100)/(100+100)
    assert learn_po_share(po, rec, 3) == pytest.approx(0.75)
    assert learn_po_share(po * 0, rec, 3) == 1.0                   # no open orders seen: no reason to hold any back
