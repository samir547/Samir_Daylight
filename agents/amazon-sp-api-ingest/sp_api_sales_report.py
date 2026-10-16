"""
Pull the Amazon SP-API Vendor Sales report (GET_VENDOR_SALES_REPORT) for
Daylight's Vendor Central account, per ASIN, for one or more marketplaces,
to CSV and/or the database. The job logic lives in vendor_report_job.py and
the SP-API plumbing in sp_api_common.py; this file is the entry point.

SETUP (in your own terminal, not in a chat window):
  Copy .env.example to .env in this folder and fill in the real values. .env
  is loaded automatically and is git-ignored - never commit it or paste its
  contents anywhere. Every marketplace has its own refresh token
  (SPAPI_REFRESH_TOKEN_<MP>); US, DE, FR, IT and ES share one app
  (SPAPI_CLIENT_ID / SPAPI_CLIENT_SECRET); the UK has its own
  (SPAPI_CLIENT_ID_UK / SPAPI_CLIENT_SECRET_UK). The database settings are
  DB_SERVER, DB_NAME, DB_USER, DB_PASSWORD (DB_DRIVER, DB_PORT optional).

RUN:
  python sp_api_sales_report.py --months 1 --marketplaces US
  python sp_api_sales_report.py --months 36 --marketplaces US,UK,DE,FR,IT,ES --out "output/daylight_sales_report_{mp}.csv"
  python sp_api_sales_report.py --weekly-refresh --marketplaces US,UK,DE,FR,IT,ES --sink db --trigger scheduled
  python sp_api_sales_report.py --weekly-refresh --marketplaces US --dry-run

Notes:
- MONTH requests cover only closed months: on day 1 to MONTH_SAFETY_DAY (5)
  of a month (UTC) the previous month is not requested yet (spec 9.2).
- SP-API caps MONTH requests at 15 calendar months and about 36 months of
  lookback; longer ranges are split automatically. Always use --months 36
  for the backfill rather than a fixed --start-month.
- WEEK requests (--weekly-refresh) are limited to 7 weeks per request and 6
  weeks of lookback before the newest published week (probe T0); the
  planning is in week_plan.py.
- No AWS SigV4 signing is needed (SP-API self-authorization) - only the LWA
  access token. Rotate SPAPI_REFRESH_TOKEN_* / SPAPI_CLIENT_SECRET periodically.
"""
import sys

import vendor_report_job

if __name__ == "__main__":
    sys.exit(vendor_report_job.main("sales"))
