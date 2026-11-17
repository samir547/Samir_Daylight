"""The explicit list of Amazon invoice accounts (config/amazon_accounts.csv).

Amazon is identified by Customer_No, never by a name pattern or by
CUSTOMERS.CHANNEL (phase1-data-check.md: AMAZ2990 is not Amazon, and some
Amazon accounts are not in category AMAZ).

status: in       used for modelling
        pending  waiting for a business decision; excluded unless asked for
        out      out of scope (AU, no Vendor Central data)
        excluded provisions, intercompany, or not Amazon

pool: one modelling pool per marketplace (US, UK, DE, ES, FR, IT); each EU country is forecast on its own.
The pool only chooses which invoices are read into a snapshot (the invoice answer key for back-tests);
invoices are never a model input, so the pool does not change any forecast value.
EU caveat: the French account (AMAZ92110) also invoices ES and IT shipments, and the German accounts
cover PL/CZ shipments, so invoice back-tests per EU country are unreliable.
"""
from __future__ import annotations

import csv
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "amazon_accounts.csv"
STATUSES = ("in", "pending", "out", "excluded")


def load_accounts(path: Path | str = DEFAULT_PATH) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    seen = set()
    for r in rows:
        r["customer_no"] = r["customer_no"].strip()
        if r["status"] not in STATUSES:
            raise ValueError(f"{r['customer_no']}: unknown status {r['status']!r}")
        if r["customer_no"] in seen:
            raise ValueError(f"duplicate customer_no {r['customer_no']}")
        seen.add(r["customer_no"])
    return rows


def accounts_for_pool(pool: str, statuses=("in",), path: Path | str = DEFAULT_PATH) -> dict[str, str]:
    """customer_no -> billing marketplace, for one modelling pool (US, UK, DE, ES, FR, IT)."""
    return {r["customer_no"]: r["marketplace"]
            for r in load_accounts(path) if r["pool"] == pool and r["status"] in statuses}
