# Showing review flags and reasons to Power BI users

Goal: a Power BI user can see, without reading any log, **(1)** which forecasts need a person's review and why,
and **(2)** the reason behind a number, including the products that were not forecast at all.

Everything below reads from the database. Nothing depends on the cron log.

## What the database provides

| Need | Where it comes from | Needs |
|---|---|---|
| Why is this month this number (or zero)? | `dbo.vw_amazon_order_forecast_sku`: `reason_code`, `reason_detail`, `confidence`, `basis` on every forecast row | `sql/005`, `sql/006` |
| Which run needs review, and why? | `dbo.vw_amazon_order_forecast_run_status`: `review_flag` (`Review`, `OK`, `No check recorded`), `sanity_ratio`, `sanity_warning`, product counts | `sql/007` |
| Which products were not forecast, and why? | `dbo.vw_amazon_order_forecast_asin_status`: `status_text`, `reason_code`, `confidence`, `last12_units` | `sql/007` |

Apply `sql/005`, `006`, `007` by hand, in that order. The views use `is_latest = 1` to pick the current run of each
market, the same way the forecast view does.

## Handling the warning, step by step

1. **A market that needs review shows a visible banner.** On the forecast page, add a card or table from
   `vw_amazon_order_forecast_run_status`, filtered to `is_latest = 1`, with `marketplace`, `review_flag` and
   `sanity_warning`. Colour `Review` amber (conditional formatting on `review_flag`). The text in
   `sanity_warning` is already plain English: "Forecast is 3.17x the last 12 months' pace of the same products
   (usual range 0.5 to 1.6). It was loaded; please review it."
2. **Do not hard-code 1.6 in Power BI.** Use `sanity_status` / `review_flag`. The limits live in the settings
   file and may change after the team's feedback.
3. **Put the reason next to every number.** In the forecast table add `reason_code`, `confidence` and, on
   hover (tooltip), `reason_detail`. Shade `confidence = LOW` lightly. Young products, products with no stock
   reading, and zero months are the ones a reviewer should see first.
4. **Add a "Not forecast" page** from `vw_amazon_order_forecast_asin_status` (`is_latest = 1`, `status <> 'FORECAST'`):
   `asin`, `product_code` (blank when unmapped), `status_text`, `last12_units`. A product with sales but no forecast
   should never disappear silently.
5. **Give the reviewer a way to answer.** A short note on the page says who reviews (the BDM team) and how to
   report back: market, product, month, what looks wrong. Feedback becomes a settings change or a client note
   (`config/order_adjustments.csv`), not a code change.
6. **Explain the codes once.** Add a small static table (code, one-line plain-English meaning) from
   `REASON_CODES` in `order_forecast/replenish.py` (also listed in `docs/replenish.md`) and relate it to
   `reason_code`, so a hover can show the meaning and not only the code.

## Suggested measures

```DAX
Markets To Review =
CALCULATE ( DISTINCTCOUNT ( vw_amazon_order_forecast_run_status[marketplace] ),
            vw_amazon_order_forecast_run_status[is_latest] = 1,
            vw_amazon_order_forecast_run_status[review_flag] = "Review" )

Review Banner =
IF ( [Markets To Review] = 0, "No forecast needs review.",
     [Markets To Review] & " market(s) flagged for review. See the table for the reason." )

Low Confidence Rows =
CALCULATE ( COUNTROWS ( vw_amazon_order_forecast_sku ),
            vw_amazon_order_forecast_sku[is_latest] = 1,
            vw_amazon_order_forecast_sku[confidence] = "LOW" )
```

## Reading the flags correctly (put this on the page)

- `Review` means "the total was unusual compared with recent sales". It does not mean the forecast is wrong. For
  the EU markets Amazon often receives more or less than it sells (Italy about 2.8 times), so a high ratio can
  be the real ordering pattern. A reviewer decides.
- Growth is not the only reason for a high ratio: the November to April horizon includes the seasonal peak, and
  young products use a rougher method.
- `No check recorded` appears on runs made before `sql/007` (for example the first US and UK runs). It is not a
  warning.
- A forecast that is far outside the hard limits (below 0.25 or above 5 times) is **not loaded**, so Power BI shows
  the previous run. If a market looks stale, check `vw_amazon_order_forecast_run_status` for the latest run date.

## Not done here

- No Power BI file is changed by the code. The report author adds the visuals above after `sql/007` is applied.
- The ASIN-to-SKU mapping is independent: unmapped ASINs still show (`is_unmapped = 1`).
