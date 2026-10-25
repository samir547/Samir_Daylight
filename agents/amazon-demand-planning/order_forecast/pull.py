"""Pull a snapshot from the database (SELECT only; nothing is written to it)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import db
from .accounts import accounts_for_pool
from .invoices import aggregate_invoices
from .snapshot import save_snapshot

POOL_MARKETPLACES = {"US": ["US"], "UK": ["UK"]}   # EU needs a (marketplace, asin) key: not built yet

SALES_SQL = """SELECT asin, period_start, ordered_units, shipped_units
FROM dbo.amazon_vendor_sales
WHERE period_type = 'MONTH' AND asin <> 'ALL' AND marketplace = ?
ORDER BY asin, period_start"""

INVENTORY_SQL = """SELECT asin, period_start, net_received_units, sellable_units, open_po_units
FROM dbo.amazon_vendor_inventory
WHERE period_type = 'MONTH' AND asin <> 'ALL' AND marketplace = ?
ORDER BY asin, period_start"""

INVOICE_SQL = """SELECT Customer_No, Date, Item_Code, Qty
FROM dbo.INVOICES_TEMP
WHERE LTRIM(RTRIM(Customer_No)) IN ({placeholders})"""


def _frame(cols, rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=cols).rename(columns={"period_start": "month_start"})
    df["month_start"] = pd.to_datetime(df["month_start"])
    return df


def pull_snapshot(pool: str, out_root: Path, accounts_path=None, include_pending: bool = False) -> Path:
    if pool not in POOL_MARKETPLACES:
        raise SystemExit(f"Pool {pool} is not supported yet (supported: {', '.join(POOL_MARKETPLACES)}).")
    marketplace = POOL_MARKETPLACES[pool][0]
    statuses = ("in", "pending") if include_pending else ("in",)
    kw = {"path": accounts_path} if accounts_path else {}
    accounts = accounts_for_pool(pool, statuses, **kw)
    conn = db.connect()
    try:
        sales = _frame(*db.fetch(conn, SALES_SQL, (marketplace,)))
        inventory = _frame(*db.fetch(conn, INVENTORY_SQL, (marketplace,)))
        ph = ",".join("?" * len(accounts))
        _, raw = db.fetch(conn, INVOICE_SQL.format(placeholders=ph), tuple(accounts))
    finally:
        conn.close()
    invoices, stats = aggregate_invoices(raw, accounts)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest = {"pulled_at_utc": stamp, "marketplace": marketplace, "accounts": sorted(accounts),
                "statuses": list(statuses), "rows": {"sales": len(sales), "inventory": len(inventory),
                "invoice_lines_read": len(raw)}, "invoice_stats": dict(stats),
                "months": [str(sales["month_start"].min().date()), str(sales["month_start"].max().date())]}
    return save_snapshot(Path(out_root) / pool / stamp, pool, sales, inventory, invoices, manifest)
