"""The EU countries (DE, ES, FR, IT) run one marketplace per pool, end to end, offline."""
import datetime as dt
import re
from pathlib import Path

import pytest

from fakedb import FakeConn
from order_forecast import pull
from order_forecast.cli import POOLS
from order_forecast.live import build_live_forecast, load_settings
from order_forecast.series import month_range
from order_forecast.snapshot import load_snapshot
from order_forecast.store import load_forecast

EU = ("DE", "ES", "FR", "IT")
MONTHS = month_range("2024-01-01", "2026-10-01")


class _PullConn:
    def close(self):
        pass


def _fake_db(monkeypatch, seen):
    """Answer the pull's SELECTs from memory and remember which marketplace each one asked for."""
    def fetch(conn, sql, params=()):
        seen.append((" ".join(sql.split()), tuple(params)))
        if "amazon_vendor_sales" in sql:
            return (["asin", "period_start", "ordered_units", "shipped_units"],
                    [(a, m.date(), 10.0 + i, 9.0) for i, a in enumerate(["B0000000A1", "B0000000A2"]) for m in MONTHS])
        if "amazon_vendor_inventory" in sql:
            return (["asin", "period_start", "net_received_units", "sellable_units", "open_po_units"],
                    [(a, m.date(), 10.0, 30.0, 5.0) for a in ["B0000000A1", "B0000000A2"] for m in MONTHS])
        if "INVOICES_TEMP" in sql:
            return (["Customer_No", "Date", "Item_Code", "Qty"], [])
        raise AssertionError(sql)
    monkeypatch.setattr(pull.db, "connect", lambda readonly=True: _PullConn())
    monkeypatch.setattr(pull.db, "fetch", fetch)


def test_cli_pool_choices_come_from_the_one_list():
    assert POOLS == sorted(pull.POOL_MARKETPLACES) and set(EU) <= set(POOLS)
    assert all(pull.POOL_MARKETPLACES[p] == [p] for p in POOLS)        # one marketplace per pool: no pooling


@pytest.mark.parametrize("code", EU)
def test_each_eu_code_flows_from_pull_to_the_stored_marketplace(monkeypatch, tmp_path, code):
    seen = []
    _fake_db(monkeypatch, seen)
    d = pull.pull_snapshot(code, tmp_path)
    # every SELECT that takes a marketplace was asked for this country only
    market_selects = [p for s, p in seen if "marketplace = ?" in s]
    assert market_selects == [(code,), (code,)]
    # the invoice query reads this country's own accounts
    inv = [p for s, p in seen if "INVOICES_TEMP" in s]
    assert len(inv) == 1 and set(inv[0]) == set(pull.accounts_for_pool(code))
    snap = load_snapshot(d)
    assert snap.pool == code and snap.manifest["marketplace"] == code and Path(d).parent.name == code
    f = build_live_forecast(snap, load_settings())
    c = FakeConn()
    load_forecast(f, snap.manifest["marketplace"], snapshot_ref=Path(d).name, conn=c)
    ins = c.statements("INSERT INTO dbo.amazon_order_forecast_run")
    assert len(ins) == 1 and ins[0][2][0] == code
    assert all(p[0] == code for _, s, p in c.statements("DELETE"))


def test_a_pool_without_accounts_skips_the_invoice_query(monkeypatch, tmp_path):
    seen = []
    _fake_db(monkeypatch, seen)
    acc = tmp_path / "accounts.csv"
    acc.write_text("customer_no,marketplace,pool,status,note\nX1,US,US,in,\n")
    d = pull.pull_snapshot("DE", tmp_path / "snaps", accounts_path=acc)
    assert not any("INVOICES_TEMP" in s for s, _ in seen)
    assert not any(re.search(r"IN \(\s*\)", s) for s, _ in seen)
    assert load_snapshot(d).manifest["accounts"] == []
