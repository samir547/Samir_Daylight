"""
One-off probe: finds where SP-API's lookback cap for
GET_VENDOR_INVENTORY_REPORT actually bites. Same idea as
probe_lookback.py (which did this for the sales report and found Sep 2023
as the boundary) - starts from that same point since this is the same
Vendor Retail Analytics report family, but this has NOT been confirmed to
share the same cap, which is exactly what this probe checks.

Run (with your venv activated, in this folder):
    python probe_inventory_lookback.py
"""
import time

import sp_api_sales_report as spapi
import sp_api_inventory_report as inv

client_id, client_secret, refresh_token, token_env = spapi.resolve_credentials("US")
if not (client_id and client_secret and refresh_token):
    raise SystemExit(f"US credentials not set (need SPAPI_CLIENT_ID, SPAPI_CLIENT_SECRET, {token_env}).")
host = spapi.REGIONS["NA"]["host"]
marketplace_id = spapi.REGIONS["NA"]["marketplaces"]["US"]

print("getting access token...")
access_token = spapi.get_access_token(client_id, client_secret, refresh_token)
print("ok\n")

end_y, end_m = spapi.last_closed_month()
print("last closed month:", f"{end_y}-{end_m:02d}")

# Start where the sales report's boundary turned out to be, then push a
# little further in both directions to see if inventory's cap matches.
candidates = [(2023, 10), (2023, 9), (2023, 8), (2023, 7), (2023, 6)]

for (y, mo) in candidates:
    start = spapi._month_start(y, mo)
    end = spapi._month_end(y, mo)
    start_iso = start.isoformat().replace("+00:00", "Z")
    end_iso = end.isoformat().replace("+00:00", "Z")
    months_back = (end_y * 12 + end_m) - (y * 12 + mo)
    print(f"--- probing {y}-{mo:02d} ({months_back} months before last-closed-month {end_y}-{end_m:02d}) ---")
    try:
        report_id = spapi.create_report(host, access_token, marketplace_id, start_iso, end_iso,
                                         report_type=inv.REPORT_TYPE, report_options=inv.REPORT_OPTIONS)
        print("  create_report OK, reportId:", report_id)
    except Exception as e:
        resp = getattr(e, "response", None)
        print("  create_report FAILED:", resp.status_code if resp is not None else "", resp.text if resp is not None else str(e))
        time.sleep(3)
        continue

    try:
        doc_id = spapi.poll_report(host, access_token, report_id, timeout_s=90)
        print("  processingStatus: DONE, reportDocumentId:", doc_id)
    except Exception as e:
        print("  poll_report result:", e)

    time.sleep(3)

print("\nprobe complete")
