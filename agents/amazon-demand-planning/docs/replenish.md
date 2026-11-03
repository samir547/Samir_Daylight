# The `replenish` method: predicting what Amazon will order from Daylight

Status: built and back-tested on the UK and US snapshots; **not yet run against the live database** (the
Amazon forecast pull and the SQL changes need the VM). The live monthly job is unchanged: it still uses the
method in `config/forecast_settings.json`. Try `replenish` with `config/forecast_settings.replenish.json`.

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
| `basis` | `replay`, or `sales_only` when the ASIN has no usable stock reading (orders then follow sales) | CSV only |
| `adjustments` | client notes applied to this ASIN | CSV only |

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
`demand_method` (our own forecast: `seasonal_half` or `seasonal`), `adjustments_file`.

## Run it
```
python -m order_forecast pull-forecast --snapshot snapshots/UK/<stamp>        # needs the database
python -m order_forecast forecast --snapshot snapshots/UK/<stamp> --settings config/forecast_settings.replenish.json
python -m order_forecast backtest --snapshot snapshots/UK/<stamp> --replenish --order-step --stride 1
python -m order_forecast run-cycle --pool UK --settings config/forecast_settings.replenish.json --no-load
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
- ASINs with under 12 months of sales are skipped, even if Amazon forecasts them.
- EU marketplaces share stock and are not built (pools are US and UK).
- Orders are timed by when Amazon receives them (net received), not when it places them.
