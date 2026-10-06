"""
One-off probe: finds where SP-API's ~36-month lookback cap for
GET_VENDOR_SALES_REPORT actually bites, by requesting a few single-month
windows working backward from Sep 2023. Reuses sp_api_sales_report.py's
own functions/config, so .env is picked up the same way.

Run (with your venv activated, in this folder):
    python probe_lookback.py
"""
import importlib.util
import sys
import time

spec = importlib.util.spec_from_file_location("m", "sp_api_sales_report.py")
m = importlib.util.module_from_spec(spec)
sys.argv = ["probe_lookback"]
spec.loader.exec_module(m)

client_id, client_secret, refresh_token, token_env = m.resolve_credentials("US")
if not (client_id and client_secret and refresh_token):
    sys.exit(f"US credentials not set (need SPAPI_CLIENT_ID, SPAPI_CLIENT_SECRET, {token_env}).")
host = m.REGIONS["NA"]["host"]
marketplace_id = m.REGIONS["NA"]["marketplaces"]["US"]

print("getting access token...")
access_token = m.get_access_token(client_id, client_secret, refresh_token)
print("ok\n")

end_y, end_m = m.last_closed_month()
print("last closed month:", f"{end_y}-{end_m:02d}")

# Candidate single-month probes, working backward across the documented
# 36-month lookback boundary (~Sep 2023 as of when this was written).
candidates = [(2023, 9), (2023, 8), (2023, 7), (2023, 6)]

for (y, mo) in candidates:
    start = m._month_start(y, mo)
    end = m._month_end(y, mo)
    start_iso = start.isoformat().replace("+00:00", "Z")
    end_iso = end.isoformat().replace("+00:00", "Z")
    months_back = (end_y * 12 + end_m) - (y * 12 + mo)
    print(f"--- probing {y}-{mo:02d} ({months_back} months before last-closed-month {end_y}-{end_m:02d}) ---")
    try:
        report_id = m.create_report(host, access_token, marketplace_id, start_iso, end_iso)
        print("  create_report OK, reportId:", report_id)
    except Exception as e:
        resp = getattr(e, "response", None)
        print("  create_report FAILED:", resp.status_code if resp is not None else "", resp.text if resp is not None else str(e))
        time.sleep(3)
        continue

    try:
        doc_id = m.poll_report(host, access_token, report_id, timeout_s=90)
        print("  processingStatus: DONE, reportDocumentId:", doc_id)
    except Exception as e:
        print("  poll_report result:", e)

    time.sleep(3)

print("\nprobe complete")
