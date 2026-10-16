"""
Pull the Amazon SP-API Vendor Inventory report (GET_VENDOR_INVENTORY_REPORT)
for Daylight's Vendor Central account, per ASIN, for one or more
marketplaces, to CSV and/or the database. Same account, .env, options and
modes as sp_api_sales_report.py (see its docstring); the job logic is in
vendor_report_job.py.

No extra Vendor Central access is needed beyond the sales report's: both are
covered by the "Brand Analytics" role (confirmed for US only, spec section 5).

RUN:
  python sp_api_inventory_report.py --months 36 --marketplaces US,UK,DE,FR,IT,ES --out "output/daylight_inventory_report_{mp}.csv"
  python sp_api_inventory_report.py --weekly-refresh --marketplaces US,UK,DE,FR,IT,ES --sink db --trigger scheduled

Notes:
- Inventory is a point-in-time snapshot. WEEK rows are taken on the
  Saturday (probe T0). Never sum inventory across weeks, months or grains.
- A field Amazon leaves out is stored as NULL, never 0.
- Reports use Pacific Time for day and month boundaries, so totals can
  differ slightly at the edges from UTC expectations.
"""
import sys

import vendor_report_job

if __name__ == "__main__":
    sys.exit(vendor_report_job.main("inventory"))
