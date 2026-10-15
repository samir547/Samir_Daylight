# Weekly report probe (T0) — results

Probe run on **Thursday 2026-10-15**, read-only, with `probe_weekly_reports.py`. Nothing was written to SQL. Raw reports and `summary.json` files are in `output/probe/` (git-ignored) and are not committed. This file answers the "to be confirmed by probe (T0)" items in section 5 of `data-ingestion-spec.md`. The answers were copied into the spec's section 5 table in revision (e).

| Run | Marketplace | Mode | Requests | Time (UTC) |
|---|---|---|---|---|
| 1 | US | full probe | 22 | 13:59 – 14:13 |
| 2 | UK | `--lag-only` | 2 | 14:00 – 14:01 |
| 3 | DE | `--lag-only` | 2 | 14:14 – 14:16 |
| 4 | US | follow-up, see note | 6 | 14:16 – 14:20 |
| 5 | US | lookback re-check, sales only (8, 13, 26, 52 weeks back, stop at first refusal, then up to 3 more to narrow) | 5 | 15:00 – 15:03 |

**Note on run 4.** The full probe stops a window series at its first failure (4 weeks DONE, 8 weeks refused). Its lookback bisection also ran out of steps (6 weeks back DONE, 13 refused). A short follow-up using the probe's own `Probe` class narrowed both limits and ran the September reconciliation (g). The full probe skipped (g) because no successful window covered September. That follow-up script is not committed. To re-run it, either add 5/6/7-week sizes to `WINDOW_SIZES` and a finer `LOOKBACK_COARSE_WEEKS`, or ask for it to be added.

Window and lookback limits were measured on **US only**. UK and DE were checked only for WEEK support and the publishing lag.

## The three constants

| Constant | Value | Basis | Confidence |
|---|---|---|---|
| `WEEK_PUBLISH_LAG_DAYS` | **5** | The week ending Sat 2026-10-10 was DONE on Thu 2026-10-15 for sales and inventory in US, UK and DE. | **Provisional.** Only checked at about 14:00 UTC, not at the 06:00 UTC cron time. See "Thursday timing" below. A shorter lag (4 days) could not be tested today. |
| `WEEK_MAX_WINDOW_WEEKS` | **7** | US: 7 weeks (Aug 23 to Oct 10) DONE for sales and inventory. 8 weeks (Aug 16 to Oct 10) refused with the "maximum length" error. | Confirmed for US. |
| `WEEK_LOOKBACK_WEEKS` | **6**: the oldest requestable week is 6 weeks before the newest published week. Together with the newest week, that makes 7 weeks of weekly data. | Measured twice on US, with single-week requests relative to the newest week (ending Oct 10). **Run 1** (14:05–14:20 UTC, sales and inventory): 6 weeks back (Aug 23–29, starting 53 days before the run) DONE; 7 weeks back (Aug 16–22, 60 days) refused as a "prohibited date range"; also refused at 9, 13, 26, 52 and 104. **Run 5** (15:00–15:03 UTC, sales only, see table above): 8 refused, 4 DONE, 6 DONE, 7 refused. | Confirmed for US, twice on one day. Unknown whether Amazon counts in weeks from the newest week or in days from today. On a Thursday the two coincide (oldest week starts 53 days back, as tested). On other weekdays it is untested. This is a different limit from `WEEK_MAX_WINDOW_WEEKS`: an over-long window gets a "maximum length" error, while an over-old week gets a "prohibited date range" error. |

## Section 5 answers

| Item to confirm | Answer |
|---|---|
| Does the **inventory** report accept `reportPeriod = WEEK`? | **Yes**, for US, UK and DE. One `ALL` row per week plus per-ASIN rows. |
| Publishing lag; is a 5-day-old week available on a Thursday? | **Yes at about 14:00 UTC** for US, UK and DE (sales and inventory). **Not checked at 06:00 UTC.** See below. |
| Maximum window per WEEK request | **7 weeks** (US, sales and inventory). |
| Weekly lookback limit | **6 weeks before the newest published week** (`WEEK_LOOKBACK_WEEKS = 6`), measured twice. The oldest available week was Aug 23–29; Aug 16–22 was refused. This is far shorter than the 36 months available for MONTH. |
| Weekly inventory snapshot: start or end of week? | **End of week (Saturday).** For the week Sep 27 to Oct 3, the weekly value was compared with the DAY value on the first and last day, for 4 stock fields across `ALL` and each ASIN: 219 comparisons. The weekly value matched only the last day 101 times and only the first day 4 times. It matched both 111 times (the stock didn't change) and neither 3 times. |
| Which inventory fields are populated for WEEK rows? | `ALL` rows: all 12 fields, no NULLs (4 weeks). ASIN rows (260 over 4 weeks): `netReceivedInventoryUnits/Cost` and `sellThroughRate` always populated. On-hand fields (`sellable…`, `unsellable…`, `aged90Plus…`) NULL on 27 rows. `openPurchaseOrderUnits` and `unhealthyInventoryUnits/Cost` NULL on 28 rows (about 10%). Sales: no NULLs in any field (4 `ALL` + 216 ASIN rows). |
| How `ALL` rows behave in weekly reports | **One `ALL` row per week** (4 rows in a 4-week request; 1 in a single-week request), same layout as monthly. |
| How Amazon signals "not available yet" | `createReport` is **accepted** (no HTTP error). The report then ends with `processingStatus = FATAL`. Its report document contains `errorDetails`: *"Error in report request: The report data for the requested date range is not yet available. Try submitting this createReport request at a later time, or modify dataEndTime to be an earlier date."* The same text appeared for the open week (Oct 11–17) and the open month (October). |

### Other FATAL errors that must count as genuine failures, not `no_data`

All three cases end in `FATAL` with an `errorDetails` document, so the loader must tell them apart **by the text**, not by status:

- Too long: *"There is a set maximum length of requested dates for every reportPeriod value… Current request exceeds that maximum length."*
- Too old or misaligned: *"…describe a prohibited date range that does not align with the requirements for the specified reportPeriod… must also abide by the maximum lookback window…"*

Caveat: the "not yet available" text was seen only for periods that **end in the future**. A week that has closed but is not yet published (for example at 06:00 UTC) has not been observed. It probably gives the same text, but that is unconfirmed.

## Thursday timing (06:00 UTC cron)

- Every availability check today ran between **13:59 and 14:16 UTC**: US 13:59–14:01, UK 14:00–14:01, DE 14:14–14:16. The weekly sales and inventory job is scheduled for **06:00 UTC**, about 8 hours earlier. A DONE at 14:00 does not show the week is available at 06:00.
- At 06:00 UTC on a Thursday it is still **Wednesday 23:00 in Pacific time** (PDT, until 2026-11-01), or Wednesday 22:00 PST after that. Measured in US Pacific time, the newest week is then 4 days old, not 5. In Europe it is Thursday 07:00–08:00 local.
- The probe prints "Thursday schedule CONFIRMED for this run". That message only checks the weekday, not the hour, so it should not be read as confirming 06:00.
- **Suggested check before finalising the schedule:** run `python probe_weekly_reports.py --lag-only` (US, then `--marketplace UK`, `--marketplace DE`) at or just before **06:00 UTC on Thursday 2026-10-22**. If any returns the "not yet available" FATAL, either move the job later on Thursday or move it to Friday.

## Month reconciliation (g): September 2026, US sales

The September WEEK rows were summed against the September MONTH row. Weeks are attributed by their Saturday (D8), so the four weeks ending Sep 5 to Sep 26 were used. The sum of the weeks is **about 8–9% below** the MONTH row: −9.2% shipped units, −9.3% ordered units, −8.4% shipped revenue. Among the top 3 ASINs the gap was −7.1%, −10.2% and −0.9%.

This is expected from the attribution rule: Aug 30–31 are counted in September, while Sep 27–30 fall in the week ending Oct 3 and so count in October. It confirms D9: **weekly and monthly rows must never be summed together**, and weekly sums will not match monthly totals.

## Conflicts with the spec, for review at Gate 1

1. **Resolved 2026-10-15 (option 1, spec revision (d)).** The window now covers current-month weeks plus only the missing previous-month weeks, so the regular Thursday runs stay within both limits. The remaining edge cases (a catch-up after several missed runs, or `--sink csv` / `--dry-run` late in a month) were open item O15. That was resolved in spec revision (e): completion weeks older than `WEEK_LOOKBACK_WEEKS` are skipped and logged. Original finding, kept for the record: **the section 9.1 WEEK window sometimes breaks both limits.** The window starts on the Sunday on or before the 1st of the previous month. Computed for upcoming Thursdays:

   | Thursday | Window start | Weeks | Days back to start |
   |---|---|---|---|
   | 2026-10-22 (the spec's worked example) | 2026-08-30 | 7 | 53 (exactly at both observed limits) |
   | 2026-10-29 | 2026-08-30 | **8** | **60** (over the length limit; past the lookback seen today) |
   | 2026-11-26 | 2026-09-27 | **8** | **60** |
   | 2026-12-31 | 2026-11-01 | **8** | **60** |

   About one Thursday a month the window is 8 weeks long and starts about 60 days back. Splitting it into 7-week chunks fixes the length, but the oldest week would probably still be refused for lookback. That week was already loaded by earlier runs, so data is not lost, but it is no longer re-pulled for restatements. A decision is needed: clamp the window start to the lookback, accept that this week is skipped, or something else. Today's spec would class that refusal as `failed`.
2. *(Resolved in spec revision (f): the weekly history backfill was removed, and weekly rows accumulate from go-live.)* **The weekly history backfill (section 6.3) can only reach 7 weeks (the newest plus 6 earlier)**, not years. It is one request per report per marketplace, so the "expected count" is about 7 weeks. Longer history exists only as MONTH rows.
3. **UK and DE limits are not measured.** Only WEEK support and the lag were checked there. Run the full probe on one EU marketplace if the limits might differ by region.
