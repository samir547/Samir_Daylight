"""
Pull the Amazon SP-API Vendor Inventory report (GET_VENDOR_INVENTORY_REPORT)
for Daylight's Vendor Central account, aggregated monthly per ASIN, in one
or more regions/marketplaces, and write the results to CSV.

This is the inventory counterpart to sp_api_sales_report.py - same account,
same auth, same .env, same date-window/chunking logic. All of that shared
plumbing is imported from sp_api_sales_report.py rather than duplicated;
only the report-specific request options, response schema, and CSV columns
differ here.

SETUP: identical to sp_api_sales_report.py - see that script's docstring
and README.md in this folder. No extra Vendor Central access is required
beyond what's already granted for the sales report (both are covered by
the same "Brand Analytics" role, per Amazon's docs and confirmed by Ankush).

RUN:
    python sp_api_inventory_report.py --months 1 --marketplaces US
    python sp_api_inventory_report.py --start-month 2023-09 --marketplaces US

Notes:
- Same SP-API caps as the sales report are assumed here (15 calendar
  months per request, up to 36 months of lookback) since this is the same
  Vendor Retail Analytics report family - but this has NOT been
  independently confirmed for the inventory report specifically. Use
  probe_inventory_lookback.py to verify the actual earliest available
  month before trusting --start-month near that boundary.
- Amazon's Vendor Retail Analytics reports use Pacific Time (PST/PDT) as
  their internal day/month boundary, even though requests here are sent in
  UTC - if a month's totals ever look slightly off at the edges, this is
  the likely reason, not a bug in the chunking logic.
"""

import csv
import sys

import sp_api_sales_report as spapi

REPORT_TYPE = "GET_VENDOR_INVENTORY_REPORT"
REPORT_OPTIONS = {
    "reportPeriod": "MONTH",
    "distributorView": "MANUFACTURING",
    "sellingProgram": "RETAIL",
}


def _inventory_row(marketplace: str, asin: str, entry: dict) -> dict:
    net_received_cost, currency = spapi._amount(entry.get("netReceivedInventoryCost"))
    net_received_units, _ = spapi._amount(entry.get("netReceivedInventoryUnits"))
    open_po_units, _ = spapi._amount(entry.get("openPurchaseOrderUnits"))
    sellable_cost, _ = spapi._amount(entry.get("sellableOnHandInventoryCost"))
    sellable_units, _ = spapi._amount(entry.get("sellableOnHandInventoryUnits"))
    unsellable_cost, _ = spapi._amount(entry.get("unsellableOnHandInventoryCost"))
    unsellable_units, _ = spapi._amount(entry.get("unsellableOnHandInventoryUnits"))
    sell_through_rate, _ = spapi._amount(entry.get("sellThroughRate"))
    aged90_cost, _ = spapi._amount(entry.get("aged90PlusDaysSellableInventoryCost"))
    aged90_units, _ = spapi._amount(entry.get("aged90PlusDaysSellableInventoryUnits"))
    unhealthy_cost, _ = spapi._amount(entry.get("unhealthyInventoryCost"))
    unhealthy_units, _ = spapi._amount(entry.get("unhealthyInventoryUnits"))
    return {
        "marketplace": marketplace,
        "asin": asin,
        "start_date": entry.get("startDate"),
        "end_date": entry.get("endDate"),
        "net_received_units": net_received_units,
        "net_received_cost": net_received_cost,
        "open_po_units": open_po_units,
        "sellable_units": sellable_units,
        "sellable_cost": sellable_cost,
        "unsellable_units": unsellable_units,
        "unsellable_cost": unsellable_cost,
        "sell_through_rate": sell_through_rate,
        "aged_90plus_units": aged90_units,
        "aged_90plus_cost": aged90_cost,
        "unhealthy_units": unhealthy_units,
        "unhealthy_cost": unhealthy_cost,
        "currency": currency,
    }


def flatten_inventory_report(report_json: dict, marketplace: str) -> list:
    """Parses the GET_VENDOR_INVENTORY_REPORT shape: an inventoryAggregate
    (dict or list of period totals) plus an inventoryByAsin list."""
    rows = []
    aggregate = report_json.get("inventoryAggregate")
    if isinstance(aggregate, dict):
        aggregate = [aggregate]
    for entry in aggregate or []:
        rows.append(_inventory_row(marketplace, "ALL", entry))
    for entry in report_json.get("inventoryByAsin", []) or []:
        rows.append(_inventory_row(marketplace, entry.get("asin", "UNKNOWN"), entry))
    return rows


def main():
    parser = spapi.argparse.ArgumentParser(description="Pull SP-API vendor inventory report to CSV")
    parser.add_argument("--months", type=int, default=1,
                         help="Number of trailing closed calendar months to pull (ignored if --start-month is given)")
    parser.add_argument("--start-month", type=str, default=None,
                         help="YYYY-MM, e.g. 2023-09. Pulls every closed month from this one through the present. "
                              "Overrides --months. Automatically split into <=15-month API requests and merged.")
    parser.add_argument("--marketplaces", type=str, default="US", help="Comma-separated: US,UK,DE,FR,IT,ES")
    parser.add_argument("--out", type=str, default="daylight_inventory_report_{mp}.csv",
                         help="Output CSV path, one file per marketplace. '{mp}' is replaced by the "
                              "marketplace code (default -> daylight_inventory_report_DE.csv, ...); without "
                              "'{mp}', '_<MP>' is inserted before the extension. Files are overwritten each run.")
    args = parser.parse_args()

    chunks = spapi.resolve_chunks(args.months, args.start_month)
    print(f"[plan] {len(chunks)} request(s) needed to cover "
          f"{chunks[0][0]:%Y-%m} through {chunks[-1][1]:%Y-%m} "
          f"(assuming the same 15-calendar-month-per-request cap as the sales report - unconfirmed for inventory)")

    rows_by_mp = {}
    failed_chunks = []
    for mp in [m.strip().upper() for m in args.marketplaces.split(",")]:
        region = spapi.MARKETPLACE_TO_REGION.get(mp)
        if not region:
            print(f"[skip] Unknown marketplace '{mp}'")
            continue

        cfg = spapi.REGIONS[region]
        client_id, client_secret, refresh_token, token_env = spapi.resolve_credentials(mp)
        if not client_id or not client_secret:
            print(f"[skip] {mp}: no client id/secret found (set SPAPI_CLIENT_ID_{mp} / "
                  f"SPAPI_CLIENT_SECRET_{mp}, or the shared SPAPI_CLIENT_ID / SPAPI_CLIENT_SECRET).")
            continue
        if not refresh_token:
            print(f"[skip] {mp} requires {token_env} to be set - not found.")
            continue

        print(f"[{mp}] getting access token ({region} endpoint)...")
        print(f"[{mp}] credentials: {spapi.describe_credentials(mp, client_id, client_secret, refresh_token, token_env)}")
        try:
            access_token = spapi.get_access_token_cached(client_id, client_secret, refresh_token)
        except spapi.requests.HTTPError as e:
            print(f"[{mp}] FAILED to get access token: {e.response.status_code} {e.response.text}")
            continue

        marketplace_id = cfg["marketplaces"][mp]
        mp_rows = rows_by_mp.setdefault(mp, [])
        for i, (chunk_start, chunk_end) in enumerate(chunks):
            start_iso = chunk_start.isoformat().replace("+00:00", "Z")
            end_iso = chunk_end.isoformat().replace("+00:00", "Z")
            print(f"[{mp}] ({i + 1}/{len(chunks)}) requesting vendor inventory report for {start_iso} to {end_iso}...")
            try:
                report_id = spapi.create_report(cfg["host"], access_token, marketplace_id, start_iso, end_iso,
                                                 report_type=REPORT_TYPE, report_options=REPORT_OPTIONS)
                doc_id = spapi.poll_report(cfg["host"], access_token, report_id)
                report_json = spapi.download_report(cfg["host"], access_token, doc_id)
            except spapi.requests.HTTPError as e:
                print(f"[{mp}] chunk {chunk_start:%Y-%m} FAILED: {e.response.status_code} {e.response.text}")
                failed_chunks.append((mp, f"{chunk_start:%Y-%m}"))
                continue
            except (RuntimeError, TimeoutError) as e:
                print(f"[{mp}] chunk {chunk_start:%Y-%m} FAILED: {e}")
                failed_chunks.append((mp, f"{chunk_start:%Y-%m}"))
                continue

            raw_path = f"raw_inventory_{mp}_{chunk_start:%Y%m}.json"
            with open(raw_path, "w", encoding="utf-8") as f:
                spapi.json.dump(report_json, f, indent=2)
            print(f"[{mp}] saved raw report to {raw_path}")

            rows = flatten_inventory_report(report_json, mp)
            print(f"[{mp}] got {len(rows)} rows for {chunk_start:%Y-%m} to {chunk_end:%Y-%m}")
            mp_rows.extend(rows)

            if i < len(chunks) - 1:
                spapi.time.sleep(2)

    fieldnames = ["marketplace", "asin", "start_date", "end_date", "net_received_units", "net_received_cost",
                  "open_po_units", "sellable_units", "sellable_cost", "unsellable_units", "unsellable_cost",
                  "sell_through_rate", "aged_90plus_units", "aged_90plus_cost", "unhealthy_units",
                  "unhealthy_cost", "currency"]
    spapi.write_per_marketplace(rows_by_mp, fieldnames, args.out, failed_chunks)


if __name__ == "__main__":
    main()
