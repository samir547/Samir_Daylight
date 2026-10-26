"""Reading INVOICES_TEMP lines into monthly gross units (the answer key).

Rules come from phase1-data-check.md and are pure functions so they can be
tested without the database:
  - Date has three text formats; always parse day-first. Never month-first
    (TRY_CAST in a us_english session swaps day and month on ~112k rows).
  - Item_Code may have a leading space; trim it.
  - A product line matches ^(DN|UN|EN|AN|DL|UL|EL|D|E|U|A)\\d. That drops ADJ,
    DS, ADMIN, Damage allowance, Q, OS, COOP FEES and similar.
  - Units are Qty only, never a value column. Gross = positive Qty.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation

import pandas as pd

PRODUCT_CODE_RE = re.compile(r"^(DN|UN|EN|AN|DL|UL|EL|D|E|U|A)\d", re.IGNORECASE)
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DMY = re.compile(r"^\d{2}/\d{2}/\d{4}$")


def clean_item_code(raw) -> str:
    return (raw or "").strip()


def is_product_code(raw) -> bool:
    return bool(PRODUCT_CODE_RE.match(clean_item_code(raw)))


def parse_invoice_date(text) -> date:
    """'2024-04-18', '18/04/2023' and 'D04/02/2025' (D prefix) -> date. Day-first."""
    s = (text or "").strip()[-10:]
    if _ISO.match(s):
        return date(int(s[0:4]), int(s[5:7]), int(s[8:10]))
    if _DMY.match(s):
        return date(int(s[6:10]), int(s[3:5]), int(s[0:2]))
    raise ValueError(f"unrecognised invoice date {text!r}")


def parse_qty(text) -> Decimal | None:
    """Qty is a string and may contain thousands commas. None if blank or invalid."""
    s = (text or "").replace(",", "").strip()
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def aggregate_invoices(rows, account_marketplace: dict[str, str]):
    """rows: iterable of (customer_no, date_text, item_code, qty_text).

    Returns (DataFrame[marketplace, product_code, month_start, gross_units, credit_units],
    Counter of what was dropped). Only accounts in account_marketplace are read.
    """
    stats = Counter()
    acc = {k.strip().upper(): v for k, v in account_marketplace.items()}
    gross, credit = Counter(), Counter()
    for customer_no, date_text, item_code, qty_text in rows:
        stats["rows"] += 1
        mkt = acc.get((customer_no or "").strip().upper())
        if mkt is None:
            stats["not_in_account_list"] += 1
            continue
        code = clean_item_code(item_code)
        if not is_product_code(code):
            stats["non_product_line"] += 1
            continue
        qty = parse_qty(qty_text)
        if qty is None:
            stats["blank_or_bad_qty"] += 1
            continue
        try:
            d = parse_invoice_date(date_text)
        except ValueError:
            stats["bad_date"] += 1
            continue
        key = (mkt, code.upper(), pd.Timestamp(d.year, d.month, 1))
        if qty > 0:
            gross[key] += float(qty)
        elif qty < 0:
            credit[key] += float(-qty)
        else:
            stats["zero_qty"] += 1
    keys = sorted(set(gross) | set(credit))
    df = pd.DataFrame(
        [(m, c, mo, gross[(m, c, mo)], credit[(m, c, mo)]) for (m, c, mo) in keys],
        columns=["marketplace", "product_code", "month_start", "gross_units", "credit_units"])
    return df, stats
