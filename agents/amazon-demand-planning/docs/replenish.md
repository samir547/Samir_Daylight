# The `replenish` method: predicting what Amazon will order from Daylight

Status: built and back-tested on the UK and US snapshots. The schedule (`scripts/crontab.example`) runs it
for US, UK, DE, ES, FR and IT, one marketplace per run, with `SETTINGS=config/forecast_settings.replenish.json`.

## How it works (plain language)
1. **Learn each ASIN's habits** from its history (about 3 years): how many months of stock Amazon holds
   when it reorders ("cover"), and its usual order size. ASINs with few past orders lean on the typical values
   across the marketplace. `habits.csv` is written next to every forecast so a number can be explained.
2. **Expected sales** for the next 6 months, in three scenarios (mean, P80, P90): Amazon's own weekly forecast,
   blended with our own seasonal forecast (`amazon_weight`, default 50%). Where Amazon has no forecast for a
   month, ours is used and scaled by the typical P80/P90 uplift seen in Amazon's forecasts.
3. **Replay month by month**: stock = stock - expected sales + orders arriving. Open purchase orders arrive
   first (the share of them that historically arrives the next month, the rest the month after). If stock would
   fall below the reorder level (cover x next month's sales), Amazon orders enough to get back to it, and never
   less than 25% of its usual order size (`min_order_share`).
4. **Output per ASIN-month** (`forecast.csv`, and the database):

| Column | Meaning | Database column |
|---|---|---|
| `units_forecast` | likely order: the mean scenario | `units_forecast` |
| `units_p80` | P80 scenario | CSV only |
| `units_upper` | high case: P90 scenario | `units_upper` |
| `order_chance` | share of the three scenarios that place an order that month (0, 0.33, 0.67, 1). An indication, not a calibrated probability | `order_chance` (needs `sql/004`) |
| `basis` | `replay`, or `sales_only` when the ASIN has no usable stock reading (orders then follow sales) | `basis` (needs `sql/005`) |
| `adjustments` | client notes applied to this ASIN | CSV only |
| `reason_code` | the rule that decided the likely case (table below) | `reason_code` (needs `sql/005`) |
| `reason_detail` | every rule that touched the row, `\|`-separated, primary first | `reason_detail` (needs `sql/005`) |
| `confidence` | `HIGH`, `MEDIUM` or `LOW` (rules below). A label, not a statistic | `confidence` (needs `sql/005`) |

Next to `forecast.csv` (and `habits.csv`) every run also writes:
- `asin_decisions.csv`: every ASIN seen in sales, Amazon's forecast or inventory, with `status`
  (`FORECAST`, `SHORT_HISTORY`, `NO_SALES`, `AMAZON_FORECAST_ONLY`, `INVENTORY_ONLY`), `reason_code`
  (`REPLAY`, `SALES_ONLY_NO_STOCK`, `YOUNG_PRODUCT_BACKUP`, or the status), `confidence` (the ASIN's lowest),
  `basis`, `habit_source`, `months_of_history`, `has_amazon_forecast` (Amazon's latest forecast covers at
  least one forecast month), `has_stock_data` (a stock reading in the last 3 months), `amazon_months`,
  `first_sales_month`, `last12_units`, `forecast_units`. Stored in `dbo.amazon_order_forecast_asin_status`.
- `run_summary.json`: ASINs in sales, forecast, young-product backups, skipped (short history, no sales),
  without an Amazon forecast, Amazon-only and inventory-only (not forecast), sales-only, default habits,
  seasonality mode (`learned`, or `flat` when the market has under 24 months), both sanity ratios, the
  skipped ASINs' share of sales, which market-level defaults were used, the Amazon forecast version, whether
  the adjustments file was found, counts per reason code and confidence, and the effective settings. Stored
  in `dbo.amazon_order_forecast_run_summary`. The cron log gets the key numbers on three `Explain:` lines.

## Reason codes
`reason_code` is the first of these that applies to the likely case (after rounding):

| Code | Meaning |
|---|---|
| `YOUNG_PRODUCT_BACKUP` | under `min_history` months of sales: the young-product backup below (always LOW) |
| `SALES_ONLY_NO_STOCK` | no stock reading in the last 3 months: orders follow expected sales |
| `REORDER_MIN_ORDER_SIZE` | reorder needed; the gap was below the minimum, so the order is `min_order_share` x the usual order |
| `REORDER_TRIGGERED` | stock would fall below the reorder level (cover x next month's sales): order the gap |
| `PINNED_ORDER` | a client-confirmed order arrives; no new order needed |
| `OPEN_PO_DELIVERY` | open purchase orders arrive; no new order needed |
| `NO_ORDER_DELAYED` | zero: the order due this month was moved later (`delay_orders`) |
| `NO_ORDER_DISCONTINUED` | zero: an order was needed but `discontinue` blocks it (also a stock-unknown ASIN's month zeroed by `discontinue`; `SALES_ONLY_NO_STOCK` then moves to the detail) |
| `NO_ORDER_STOCK_COVERS` | zero: stock and open orders cover expected sales |

`reason_detail` adds any of: `DEMAND_AMAZON_BLEND` (Amazon's forecast blended with ours),
`DEMAND_OWN_NO_AMAZON` (Amazon has no usable forecast for the ASIN-month, so our seasonal forecast is used),
`DEMAND_OWN_AMAZON_OFF` (`amazon_weight` 0), `DEMAND_AMAZON_FORECAST` / `DEMAND_RECENT_AVERAGE` (young
products), `DEMAND_OVERRIDE`, `DEMAND_MULTIPLIER`, `HABIT_OWN` / `HABIT_BLENDED` / `HABIT_POOLED` (default
habits: the ASIN has no past orders), `HABIT_MARKET_DEFAULT` (the market itself has too little history, so the
hard-coded cover 1 month / order size 3 months of sales), `HABIT_OVERRIDE`, `STOCK_OVERRIDE`,
`OPEN_PO_OVERRIDE`, `OPEN_PO_DELIVERY` / `PINNED_ORDER` (also arriving in a reorder month), `ORDERS_DELAYED`,
`DELAY_NOT_APPLIED_NO_STOCK` (`delay_orders` has no effect on a stock-unknown ASIN: only replayed orders move),
`DISCONTINUED`, `CLIENT_ADJUSTED`, `HIGH_CASE_ORDER_ONLY` (no order in the likely case, but in P80/P90),
`SEASONALITY_FLAT`, `PO_SHARE_DEFAULT` (no open orders in the history, so all are assumed to arrive next
month), `AMAZON_RATIOS_DEFAULT` (no Amazon forecast at all, so the P80/P90 uplift is 1.25/1.6).
The list lives in `order_forecast/replenish.py` (`REASON_CODES`); codes are only ever added, never renamed.

`confidence`: LOW for a young-product backup, sales-only, `HABIT_POOLED` or `HABIT_MARKET_DEFAULT`; MEDIUM for
`HABIT_BLENDED`, no Amazon forecast that month (`DEMAND_OWN_*`) or `SEASONALITY_FLAT`; otherwise HIGH.

## Young products
An ASIN with fewer than `min_history` (12) months of sales but at least `young_min_months` (default 3,
overridable per market under `"pools"`, e.g. `{"ES": {"young_min_months": 2}}`) and some sales is no longer
dropped. Its expected sales are Amazon's forecast for the months Amazon covers, otherwise the average of its
last 3 months (with the usual P80/P90 uplift), and it goes through the same habits (it has few orders, so
mostly the market's typical values), the same replay and the same client adjustments. With no stock reading it
follows its 3-month average only: Amazon's forecast is a demand signal and is never copied into the order
forecast. Its rows are `YOUNG_PRODUCT_BACKUP`, confidence LOW. Because Amazon's forecast can be far above a new
product's recent sales, and the learned cover multiplies it, a backup can order several months of Amazon's
expected sales at once: read these rows as indicative.
ASINs with fewer than `young_min_months` months, or with no sales at all, are still skipped and listed in
`asin_decisions.csv`. Everything learned across the market (seasonality, typical habits, open-order share,
P80/P90 uplift) uses the same ASINs as before, so ASINs with 12+ months get exactly the same numbers.

## Sanity check
Before loading, the forecast total is compared with the recent pace of the **same ASINs**: each forecast ASIN's
average monthly sales over the last 12 months it existed, times the horizon (for an ASIN with 12+ months that is
the old "last 12 months scaled to the horizon"). It must be between `sanity_ratio_min` and `sanity_ratio_max`
(0.5 to 1.6). The old ratio over all sales ASINs, which also counted the sales of skipped ASINs and so pushed
markets with many young products towards the floor, and the skipped ASINs' share of sales are logged for
information only.

## Client knowledge (adjustments)
Facts the data cannot show (promotions, launches, confirmed orders...) go in `config/order_adjustments.csv`
(copy `config/order_adjustments.example.csv`; the real file is git-ignored because it holds business
information). The monthly job reads it before the replay. Types: `demand_multiplier`, `demand_override`,
`pinned_order`, `stock_override`, `open_po_override`, `cover_override`, `lot_override`, `discontinue`,
`delay_orders`. Rows can carry a `valid_until` date so old notes expire. A bad row stops the run and names the
line. When any adjustment applies, the same forecast without adjustments is written as
`forecast_unadjusted.csv` and loaded as method `replenish_unadjusted`, so their effect can be seen and later
checked against actuals.

## Settings (`config/forecast_settings.replenish.json`)
`amazon_weight` (0 = ignore Amazon's forecast, 1 = use it only), `percentile_shrink` (1 = plain sum of weekly
percentiles, a cautious upper bound; lower pulls the high case toward the mean), `min_order_share`,
`demand_method` (our own forecast: `seasonal_half` or `seasonal`), `adjustments_file`, `young_min_months`.
Any of them can be set per market under `"pools"`, e.g. `"pools": {"ES": {"young_min_months": 2}}`. A market
with no entry uses the shared values.

## Run it
```
python -m order_forecast pull-forecast --snapshot snapshots/UK/<stamp>        # needs the database
python -m order_forecast forecast --snapshot snapshots/UK/<stamp> --settings config/forecast_settings.replenish.json
python -m order_forecast backtest --snapshot snapshots/UK/<stamp> --replenish --order-step --stride 1
python -m order_forecast run-cycle --pool DE --settings config/forecast_settings.replenish.json --no-load
```
Without `amazon_forecast.csv` beside the snapshot the method still runs on our own demand forecast.

## Back-test (receipts = what Amazon actually took in; 6-month windows, origins every month, so overlapping)
Demand in the back-test is our own seasonal forecast, because Amazon's forecast has only two stored versions
and cannot be back-tested yet. So this tests the reorder logic (habits, stock, open orders), not Amazon's
forecast. Error is WAPE (lower is better); `seasonal_half+po` is the best simple method on most lines.

| | replenish UK | seasonal_half+po UK | replenish US | seasonal_half+po US |
|---|---|---|---|---|
| per ASIN, month | 0.82 | 0.87 | 0.90 | 0.87 |
| per ASIN, quarter | 0.44 | 0.53 | 0.62 | 0.58 |
| per ASIN, half year | 0.48 | 0.49 | 0.65 | 0.61 |
| marketplace total, month | 0.33 | 0.32 | 0.26 | 0.22 |
| marketplace total, quarter | 0.19 | 0.23 | 0.12 | 0.11 |
| volume bias | -12% | +3% | -3% | -7% |

Read it as: competitive with the simple methods, clearly better only per ASIN over a quarter in the UK. It is
not a large accuracy win on this proxy; its value is that it uses stock, open orders and Amazon's forecast and
can explain and adjust each number. Only 7 (UK) and 8 (US) origins, so differences of a few points are noise.

What the back-test changed in the design (each kept only because it is principled, not just because it scored better):
- A full usual-size order every time overshoots (Amazon orders a median of 12 times in 36 months per ASIN),
  so the order is the gap to the reorder level with a floor of 25% of the usual size.
- Only about 56% (UK) of open purchase orders are received the next month, so the share is learned from history.
- Empty stock readings stay empty. `order_step._wide` (used by the older `+po` methods) sums them to 0, which
  turned about a fifth of the UK stock cells (the empty ones) into fake zeros. `replenish` does not use it; the older code is unchanged.

## Known limits and open questions
- Which percentile Amazon actually buys against is unknown; mean is the base case, P90 the high case.
- Summing weekly percentiles overstates a monthly percentile. Without Amazon's forecast the pooled P80/P90
  uplift on US data is 1.71/2.52 of the mean, which is too wide to be a real monthly P90: check `percentile_shrink`.
- Empty stock/open-order readings (27% UK, 32% US) are treated as unknown. If the ingest confirms empty means
  none, they should be 0 and more ASINs would replay instead of falling back to sales. About a third of UK ASINs
  currently fall back (`basis = sales_only`).
- ASINs with under 3 months of sales (`young_min_months`), or none, are skipped, even if Amazon forecasts
  them; products with 3 to 11 months get the low-confidence young-product backup.
- Each EU country is forecast on its own. FR and IT share stock with the other EU countries (FR receives about
  0.51x what it sells, IT 2.76x: [phase1-data-check.md](phase1-data-check.md)), which a per-country replay
  cannot see. Pooling the EU is an open decision with the client.
- Orders are timed by when Amazon receives them (net received), not when it places them.
