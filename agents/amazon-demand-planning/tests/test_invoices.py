from datetime import date

import pytest

from order_forecast.invoices import aggregate_invoices, is_product_code, parse_invoice_date, parse_qty


@pytest.mark.parametrize("text,expected", [
    ("2024-04-18", date(2024, 4, 18)),          # 2024 rows are ISO
    ("18/04/2023", date(2023, 4, 18)),          # 2018-2023 rows are dd/mm/yyyy
    ("D04/02/2025", date(2025, 2, 4)),          # D prefix; day first: 4 Feb, not 2 Apr
    ("D10/09/2025", date(2025, 9, 10)),         # 10 Sep, not 9 Oct
    ("D06/06/2025", date(2025, 6, 6)),
    (" D31/12/2025 ", date(2025, 12, 31)),
])
def test_dates_are_day_first(text, expected):
    assert parse_invoice_date(text) == expected


@pytest.mark.parametrize("bad", ["", None, "04/2025", "2025/04/01", "31/02/2025x"])
def test_bad_dates_raise(bad):
    with pytest.raises(ValueError):
        parse_invoice_date(bad)


@pytest.mark.parametrize("code,ok", [
    ("DN1430", True), (" U52107", True), ("UN1530", True), ("E35500", True), ("AN1430", True), ("D25400", True),
    ("ADJ", False), ("DS", False), ("ADMIN", False), ("Damage allowance", False), ("Q", False), ("OS", False),
    ("COOP FEES", False), ("Sales Promo & Rebates", False), ("S25201G", False), ("", False), (None, False),
])
def test_product_code_rule(code, ok):
    assert is_product_code(code) is ok


def test_qty_parsing():
    assert parse_qty("1,234") == 1234
    assert parse_qty("-525") == -525
    assert parse_qty("") is None and parse_qty(None) is None and parse_qty("abc") is None


def test_aggregate_counts_gross_and_credits_and_drops_the_rest():
    rows = [
        ("30003", "18/02/2023", " UN1530", "390"),
        ("30003", "17/02/2023", "UN1530", "-525"),       # bulk return: credit, not netted into gross
        ("30003", "2024-03-05", "UN1530", "100"),
        ("30003", "D07/03/2025", "UN1530", "1,000"),
        ("30003", "01/01/2023", "ADJ", "1"),             # value adjustment, not a product
        ("30003", "01/01/2023", "UN1430", ""),           # blank qty (payment/journal)
        ("30003", "99/99/2023", "UN1430", "5"),          # bad date
        ("AMAZ2990", "01/01/2023", "UN1430", "50"),      # not an account in the list
    ]
    df, stats = aggregate_invoices(rows, {"30003": "US"})
    g = df.set_index(["product_code", "month_start"])
    assert g.loc[("UN1530", "2023-02-01"), "gross_units"] == 390
    assert g.loc[("UN1530", "2023-02-01"), "credit_units"] == 525
    assert g.loc[("UN1530", "2024-03-01"), "gross_units"] == 100
    assert g.loc[("UN1530", "2025-03-01"), "gross_units"] == 1000
    assert stats["non_product_line"] == 1 and stats["blank_or_bad_qty"] == 1
    assert stats["bad_date"] == 1 and stats["not_in_account_list"] == 1
