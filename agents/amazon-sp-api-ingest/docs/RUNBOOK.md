# Amazon ingest — VM runbook

Operating guide for the weekly Amazon Vendor Central jobs on `vm-daylight-agents-01` (Ubuntu 24.04, UK South, time zone UTC). Design and decisions: `data-ingestion-spec.md` (sections 8, 9 and 11, decisions D1 to D12).

## 1. What runs

| Job | When (UTC) | Wrapper | Writes |
|---|---|---|---|
| Sales and inventory refresh | Thursday 06:00 | `run_weekly_sales_inventory.sh` | `dbo.amazon_vendor_sales`, `dbo.amazon_vendor_inventory`, `dbo.amazon_ingest_run` |
| Forecast pull | Thursday 07:00 | `run_weekly_forecast.sh` | `dbo.amazon_forecast_run`, `dbo.amazon_forecast_weekly`, `dbo.amazon_ingest_run`, `archive/forecast/*.json` |

Each wrapper:
- logs to `logs/<job>_<YYYYMMDD>.log`;
- holds a lock (`flock`), so two copies never overlap;
- stops a step after 3 hours;
- retries a failed run up to 3 attempts, 15 minutes apart, the same day. The sales and inventory wrapper retries only the report that failed. Exit code 2 ("nothing could run") is not retried.

Re-running is always safe: rows are upserted, unchanged rows are not touched, and nothing is deleted. The only exception is a forecast version replaced under the same generation date (D5).

Exit codes, for the scripts and the wrappers:

| Code | Meaning |
|---|---|
| 0 | Every marketplace succeeded or had nothing new (`no_data`) |
| 1 | At least one marketplace failed or was partial, or was skipped for missing credentials. Successful marketplaces are committed. |
| 2 | Nothing could run: no credentials at all, bad arguments, or the database is unreachable or missing its tables |

## 2. One-time setup on the VM

Run these as the service account that will own the cron jobs, unless the step says `sudo`.

**2.1 Packages: Python, unixODBC and Microsoft ODBC Driver 18**

```bash
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip curl git
curl -sSL -O https://packages.microsoft.com/config/ubuntu/24.04/packages-microsoft-prod.deb
sudo dpkg -i packages-microsoft-prod.deb && rm packages-microsoft-prod.deb
sudo apt-get update
sudo ACCEPT_EULA=Y apt-get install -y msodbcsql18 unixodbc-dev
odbcinst -q -d    # must list [ODBC Driver 18 for SQL Server]
```

**2.2 Code and virtual environment**

```bash
cd /path/to/Samir_Daylight          # the repo checkout on the VM
git fetch && git checkout <branch agreed for release>
cd agents/amazon-sp-api-ingest
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
chmod +x run_weekly_sales_inventory.sh run_weekly_forecast.sh
```

**2.3 `.env`**

Copy `.env.example` to `.env` and fill it in. Use the VM's own SQL login (section 2.5), not an admin login. Make it readable by the service account only:

```bash
cp .env.example .env && nano .env
chmod 600 .env
```

Never print, paste or commit `.env`. The scripts print only the variable names, the lengths, and the last 6 characters of the client id.

**2.4 Azure SQL firewall**

Find the VM's outbound IP and have it added to the `daylight-powerbi-db-1` firewall (Varun):

```bash
curl -s https://ifconfig.me; echo
```
```bash
az sql server firewall-rule create --resource-group <rg> --server daylight-powerbi-db-1 \
  --name vm-daylight-agents-01 --start-ip-address <ip> --end-ip-address <ip>
```

**2.5 Least-privilege SQL login (Varun)**

The job needs only these rights. It has **no DELETE on the fact tables**, so the "never delete" rule is also enforced by the database. The only DELETE is on `amazon_forecast_weekly`, for replacing a forecast version (D5). Temp tables (`#stage`) need no grant. Run in `DAYHANSA_SQL1` as an admin. This creates a contained database user; a server login plus a user mapped to it works the same.

```sql
CREATE USER amazon_ingest_job WITH PASSWORD = '<strong password - store it only in the VM .env>';

GRANT SELECT, INSERT, UPDATE ON dbo.amazon_ingest_run       TO amazon_ingest_job;
GRANT SELECT, INSERT, UPDATE ON dbo.amazon_vendor_sales     TO amazon_ingest_job;
GRANT SELECT, INSERT, UPDATE ON dbo.amazon_vendor_inventory TO amazon_ingest_job;
GRANT SELECT, INSERT, UPDATE ON dbo.amazon_forecast_run     TO amazon_ingest_job;
GRANT SELECT, INSERT, DELETE ON dbo.amazon_forecast_weekly  TO amazon_ingest_job;
GRANT SELECT                 ON dbo.amazon_asin_sku_map     TO amazon_ingest_job;   -- quality checks (unmapped ASINs)
```

No DDL rights, no rights on any other table (in particular nothing used by the demand-planning agent), and no rights on the views. The views are for readers such as Power BI, which use their own logins. Put `DB_USER=amazon_ingest_job` and the password in the VM's `.env`.

**2.6 Before the first scheduled run**

1. The tables exist: `sql/001` to `sql/005` applied (done at Gate 2). `sql/007_checks.sql` check 2 shows `present = 1`.
2. History loaded (spec 6.2 steps 5 to 7, from a developer machine): `load_csv_to_db.py` lists the files, then `--load`. The UK backfill (step 6) runs first if UK is to be loaded in the same pass.
3. US forecast snapshot imported once (spec 8.6):
   ```bash
   .venv/bin/python load_csv_to_db.py --forecast-json /path/raw_forecast_US_20260927.json --marketplace US          # check
   .venv/bin/python load_csv_to_db.py --forecast-json /path/raw_forecast_US_20260927.json --marketplace US --load
   ```
4. Dry runs. These need no Amazon or database access:
   ```bash
   .venv/bin/python sp_api_sales_report.py --weekly-refresh --marketplaces US,UK,DE,FR,IT,ES --sink db --dry-run
   .venv/bin/python sp_api_forecast_report.py --marketplaces US,UK,DE,FR,IT,ES --sink db --dry-run
   ```
5. One manual run, using the wrappers with `TRIGGER=manual`, then read the run log (section 5):
   ```bash
   TRIGGER=manual ./run_weekly_sales_inventory.sh; echo "exit $?"
   TRIGGER=manual ./run_weekly_forecast.sh;        echo "exit $?"
   ```
6. **Publishing-lag check at cron time (open item O14).** The 5-day lag was only seen at about 14:00 UTC. At or just before **06:00 UTC on a Thursday**, run `.venv/bin/python probe_weekly_reports.py --lag-only` (then `--marketplace UK`, `--marketplace DE`). If the newest week comes back "not yet available", move the sales and inventory job later on Thursday, or to Friday. Until then, a run that finds the newest week unpublished logs it as `no_data` for that window, and the next Thursday catches it up.

## 3. Cron

`crontab -e` for the service account. Replace `/path/to` with the real checkout:

```
# m h dom mon dow  command   (VM time zone is UTC)
0 6 * * 4  /path/to/Samir_Daylight/agents/amazon-sp-api-ingest/run_weekly_sales_inventory.sh >> /path/to/Samir_Daylight/agents/amazon-sp-api-ingest/logs/cron.log 2>&1
0 7 * * 4  /path/to/Samir_Daylight/agents/amazon-sp-api-ingest/run_weekly_forecast.sh        >> /path/to/Samir_Daylight/agents/amazon-sp-api-ingest/logs/cron.log 2>&1
```

The two jobs use different locks. If the sales and inventory job is still retrying at 07:00, both run at once. That is safe, because they write different tables. Both share Amazon's report throttling, which the retry handles.

## 4. Re-running

- **One marketplace, both reports:** `MPS=UK TRIGGER=manual ./run_weekly_sales_inventory.sh`
- **One report directly:** `.venv/bin/python sp_api_inventory_report.py --weekly-refresh --marketplaces UK --sink db --trigger manual`
- **Forecast for one marketplace:** `MPS=DE TRIGGER=manual ./run_weekly_forecast.sh`
- **A closed month again** (for example if Sep 2026 is still missing after go-live, spec 6.6): `.venv/bin/python sp_api_sales_report.py --start-month 2026-09 --marketplaces US,DE,FR,IT,ES --sink db --trigger manual`, and the same for `sp_api_inventory_report.py`.
- **Weeks** cannot be requested by date. The weekly job requests every missing week still inside Amazon's 6-week lookback, so the next run fills gaps by itself.

## 5. Reading the run log

```sql
-- Latest runs
SELECT TOP 50 run_id, job_name, marketplace, trigger_type, status, window_start, window_end,
       rows_read, rows_inserted, rows_updated, started_at, finished_at, message
FROM dbo.amazon_ingest_run ORDER BY run_id DESC;

-- Anything not success / no_data in the last 14 days
SELECT run_id, job_name, marketplace, status, started_at, message
FROM dbo.amazon_ingest_run
WHERE status NOT IN ('success', 'no_data') AND started_at >= DATEADD(day, -14, SYSUTCDATETIME())
ORDER BY run_id DESC;
```

| Status | Meaning |
|---|---|
| `success` | At least one row was inserted or updated |
| `no_data` | Nothing new: no week closed yet, "not yet available", the same data again, or no new forecast version. Not an error. |
| `partial` | One request worked and another failed. The working part is committed. Investigate. |
| `failed` | Authentication, a 4xx other than "not yet available", 429 or 5xx after retries, or a parse or load error. That marketplace was rolled back. |
| `running` | Still running, or the process was killed. Check the log for that date. |

`message` holds the plan notes, including weeks "skipped, outside lookback", plus Amazon's error text and the data quality findings. `rows_updated` counts restatements from Amazon. A large, unexpected value is worth a look.

Logs: `logs/cron.log` (wrapper exit codes) and `logs/sales_inventory_<date>.log` / `logs/forecast_<date>.log` (full output). Lines starting `ALERT` are the freshness checks, which run at the start of every job.

## 6. Alerts to watch

There is no notification channel yet (open item O3). Check these weekly, for example on Thursday afternoon:

```sql
-- Stale weekly data: newest WEEK row older than 13 days (one missed Thursday)
SELECT 'sales' AS report, marketplace, MAX(period_end) AS newest_week_end,
       DATEDIFF(day, MAX(period_end), CAST(SYSUTCDATETIME() AS date)) AS age_days
FROM dbo.amazon_vendor_sales WHERE period_type = 'WEEK' GROUP BY marketplace
HAVING DATEDIFF(day, MAX(period_end), CAST(SYSUTCDATETIME() AS date)) > 13
UNION ALL
SELECT 'inventory', marketplace, MAX(period_end), DATEDIFF(day, MAX(period_end), CAST(SYSUTCDATETIME() AS date))
FROM dbo.amazon_vendor_inventory WHERE period_type = 'WEEK' GROUP BY marketplace
HAVING DATEDIFF(day, MAX(period_end), CAST(SYSUTCDATETIME() AS date)) > 13;

-- Stale forecast: latest generation date older than 9 days
SELECT marketplace, MAX(forecast_generation_date) AS latest_generation,
       DATEDIFF(day, MAX(forecast_generation_date), CAST(SYSUTCDATETIME() AS date)) AS age_days
FROM dbo.amazon_forecast_run GROUP BY marketplace
HAVING DATEDIFF(day, MAX(forecast_generation_date), CAST(SYSUTCDATETIME() AS date)) > 9;
```

- **Weekly data:** up to 6 consecutive missed Thursdays lose nothing. From 7, the oldest missing weeks fall outside Amazon's lookback and are lost; the MONTH rows still cover those months (spec 6.3). The stale check fires after one missed Thursday.
- **The lookback was measured only on a Thursday.** A manual run on another weekday could see its oldest requested week refused as a "prohibited date range" (status `failed`). If that happens, re-run on a Thursday, or check the edges with `probe_weekly_reports.py --lag-only` (spec 9.1).

## 7. A missed forecast week

Amazon serves only its latest forecast, so a week that is never pulled is lost for good.

1. As soon as the stale-forecast query or a failed run shows it, run `MPS=<MP> TRIGGER=manual ./run_weekly_forecast.sh` the same week. If Amazon still serves the missed generation date, it is stored normally.
2. If Amazon has already moved to a newer generation date, the missed version cannot be recovered. Record the gap (marketplace and generation date) in the run log's follow-up, or in the team channel. Forecast accuracy for that week is then measured against the neighbouring versions.
3. Raw JSON for every stored version is in `archive/forecast/raw_forecast_<MP>_<YYYYMMDD>.json`. A lost database row can be rebuilt from it with `load_csv_to_db.py --forecast-json <file> --marketplace <MP> --load`, which is logged as an `import` run.
