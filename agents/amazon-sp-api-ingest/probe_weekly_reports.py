"""
Read-only probe: how do WEEK-period vendor reports behave for this account?
Answers the "to be confirmed by probe (T0)" items in docs/data-ingestion-spec.md
section 5, for ONE marketplace (default US).

Nothing is written to SQL. Raw reports and a summary.json go to
output/probe/<timestamp>/ (git-ignored). No secrets are printed: only the
credential description the report scripts already print (lengths and the
last 6 characters of the client id).

What it measures, in this order (each step reuses earlier results to keep the
number of report requests small):
  a/b  WEEK sales for the newest closed weeks -> does WEEK work, publishing lag
  f    the still-open current week, and the open current MONTH -> how
       "not yet available" fails
  c    WEEK windows of 4, 8, 13, 26, 52 weeks (sales, then inventory), and the
       weekly lookback limit (single weeks further and further back)
  d    NULL profile of weekly rows, and whether ALL rows appear per week
  e    weekly inventory snapshot vs DAY inventory on the week's first and last
       day -> start or end of week
  g    closed previous month: sum of WEEK rows whose Saturday is in the month
       vs the MONTH row (ALL and the top ASINs)

Run (venv activated, in this folder):
    python probe_weekly_reports.py                 # US
    python probe_weekly_reports.py --marketplace DE
    python probe_weekly_reports.py --lag-only      # newest week + the two lookback edges

--lag-only also requests the single sales week WEEK_LOOKBACK_WEEKS back (expected
DONE) and one week further (expected refused, "prohibited date range"), both
counted from week_plan.latest_published_saturday(today). The lookback was only
measured on a Thursday; running this on another day shows whether Amazon counts
from the newest week (as week_plan assumes) or in days from today.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

import sp_api_common as spapi
import week_plan

HERE = Path(__file__).parent
SALES = "GET_VENDOR_SALES_REPORT"
INVENTORY = "GET_VENDOR_INVENTORY_REPORT"
RETRY_STATUS = (429, 500, 502, 503, 504)
BACKOFF_S = (30, 60, 120)
POLL_TIMEOUT_S = 600
WINDOW_SIZES = (4, 8, 13, 26, 52)
LOOKBACK_COARSE_WEEKS = (104, 152, 156, 160, 164, 170)
SALES_FIELDS = ("orderedRevenue", "orderedUnits", "shippedRevenue", "shippedCogs",
                "shippedUnits", "customerReturns")
INVENTORY_FIELDS = ("netReceivedInventoryUnits", "netReceivedInventoryCost", "openPurchaseOrderUnits",
                    "sellableOnHandInventoryUnits", "sellableOnHandInventoryCost",
                    "unsellableOnHandInventoryUnits", "unsellableOnHandInventoryCost", "sellThroughRate",
                    "aged90PlusDaysSellableInventoryUnits", "aged90PlusDaysSellableInventoryCost",
                    "unhealthyInventoryUnits", "unhealthyInventoryCost")


# ---------------------------------------------------------------- dates

def latest_closed_saturday(today: date) -> date:
    """Most recent Saturday strictly before today (a Saturday is not closed until it ends)."""
    back = (today.weekday() - 5) % 7 or 7
    return today - timedelta(days=back)


def week_window(last_saturday: date, weeks: int):
    """Sunday..Saturday window of `weeks` whole weeks ending on last_saturday."""
    return last_saturday - timedelta(days=7 * weeks - 1), last_saturday


def month_bounds(d: date):
    first = d.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return first, nxt - timedelta(days=1)


# ---------------------------------------------------------------- HTTP

def with_backoff(label: str, fn, *args, **kwargs):
    """Retry fn on 429/5xx (30s, 60s, 120s; Retry-After honoured if longer) and on
    connection errors. Any other HTTP error is raised to the caller."""
    for attempt in range(len(BACKOFF_S) + 1):
        try:
            return fn(*args, **kwargs)
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status not in RETRY_STATUS or attempt == len(BACKOFF_S):
                raise
            wait = BACKOFF_S[attempt]
            retry_after = e.response.headers.get("Retry-After") if e.response is not None else None
            if retry_after and retry_after.isdigit():
                wait = max(wait, int(retry_after))
            print(f"    {label}: HTTP {status}, waiting {wait}s")
            time.sleep(wait)
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt == len(BACKOFF_S):
                raise
            print(f"    {label}: network error ({type(e).__name__}), waiting {BACKOFF_S[attempt]}s")
            time.sleep(BACKOFF_S[attempt])


def get_report(host: str, token: str, report_id: str) -> dict:
    resp = requests.get(f"{host}/reports/2021-06-30/reports/{report_id}",
                        headers={"x-amz-access-token": token}, timeout=30)
    resp.raise_for_status()
    return resp.json()


class Probe:
    def __init__(self, mp: str, out_dir: Path):
        region = spapi.MARKETPLACE_TO_REGION.get(mp)
        if not region:
            sys.exit(f"Unknown marketplace '{mp}'")
        client_id, client_secret, refresh_token, token_env = spapi.resolve_credentials(mp)
        if not (client_id and client_secret and refresh_token):
            sys.exit(f"{mp}: credentials not set (needs {token_env} and a client id/secret in .env).")
        print(f"[{mp}] credentials: {spapi.describe_credentials(mp, client_id, client_secret, refresh_token, token_env)}")
        self.mp = mp
        self.host = spapi.REGIONS[region]["host"]
        self.marketplace_id = spapi.REGIONS[region]["marketplaces"][mp]
        self.token = with_backoff("token", spapi.get_access_token, client_id, client_secret, refresh_token)
        self.out_dir = out_dir
        self.results = []
        self.requests_made = 0

    def run(self, label: str, report_type: str, period: str, start: date, end: date) -> dict:
        """One report request. Never raises for an Amazon outcome; the outcome is recorded."""
        res = {"label": label, "report_type": report_type, "period": period,
               "start": start.isoformat(), "end": end.isoformat(),
               "requested_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        print(f"  [{label}] {report_type.split('_')[2]} {period} {start}..{end}")
        options = {"reportPeriod": period, "distributorView": "MANUFACTURING", "sellingProgram": "RETAIL"}
        t0 = time.time()
        self.requests_made += 1
        try:
            report_id = with_backoff("createReport", spapi.create_report, self.host, self.token,
                                     self.marketplace_id, f"{start}T00:00:00Z", f"{end}T23:59:59Z",
                                     report_type=report_type, report_options=options)
        except requests.HTTPError as e:
            res.update(outcome="CREATE_REJECTED", http_status=e.response.status_code,
                       error=e.response.text[:800])
            return self._done(res, t0)
        res["report_id"] = report_id

        deadline = time.time() + POLL_TIMEOUT_S
        body = {}
        while time.time() < deadline:
            body = with_backoff("getReport", get_report, self.host, self.token, report_id)
            if body.get("processingStatus") in ("DONE", "FATAL", "CANCELLED"):
                break
            time.sleep(10)
        status = body.get("processingStatus", "TIMEOUT")
        res["outcome"] = status if status in ("DONE", "FATAL", "CANCELLED") else "TIMEOUT"
        res["report_meta"] = {k: body.get(k) for k in
                              ("processingStatus", "dataStartTime", "dataEndTime", "createdTime",
                               "processingStartTime", "processingEndTime")}
        doc_id = body.get("reportDocumentId")
        if doc_id:
            try:
                doc = with_backoff("download", spapi.download_report, self.host, self.token, doc_id)
            except (requests.HTTPError, ValueError) as e:
                res["download_error"] = str(e)[:300]
                doc = None
            if doc is not None:
                path = self.out_dir / f"{label}.json"
                path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
                res["raw_file"] = path.name
                res["doc"] = doc  # dropped from summary.json, used for analysis
                if status != "DONE":
                    res["error"] = json.dumps(doc)[:800]
        return self._done(res, t0)

    def _done(self, res: dict, t0: float) -> dict:
        res["seconds"] = round(time.time() - t0)
        print(f"    -> {res['outcome']}" + (f" ({res.get('http_status')})" if res.get("http_status") else "")
              + (f": {res['error'][:200]}" if res.get("error") else ""))
        self.results.append(res)
        time.sleep(2)
        return res


# ---------------------------------------------------------------- analysis

def entries(doc: dict, kind: str):
    """(aggregate entries, by-ASIN entries) of a sales or inventory report."""
    agg_key, asin_key = ("salesAggregate", "salesByAsin") if kind == SALES else \
                        ("inventoryAggregate", "inventoryByAsin")
    agg = doc.get(agg_key) or []
    if isinstance(agg, dict):
        agg = [agg]
    return agg, doc.get(asin_key) or []


def value(entry: dict, field: str):
    v, _ = spapi._amount(entry.get(field))
    return v


def null_profile(doc: dict, kind: str) -> dict:
    fields = SALES_FIELDS if kind == SALES else INVENTORY_FIELDS
    agg, by_asin = entries(doc, kind)
    prof = {}
    for name, rows in (("ALL", agg), ("ASIN", by_asin)):
        prof[name] = {"rows": len(rows),
                      "null_counts": {f: sum(1 for r in rows if value(r, f) is None) for f in fields}}
    return prof


def periods(doc: dict, kind: str):
    agg, by_asin = entries(doc, kind)
    return {"aggregate_periods": sorted({(r.get("startDate"), r.get("endDate")) for r in agg}),
            "asin_periods": sorted({(r.get("startDate"), r.get("endDate")) for r in by_asin}),
            "distinct_asins": len({r.get("asin") for r in by_asin})}


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return round(100.0 * (a - b) / b, 3)


def reconcile_month(week_doc: dict, month_doc: dict, month_first: date, top_n: int = 3) -> dict:
    """Sum WEEK rows whose Saturday (endDate) falls in the month vs the MONTH row."""
    def in_month(r):
        return r.get("endDate", "")[:7] == month_first.isoformat()[:7]
    w_agg, w_asin = entries(week_doc, SALES)
    m_agg, m_asin = entries(month_doc, SALES)
    out = {"weeks_used": sorted({(r["startDate"], r["endDate"]) for r in w_agg if in_month(r)})}
    m_all = m_agg[0] if m_agg else {}
    for field in ("shippedUnits", "orderedUnits", "shippedRevenue"):
        wk = sum(value(r, field) or 0 for r in w_agg if in_month(r))
        mo = value(m_all, field)
        out[f"ALL_{field}"] = {"weeks_sum": wk, "month": mo, "diff": None if mo is None else wk - mo,
                               "diff_pct": pct(wk, mo)}
    top = sorted(m_asin, key=lambda r: value(r, "shippedUnits") or 0, reverse=True)[:top_n]
    out["top_asins"] = []
    for i, m in enumerate(top, 1):
        wk = sum(value(r, "shippedUnits") or 0 for r in w_asin if r.get("asin") == m.get("asin") and in_month(r))
        mo = value(m, "shippedUnits")
        out["top_asins"].append({"label": f"ASIN-{i}", "asin": m.get("asin"), "weeks_sum": wk, "month": mo,
                                 "diff": None if mo is None else wk - mo, "diff_pct": pct(wk, mo)})
    return out


def snapshot_side(week_doc: dict, day_doc: dict, week_start: date, week_end: date) -> dict:
    """Compare the weekly inventory snapshot with DAY rows on the week's first and last day."""
    fields = ("sellableOnHandInventoryUnits", "unsellableOnHandInventoryUnits",
              "aged90PlusDaysSellableInventoryUnits", "openPurchaseOrderUnits")
    w_agg, w_asin = entries(week_doc, INVENTORY)
    d_agg, d_asin = entries(day_doc, INVENTORY)
    ws, we = week_start.isoformat(), week_end.isoformat()

    def pick(rows, start):
        return {r.get("asin", "ALL"): r for r in rows if r.get("startDate") == start}

    week_rows = {**{"ALL": r for r in w_agg if r.get("startDate") == ws},
                 **pick(w_asin, ws)}
    first_rows = {**{"ALL": r for r in d_agg if r.get("startDate") == ws}, **pick(d_asin, ws)}
    last_rows = {**{"ALL": r for r in d_agg if r.get("startDate") == we}, **pick(d_asin, we)}
    tally = {"match_first_day": 0, "match_last_day": 0, "match_both": 0, "match_neither": 0, "compared": 0}
    for key, w in week_rows.items():
        f, l = first_rows.get(key), last_rows.get(key)
        if not f or not l:
            continue
        for field in fields:
            wv, fv, lv = value(w, field), value(f, field), value(l, field)
            if wv is None or (fv is None and lv is None):
                continue
            tally["compared"] += 1
            mf, ml = wv == fv, wv == lv
            tally["match_both" if mf and ml else "match_first_day" if mf else
                  "match_last_day" if ml else "match_neither"] += 1
    return {"week": [ws, we], "fields": fields, **tally}


def newest_week_verdict(today: date, sat: date, findings: dict) -> dict:
    """Was the newest closed week (ending `sat`) available today for sales and inventory?
    The weekly job runs on Thursday (spec D11), when that week is 5 days old."""
    days_old = (today - sat).days

    def first_outcome(tag):
        attempts = findings.get(f"b_{tag}_lag_attempts") or [{}]
        return attempts[0].get("outcome")

    def ever_done(tag):
        return any(a.get("outcome") == "DONE" for a in findings.get(f"b_{tag}_lag_attempts") or [])

    sales, inv = first_outcome("sales"), first_outcome("inv")
    verdict = (f"week {sat - timedelta(days=6)}..{sat} at {days_old} days old ({today:%A}): "
               f"sales {sales}, inventory {inv}. ")
    # A report type with no successful week at all is "WEEK unsupported", not "late".
    relevant = [o for o, tag in ((sales, "sales"), (inv, "inv")) if ever_done(tag)]
    if not ever_done("inv"):
        verdict += "Inventory: no recent week succeeded, so WEEK looks unsupported for inventory. "
    if today.weekday() == 3:  # Thursday
        if not relevant:
            verdict += "No WEEK report succeeded, so the schedule cannot be judged."
        elif all(o == "DONE" for o in relevant):
            verdict += "Thursday schedule CONFIRMED for this run."
        else:
            verdict += "NOT available on Thursday - recommend moving the weekly job to Friday."
    else:
        verdict += "Not run on a Thursday, so this does not confirm or reject the Thursday schedule."
    return {"week_start": (sat - timedelta(days=6)).isoformat(), "week_end": sat.isoformat(),
            "days_old": days_old, "weekday": today.strftime("%A"),
            "sales": sales, "inventory": inv, "verdict": verdict}


def lookback_edges(p: Probe, today: date) -> dict:
    """Request the single sales week WEEK_LOOKBACK_WEEKS before the newest published
    Saturday (expected DONE) and the week before that (expected refused), as
    week_plan computes them, and print both results."""
    newest = week_plan.latest_published_saturday(today)
    edges = []
    for age, expected in ((week_plan.WEEK_LOOKBACK_WEEKS, "DONE"),
                          (week_plan.WEEK_LOOKBACK_WEEKS + 1, "FATAL")):
        s = newest - timedelta(days=7 * age)
        r = p.run(f"b_sales_lookback_{age}w_{s}", SALES, "WEEK", s - timedelta(days=6), s)
        refused_for_lookback = "prohibited date range" in (r.get("error") or "")
        ok = r["outcome"] == expected and (expected == "DONE" or refused_for_lookback)
        edges.append({"weeks_back": age, "week": [(s - timedelta(days=6)).isoformat(), s.isoformat()],
                      "expected": "DONE" if expected == "DONE" else "FATAL (prohibited date range)",
                      "outcome": r["outcome"], "as_expected": ok, "error": r.get("error")})
    all_ok = all(e["as_expected"] for e in edges)
    print(f"\nLookback edges ({today:%A}, counted from newest published Saturday {newest}):")
    for e in edges:
        print(f"  {e['weeks_back']} weeks back {e['week'][0]}..{e['week'][1]}: {e['outcome']} "
              f"(expected {e['expected']}) {'OK' if e['as_expected'] else 'MISMATCH'}")
    print("  Lookback is where week_plan expects it." if all_ok else
          "  MISMATCH: the lookback is not where week_plan expects it on this day. "
          "Check WEEK_LOOKBACK_WEEKS before relying on runs on this weekday.")
    return {"weekday": today.strftime("%A"), "newest_published_saturday": newest.isoformat(),
            "edges": edges, "as_expected": all_ok}


# ---------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--marketplace", default="US", help="One marketplace code (default US)")
    parser.add_argument("--lag-only", action="store_true",
                        help="Only check the newest closed weeks for sales and inventory, plus the two "
                             "sales lookback edges (4 to 8 requests): does WEEK work, is the newest week "
                             "available yet, and is the lookback where week_plan expects it. Run it on a "
                             "Thursday at 06:00 UTC to re-check the schedule, or on another marketplace "
                             "for a quick WEEK-support check.")
    parser.add_argument("--max-lookback-probes", type=int, default=4,
                        help="Extra single-week requests to narrow the lookback boundary (default 4)")
    args = parser.parse_args()

    mp = args.marketplace.strip().upper()
    today = datetime.now(timezone.utc).date()
    out_dir = HERE / "output" / "probe" / f"{mp}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Weekly report probe, {mp}, today (UTC) {today}, raw output in {out_dir.relative_to(HERE)}")
    p = Probe(mp, out_dir)
    findings = {"marketplace": mp, "today_utc": today.isoformat()}

    # a/b: newest closed weeks, sales and inventory.
    sat = latest_closed_saturday(today)
    newest = {}
    for kind, tag in ((SALES, "sales"), (INVENTORY, "inv")):
        attempts = []
        for back in range(3):
            s = sat - timedelta(days=7 * back)
            r = p.run(f"b_{tag}_week_{s}", kind, "WEEK", s - timedelta(days=6), s)
            attempts.append({"saturday": s.isoformat(), "days_after_saturday": (today - s).days,
                             "outcome": r["outcome"], "error": r.get("error")})
            if r["outcome"] == "DONE":
                newest[kind] = s
                findings[f"b_{tag}_week_periods"] = periods(r["doc"], kind)
                break
        findings[f"b_{tag}_lag_attempts"] = attempts
    findings["a_sales_week_supported"] = SALES in newest
    findings["a_inventory_week_supported"] = INVENTORY in newest
    findings["b_newest_week_check"] = newest_week_verdict(today, sat, findings)
    print(f"\nNewest closed week: {findings['b_newest_week_check']['verdict']}\n")
    if args.lag_only:
        findings["b_lookback_edges"] = lookback_edges(p, today)
        return finish(p, findings, out_dir)
    if SALES not in newest:
        print("No WEEK sales report succeeded for the last 3 closed weeks - stopping here.")
        return finish(p, findings, out_dir)
    # f, c, d, e, g below use the newest week that worked for each report type.

    # f: the open current week and the open current month.
    open_sat = sat + timedelta(days=7)
    p.run(f"f_sales_open_week_{open_sat}", SALES, "WEEK", open_sat - timedelta(days=6), open_sat)
    m_first, m_last = month_bounds(today)
    p.run(f"f_sales_open_month_{m_first:%Y%m}", SALES, "MONTH", m_first, m_last)

    # c: window sizes, sales then inventory. Stop a series at its first failure.
    window_docs = {}
    for kind, tag in ((SALES, "sales"), (INVENTORY, "inv")):
        if kind not in newest:
            continue
        for n in WINDOW_SIZES:
            start, end = week_window(newest[kind], n)
            r = p.run(f"c_{tag}_window_{n}w", kind, "WEEK", start, end)
            if r["outcome"] != "DONE":
                break
            window_docs[(kind, n)] = r["doc"]
    findings["c_window_results"] = [
        {"label": r["label"], "outcome": r["outcome"], "start": r["start"], "end": r["end"],
         "error": r.get("error")} for r in p.results if r["label"].startswith("c_")]
    # c: lookback, single weeks further back. Coarse pass, then bisect the gap.
    for kind, tag in ((SALES, "sales"), (INVENTORY, "inv")):
        if kind == INVENTORY and not findings["a_inventory_week_supported"]:
            break
        newest_ok = newest[kind]
        ok_age, fail_age = 0, None
        for age in LOOKBACK_COARSE_WEEKS:
            s = newest_ok - timedelta(days=7 * age)
            r = p.run(f"c_{tag}_lookback_{age}w_{s}", kind, "WEEK", s - timedelta(days=6), s)
            if r["outcome"] == "DONE":
                ok_age = age
            else:
                fail_age = age
                break
        for _ in range(args.max_lookback_probes if fail_age else 0):
            if fail_age - ok_age <= 1:
                break
            age = (ok_age + fail_age) // 2
            s = newest_ok - timedelta(days=7 * age)
            r = p.run(f"c_{tag}_lookback_{age}w_{s}", kind, "WEEK", s - timedelta(days=6), s)
            if r["outcome"] == "DONE":
                ok_age = age
            else:
                fail_age = age
        oldest = newest_ok - timedelta(days=7 * ok_age)
        findings[f"c_{tag}_lookback"] = {
            "oldest_ok_weeks_back": ok_age, "first_fail_weeks_back": fail_age,
            "oldest_ok_week": [(oldest - timedelta(days=6)).isoformat(), oldest.isoformat()],
            "note": "relative to the newest available Saturday " + newest_ok.isoformat()}

    # d: NULL profile and ALL rows, from the largest successful window of each kind.
    for kind, tag in ((SALES, "sales"), (INVENTORY, "inv")):
        sizes = [n for (k, n) in window_docs if k == kind]
        if sizes:
            doc = window_docs[(kind, max(sizes))]
            findings[f"d_{tag}_null_profile_{max(sizes)}w"] = null_profile(doc, kind)
            per = periods(doc, kind)
            findings[f"d_{tag}_all_rows"] = {"weeks_in_window": max(sizes),
                                             "aggregate_rows": len(per["aggregate_periods"]),
                                             "distinct_asins": per["distinct_asins"]}

    # e: weekly inventory snapshot vs DAY rows on the first and last day of one week.
    if findings["a_inventory_week_supported"]:
        e_end = newest[INVENTORY] - timedelta(days=7)
        e_start = e_end - timedelta(days=6)
        week = p.run(f"e_inv_week_{e_end}", INVENTORY, "WEEK", e_start, e_end)
        day = p.run(f"e_inv_day_{e_start}_{e_end}", INVENTORY, "DAY", e_start, e_end)
        if week["outcome"] == "DONE" and day["outcome"] == "DONE":
            findings["e_snapshot_side"] = snapshot_side(week["doc"], day["doc"], e_start, e_end)

    # g: previous month, weeks vs month (sales). Use a window that covers every week of it.
    prev_first, prev_last = month_bounds(today.replace(day=1) - timedelta(days=1))
    month = p.run(f"g_sales_month_{prev_first:%Y%m}", SALES, "MONTH", prev_first, prev_last)
    covering = [n for (k, n) in window_docs if k == SALES
                and week_window(newest[SALES], n)[0] <= prev_first - timedelta(days=(prev_first.weekday() + 1) % 7)]
    if month["outcome"] == "DONE" and covering:
        findings["g_reconcile"] = reconcile_month(window_docs[(SALES, min(covering))], month["doc"], prev_first)
        findings["g_reconcile"]["month"] = prev_first.isoformat()[:7]
        findings["g_reconcile"]["window_weeks_used"] = min(covering)
    # e (extra): monthly inventory snapshot of the same month, for comparison by hand.
    if findings["a_inventory_week_supported"]:
        p.run(f"e_inv_month_{prev_first:%Y%m}", INVENTORY, "MONTH", prev_first, prev_last)

    return finish(p, findings, out_dir)


def finish(p: Probe, findings: dict, out_dir: Path) -> int:
    findings["requests_made"] = p.requests_made
    findings["requests"] = [{k: v for k, v in r.items() if k != "doc"} for r in p.results]
    path = out_dir / "summary.json"
    path.write_text(json.dumps(findings, indent=2, default=str), encoding="utf-8")
    print(f"\n{p.requests_made} report requests. Summary written to {path.relative_to(HERE)}")
    print(json.dumps({k: v for k, v in findings.items() if k != "requests"}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
