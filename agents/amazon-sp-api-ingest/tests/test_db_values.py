"""Value conversion for the #stage bulk insert (no database needed)."""
from datetime import date
from decimal import Decimal

import pytest

import db


def _sales_row(**kw):
    r = {"marketplace": "DE", "asin": "B000TEST01", "period_type": "MONTH",
         "start_date": "2024-03-01", "end_date": "2024-03-31", "currency": "EUR",
         "ordered_revenue": "59.45", "ordered_units": "1", "shipped_revenue": "59.45",
         "shipped_cogs": "59.99", "shipped_units": "1", "customer_returns": "0"}
    r.update(kw)
    return r


def test_scale_parsing():
    assert db._scale("DECIMAL(18,2) NULL") == 2
    assert db._scale("DECIMAL(9,4) NULL") == 4
    assert db._scale("INT NULL") is None


def test_all_decimals_share_one_exponent():
    rows = [_sales_row(ordered_revenue="0"), _sales_row(asin="B000TEST02", ordered_revenue="1050.8"),
            _sales_row(asin="B000TEST03", ordered_revenue="0.3653442")]
    out = db.prepare_vendor_rows("sales", rows)
    idx = list(db.VENDOR_TABLES["sales"]["columns"]).index("ordered_revenue")
    vals = [t[idx] for t in out]
    assert vals == [Decimal("0.00"), Decimal("1050.80"), Decimal("0.37")]
    assert {v.as_tuple().exponent for v in vals} == {-2}


def test_inventory_rate_uses_four_decimals():
    r = {"marketplace": "DE", "asin": "ALL", "period_type": "MONTH", "start_date": "2024-04-01",
         "end_date": "2024-04-30", "currency": "EUR", "sell_through_rate": "0.13636363", "open_po_units": ""}
    out = db.prepare_vendor_rows("inventory", [r])
    cols = list(db.VENDOR_TABLES["inventory"]["columns"])
    assert out[0][cols.index("sell_through_rate")] == Decimal("0.1364")
    assert out[0][cols.index("open_po_units")] is None


@pytest.mark.parametrize("bad", ["nan", "inf", "abc"])
def test_bad_numbers_rejected(bad):
    with pytest.raises(db.DataError):
        db.prepare_vendor_rows("sales", [_sales_row(ordered_revenue=bad)])


def test_empty_and_none_become_null():
    out = db.prepare_vendor_rows("sales", [_sales_row(ordered_revenue="", shipped_cogs=None)])
    cols = list(db.VENDOR_TABLES["sales"]["columns"])
    assert out[0][cols.index("ordered_revenue")] is None
    assert out[0][cols.index("shipped_cogs")] is None


def test_whole_number_required_for_int():
    with pytest.raises(db.DataError):
        db.prepare_vendor_rows("sales", [_sales_row(shipped_units="1.5")])
    out = db.prepare_vendor_rows("sales", [_sales_row(shipped_units="3.0")])
    assert out[0][list(db.VENDOR_TABLES["sales"]["columns"]).index("shipped_units")] == 3
