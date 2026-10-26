# Phase 1 data check: Amazon order prediction

Run on 2026-10-16. Read-only: SELECTs only, over a read-intent connection using the `DB_*` settings from `agents/amazon-sp-api-ingest/.env`. No tables, views, `sql/` files or agent code were changed. The scratch scripts were kept outside the repo.

**Scope and conventions used throughout**

- Amazon facts come from `vw_amazon_vendor_sales_monthly_sku` and `vw_amazon_vendor_inventory_monthly_sku`. These views already exclude `asin = 'ALL'` rows and hold only closed months.
- **The window is the 36 loaded months for each marketplace:** UK 2023-10 to 2026-09; US, DE, FR, IT and ES 2023-09 to 2026-08. The current partial month (2026-10) is excluded. Invoices outside the window are excluded unless stated.
- **An Amazon invoice line** is `Customer_Category = 'AMAZ'`, plus five `AMAZ…` customers coded `Other` (see 1c). `AMAZ2990` (*Amazing Products N.V.*, category `HEAL`) is **not Amazon** and is excluded.
- **A product line** has a trimmed `Item_Code` matching `^(DN|UN|EN|AN|DL|UL|EL|D|E|U|A)\d` and a numeric `Qty`. Non-product codes are dropped: `Q` (carriage), `OS`/`DS` (out of stock or discontinued notes), `ADJ`, `COOP FEES`, `Sales Promo & Rebates`, `Shortage claim`, `Damage allowance` and others. Over all years these lines total 812 units of 273,440 for the six marketplaces.
- **Units are always the quantity column**, never a value column (`Qty`, `shipped_units`, `net_received_units`).

---

## 1. INVOICES_TEMP structure

**1a. Table shape**

| Item | Finding |
|---|---|
| Rows | 344,086 in total; 50,668 match the Amazon filter; 50,624 after excluding `AMAZ2990` (44 rows) |
| Columns | `Inv_No, Date, Customer_No, Customer_Name, Customer_Country, Customer_Category, Item_Code, Qty, Description, Unit_Price, Row_Sum, Unit_Cost, Row_Cost, Row_GP, Sales_AC, Account_Name, Currency, Customer_Category_Title, Salesman_Full, Salesman, Order_No`. **Every column is `nvarchar(max)`.** |
| Date column | `Date` (string). There are **three formats**: `dd/mm/yyyy` for 2018–2023 (256,897 rows), **`yyyy-mm-dd` for 2024** (29,819 rows) and `Ddd/mm/yyyy` (a `D` prefix) for 2025-01-01 to 2026-10-16 (57,370 rows). Parsing with `RIGHT(Date,10)` and style 103 alone **turns every 2024 row into NULL**. With an ISO fallback, 0 rows fail to parse. |
| Quantity column | `Qty` (string, may contain thousands commas). 0 non-integer values. On 6,591 Amazon lines `Qty` is blank; these are payments (`PYMT`), journals (`JE`) and memo lines, not product lines. |
| Value columns | `Unit_Price`, `Row_Sum` (strings with commas). Qty × Unit_Price is within 2% of Row_Sum on 94.8% of product lines. Most of the rest have the opposite sign (median ratio −1, credit-type lines). |
| Item code | `Item_Code` has a **leading space** on 42% of Amazon lines; trim it before joining. |
| Currency | Labels change with the system: `$` / `US Dollar`, `£` (stored as a mis-encoded byte) / `British pound`, `Euro`, `AUD` / `Australian Dollar`. FR (`AMAZ92110`) has 6 product lines in `British pound`. |
| Returns / credits | Yes. From 2024 credit memos have the `CM` prefix (1,767 lines, all negative). Before 2024, credits are negative lines on ordinary invoice numbers. Credits on product lines inside the window: UK −1,167 units (2.3% of gross), US −298 (0.45%), DE −8, FR −9, IT −20, ES 0. |
| Duplicates | 4,339 exact duplicate lines (same invoice, item, qty, value, date and customer), but they carry only 90 units. Almost all are blank-Qty memo lines from 2018–2023. |
| Invoice numbering | Numeric `Inv_No` up to 2023; `INV` / `CM` / `PYMT` / `JE` prefixes from 2024 (a system change at 2024-01). |
| Rows per month | All Amazon lines in the window: median 530 per month (min 263, max 1,067). Product lines per marketplace per month (median / min / max): UK 147 / 54 / 675, US 259 / 117 / 524, DE 18 / 1 / 71, FR 16 / 4 / 41, IT 13 / 1 / 37, ES 16.5 / 1 / 26. |

Query for the 2024 date format:
```sql
SELECT COUNT(*), MIN(Date), MAX(Date) FROM dbo.INVOICES_TEMP
WHERE TRY_CONVERT(date, RIGHT(Date,10), 103) IS NULL AND TRY_CONVERT(date, Date, 23) IS NOT NULL;
-- 29819 | 2024-01-01 | 2024-12-31
```

**1b. Amazon customers and their marketplaces** (product and non-product lines, all years)

| Marketplace | Customer_No (names) | Lines | Net units | First – last invoice |
|---|---|---|---|---|
| UK | `AMAZSL1` (Amazon EU S.a.r.L.), plus 24 FC-level accounts `AMAZBRS1/2, AMAZMAN1-4, AMAZLBA2/4, AMAZEMA1/2, AMAZLCY3, AMAZLE33, AMAZXBH5, AMAZDSA6, AMAZNCL1, AMAZXDS1, AMAZLTN4, AMAZMME1/2, AMAZBHX2/4/7, AMAZTS22, AMAZB76` (2024-01 to 2024-08 only), and `AMAZHP2, AMAZWS15, AMAZCV5` (2019–2021, tiny) | 29,117 | 126,293 | 2018-01 – 2026-10 |
| US | `30003` (AMAZON) | 18,144 | 136,274 | 2018-01 – 2026-10 |
| US (FBA) | `AMAZFB13` (AMAZON FBA US) | 7 | 12 | 2026-08 – 2026-09 |
| DE | `AMAZ81000` (Amazon EU SARL DE; ship-to Germany 2,578 / Poland 1,588 / Czechia 32 / Austria 7 / UK 7 units), plus `AMAZ44145, 55040, 47495, 66340, 75177` | 969 | 4,043 | 2024-04 – 2026-10 |
| FR | `AMAZ92110` (Amazon EU SARL FRANCE) | 382 | 1,774 | 2025-07 – 2026-10 |
| IT | `AMAZ20124` (Amazon EU SARL Italy), plus `AMAZ13100, 29015, 27050, 00034, 10037, 20074, 45020, 02032, 24050` | 674 | 4,110 | 2024-03 – 2026-10 |
| ES | `AMAZ2804` (Amazon EU S.à r.l Spain) | 158 | 946 | 2026-01 – 2026-09 |
| AU (out of scope) | `AMAZ2000, AMAZ2178_BWU2, AMAZ6105_PER4, AMAZ2175_BWU6, AMAZ3023_MEL5` | 727 | 1,215 | 2020-12 – 2025-11 |
| CA (out of scope) | `30115` (Amazon - Canada) | 78 | 56 | 2022-10 – 2024-11 |
| Excluded | `AMAZSL1P`, `AMASSL1P`, `300031` (provision for receivables, net −3 units); `30379` (Daylight Company – Amazon replacements, intercompany, 1 unit); `AMAZ2990` (not Amazon) | | | |

**1c. Category check.** Five Amazon accounts are coded `Other`, not `AMAZ`: `AMAZ2175_BWU6`, `AMAZ27050` and some `AMAZSL1P` lines. They total 42 units. Filtering on `Customer_Category = 'AMAZ'` alone would miss them; filtering on `Customer_No LIKE 'AMA%'` alone would wrongly include `AMAZ2990`.

---

## 2. Mapping coverage (`amazon_asin_sku_map`, joined on asin AND market)

Map contents: UK 66 rows, US 78, DE 40, ES 40, **FR 0, IT 0**. `(asin, market)` is unique. US has 3 rows with a NULL `product_code` (`B075PJ9WX5, B07NDYXSW1, B07QQH6MVF`) and **6 product codes that map to 2 ASINs each** (`U35050, U35151, U35152, U35153, U95001, UN1530`). Those 6 codes carry 3,821 US invoice units in the window, which cannot be assigned to one ASIN.

| Marketplace | Invoice units in window | % mapped to an ASIN | Unmapped codes / all codes | Shipped units | % shipped mapped to a code | Unmapped ASINs / all ASINs |
|---|---|---|---|---|---|---|
| UK | 47,718 | **74.8%** | 22 / 83 | 46,859 | **71.0%** | 28 / 89 |
| US | 64,180 | **84.7%** | 17 / 80 | 65,033 | **92.1%** | 20 / 87 |
| DE | 3,895 | 93.0% | 12 / 37 | 3,792 | 91.0% | 11 / 40 |
| ES | 619 | 91.3% | 4 / 21 | 374 | 85.3% | 9 / 33 |
| FR | 1,285 | **0.0%** | 24 / 24 | 2,483 | **0.0%** | 43 / 43 |
| IT | 3,773 | **0.0%** | 46 / 46 | 557 | **0.0%** | 32 / 32 |

**The FR and IT gap is confirmed:** there are no map rows for either marketplace. As a test only (not a fix), 97.1% of FR and 79.7% of IT invoice units use item codes that appear in the DE/ES map.

**DE/ES map defect:** 5 ASINs in DE and ES map to UK `D…` codes: `B003TX1QSK→D52107`, `B003TX2PBC→D53060`, `B00F9YE1K2→D91707`, `B00XTUD5EQ→DN1350`, `B071G3B24C→DN1380`. DE invoices use `E…` codes, so DE invoices for these products cannot match.
```sql
SELECT asin, market, product_code FROM dbo.amazon_asin_sku_map
WHERE market IN ('DE','ES') AND product_code NOT LIKE 'E%';   -- 10 rows
```

**Top 20 unmapped invoice codes, all marketplaces except FR and IT (window units)**

| # | Mkt | Item code | Description | Units |
|---|---|---|---|---|
| 1 | UK | DN1430 | UnoLamp Floor | 4,717 |
| 2 | UK | DN1530 | DuoLamp Floor | 2,120 |
| 3 | US | U52107 | Slimline Table Base | 1,924 |
| 4 | US | UN1380 | Smart Clip-on Lamp | 1,131 |
| 5 | UK | D25201 | Halo Go Rechargeable Magnifier | 909 |
| 6 | US | UN1430 | UnoLamp Floor | 891 |
| 7 | UK | DN1510 | DuoLamp with Clamp | 868 |
| 8 | US | UN1560 | DuoPro Floor Lamp | 836 |
| 9 | UK | DN1420 | UnoLamp Table | 832 |
| 10 | US | U25020 | StitchSmart LED Magnifier & Chart Holder | 725 |
| 11 | US | U35030 | Wafer 2 Lightbox (A3) | 605 |
| 12 | US | U35020 | Wafer 3 Lightbox (A2) | 576 |
| 13 | US | UN1520 | DuoLamp Table | 575 |
| 14 | US | UN1420 | UnoLamp Table | 552 |
| 15 | US | U35000 | Techne Artist & Drafting Lamp | 486 |
| 16 | US | U25200 | Halo Table Magnifier Lamp | 402 |
| 17 | UK | DN1520 | DuoLamp Table | 401 |
| 18 | UK | D25400 | Halo Go 2 Rechargeable Magnifier Lamp | 390 |
| 19 | UK | DN1410 | UnoLamp with Clamp | 363 |
| 20 | US | UN1410 | UnoLamp with Clamp | 352 |

Largest FR/IT codes (all unmapped): IT `E35500` Lumi Task Lamp 936 and IT `E35090` Twist 2 Go 549.

**Top 20 unmapped ASINs by shipped units, excluding FR and IT:** UK B01KTQ8HXG 5,129; UK B01M01AN1C 2,261; US B01GS20IFU 1,094; UK B07WC6Z5XY 947; UK B01KTQXH4A 946; UK B01KTQ8KC4 896; US B00VG1PTHQ 793; US B01GS20IP0 709; US B01GS20IX2 607; US B01LXYUMJJ 570; US B075PJ9WX5 510 (map row has a NULL code); UK B01KTQ8F9W 455; UK B08CVQP1JR 455; UK B071G3B66K 437; US B01GS20IUA 429; UK B01KTQ89GQ 393; UK B0DYF5RZZ5 348; US B07NDYXSW1 304 (NULL code); UK B082DWY9NN 277; UK B00TXD3DYA 262.

It is *not verified* whether the unmapped UK ASIN B01KTQ8HXG is the unmapped code DN1430 (UnoLamp Floor), though the volumes are close (5,129 shipped vs 4,717 invoiced). The sales tables carry no product description to confirm it.

---

## 3. Reconciliation per marketplace (36 months, all items, unmapped included)

`inv` is net invoiced product units. `rec` is the sum of `net_received_units`. `shp` is the sum of `shipped_units`. Differences are absolute monthly differences in units.

| Mkt | Σ inv | Σ rec | Σ shp | inv/rec | inv/shp | corr inv-rec | corr inv-shp | median abs diff inv-rec (abs %) | p90 abs diff inv-rec | median / p90 abs diff inv-shp |
|---|---|---|---|---|---|---|---|---|---|---|
| UK | 47,718 | 48,650 | 46,859 | 0.98 | 1.02 | 0.73 | 0.59 | 199 (23%) | 1,245 | 380 / 734 |
| US | 64,180 | 67,011 | 65,033 | 0.96 | 0.99 | 0.68 | 0.37 | 431 (26%) | 1,198 | 477 / 1,151 |
| DE | 3,895 | 3,739 | 3,792 | 1.04 | 1.03 | 0.92 | 0.89 | 8 (18%) | 86 | 22 / 86 |
| FR | 1,285 | 1,275 | 2,483 | 1.01 | **0.52** | 0.95 | 0.80 | 0 (26%) | 29 | 33 / 101 |
| IT | 3,773 | 1,540 | 557 | **2.45** | **6.77** | 0.76 | 0.67 | 51 (174%) | 155 | 72 / 209 |
| ES | 619 | 598 | 374 | 1.04 | 1.66 | 0.93 | 0.84 | 0 (53%) | 20 | 3 / 31 |

Coverage caveat: the EU invoice customers start late, so many months show zero on both sides and the EU correlations are flattered. Months with non-zero invoices: DE 29 of 36, IT 30, FR 14, ES 7.

The IT ratio was checked in plain SQL:
```sql
-- it_invoiced 3773 | it_net_received 1540 | it_shipped 557
WITH p AS (SELECT Customer_Name, TRY_CAST(REPLACE(Qty,',','') AS decimal(18,3)) q,
                  COALESCE(TRY_CONVERT(date,RIGHT(Date,10),103), TRY_CONVERT(date,Date,23)) d, LTRIM(RTRIM(Item_Code)) ic
           FROM dbo.INVOICES_TEMP WHERE Customer_Category='AMAZ' OR Customer_No LIKE 'AMAZ%')
SELECT (SELECT SUM(q) FROM p WHERE Customer_Name LIKE '%SARL Italy%' AND d BETWEEN '2023-09-01' AND '2026-08-31'
          AND (ic LIKE '[ADEU][0-9]%' OR ic LIKE '[ADEU][NL][0-9]%')),
       (SELECT SUM(net_received_units) FROM dbo.vw_amazon_vendor_inventory_monthly_sku WHERE marketplace='IT'),
       (SELECT SUM(shipped_units)     FROM dbo.vw_amazon_vendor_sales_monthly_sku     WHERE marketplace='IT');
```
Received ÷ shipped for the EU marketplaces: DE 0.99, **FR 0.51, IT 2.76, ES 1.60**. With DE, FR, IT and ES pooled: inv/rec 1.34, inv/shp 1.33, corr inv-rec 0.93.

**Lag test.** An invoice in month *t−k* is compared with receipts or shipments in month *t*.

| Mkt | corr vs rec k=0 / 1 / 2 | mean abs diff vs rec k=0 / 1 / 2 | corr vs shp k=0 / 1 / 2 | Best lag vs receipts |
|---|---|---|---|---|
| UK | 0.73 / 0.74 / 0.18 | **443** / 499 / 884 | 0.59 / **0.83** / 0.66 | 0 (by error; correlation ties with k=1) |
| US | **0.68** / 0.48 / −0.13 | **488** / 582 / 903 | 0.37 / **0.62** / 0.30 | 0 |
| DE | **0.92** / 0.79 / 0.77 | **27** / 48 / 53 | **0.89** / 0.84 / 0.77 | 0 |
| FR | **0.95** / 0.89 / 0.54 | **8.6** / 15 / 26 | 0.80 / **0.85** / 0.73 | 0 |
| IT | **0.76** / 0.56 / 0.44 | 62.0 / **61.8** / 66 | 0.67 / 0.66 / **0.68** | 0 (error ties) |
| ES | 0.93 / **0.94** / 0.64 | **6.6** / 7.1 / 9.3 | **0.84** / 0.73 / 0.59 | 0 (correlation ties with k=1) |
| EU pooled | **0.93** / 0.85 / 0.73 | **77** / 90 / 117 | | 0 |

ASIN-level check: invoice units for mapped codes with one ASIN, pooled over ASIN-months, invoice in *t−k* against `net_received_units` in *t*.

| Mkt | corr k=0 / 1 / 2 | mean abs diff k=0 / 1 / 2 | ASINs whose best lag is k=0 / k=1 |
|---|---|---|---|
| UK | **0.87** / 0.73 / 0.47 | **7.6** / 13.2 / 19.3 | 57 / 4 |
| US | **0.88** / 0.68 / 0.47 | **14.6** / 21.9 / 31.5 | 40 / 17 |
| DE | **0.85** / 0.69 / 0.51 | **2.1** / 4.2 / 6.0 | 20 / 2 |
| ES | **0.90** / 0.76 / 0.41 | **0.7** / 0.9 / 1.5 | 11 / 6 |

---

## 4. Lumpiness (per ASIN; mapped marketplaces only)

The universe is ASINs with at least one invoice line through a code that maps to exactly one ASIN (UK, US, DE and ES; FR and IT cannot be mapped). An ASIN's life starts in the first window month it appears in sales, inventory or invoices, and runs to the end of the window. A gap is the number of months between consecutive invoiced months, so 1 means back-to-back months.

| Mkt | ASINs | Median share of zero-invoice months (p25–p75) | Median gap, months (p75 / p90 across ASINs) | ASINs with ≥24 months of sales rows | ASINs with ≥24-month life | ASINs with ≤1 invoiced month |
|---|---|---|---|---|---|---|
| UK | 61 | 0.58 (0.36–0.76) | 1.0 (1.0 / 2.0) | 44 | 48 | 4 |
| US | 57 | 0.53 (0.22–0.83) | 1.0 (1.0 / 2.0) | 30 | 34 | 10 |
| DE | 25 | 0.75 (0.67–0.83) | 1.0 (1.9 / 3.4) | 7 | 13 | 1 |
| ES | 17 | 0.90 (0.81–0.97) | 1.0 (2.5 / 3.0) | 1 | 6 | 6 |
| All | 160 | 0.67 | 1.0 | 82 | 101 | 21 |

Invoices cluster in consecutive months (median gap 1) separated by long zero stretches, so more than half of ASIN-months are zero. The invoice history before the window (back to 2018) exists for UK and US at product-code level but not for the EU marketplaces.

---

## 5. Inventory NULLs (`vw_amazon_vendor_inventory_monthly_sku`)

| Mkt | ASIN-months | % rows with non-NULL `sellable_units` | % rows with non-NULL `open_po_units` | Months with any non-NULL sellable / open PO (of 36) | First month with open PO data | % non-NULL `net_received_units` |
|---|---|---|---|---|---|---|
| UK | 2,733 | 72.9 | 73.3 | 36 / 36 | 2023-10 | 100 |
| US | 2,544 | 67.5 | 66.2 | 36 / 36 | 2023-09 | 100 |
| DE | 1,437 | 45.9 | 33.8 | 36 / 30 | 2024-03 | 100 |
| IT | 1,230 | 38.1 | 33.8 | 32 / 30 | 2024-03 | 100 |
| FR | 1,407 | 35.0 | 18.1 | 36 / 26 | 2024-03 | 100 |
| ES | 1,119 | 23.9 | 11.9 | 36 / 21 | 2024-12 | 100 |

No month is 100% non-NULL in any marketplace. Where `sellable_units` is NULL, the same ASIN-month has mean `net_received_units` of 0.01–0.10 (vs 2–39 where it is non-NULL), and shipped_units > 0 in only 2–10% of rows (vs 35–87%). `sellable_units = 0` appears in 599 rows. NULL therefore *probably* means no stock or no activity, not missing data, but this is **not verified** against Amazon's report definition. `net_received_units` is never negative; it is 0 in 7,673 rows.

---

## 6. Forecast availability (`vw_amazon_forecast_versioned`)

| Mkt | Versions loaded | Generation date | ASINs | ASINs mapped to a code | Weeks | First week start – last week end | Σ mean_units |
|---|---|---|---|---|---|---|---|
| UK | 1 | 2026-10-11 | 95 | 63 | 48 | 2026-10-11 – 2027-09-11 | 15,792 |
| US | 1 | 2026-10-11 | 81 | 67 | 48 | same | 21,287 |
| DE | 1 | 2026-10-11 | 37 | 28 | 48 | same | 4,804 |
| FR | 1 | 2026-10-11 | 41 | 0 | 48 | same | 3,159 |
| IT | 1 | 2026-10-11 | 30 | 0 | 48 | same | 961 |
| ES | 1 | 2026-10-11 | 31 | 23 | 48 | same | 895 |

Only one version exists (pulled 2026-10-16), so **no past forecast can be compared with actuals yet.** `mean_units` has 0 NULLs. ASINs that sold in the last 3 loaded months but have no forecast: UK 0, US 2, DE 3, FR 1, IT 2, ES 2. ASINs with a forecast but no recent sales: UK 38, US 28, DE 8, FR 10, IT 9, ES 7.

Related: `amazon_vendor_sales` and `amazon_vendor_inventory` hold **only MONTH rows** (8,100 and 10,686 including `ALL`); no WEEK rows are loaded. The `ALL` row equals the sum of ASIN rows for shipped units in every marketplace (ratio 1.000).

---

## 7. Currency / quantity sanity

| Check | Result |
|---|---|
| Quantity, not value, used | Yes: `Qty`, `shipped_units`, `net_received_units`. No value column is used in any unit figure above. |
| Non-integer quantities | 0 product lines |
| Largest single invoice lines | US UN1530 942 units (2020-02-27, $45, Row_Sum 42,390); US UN1430 594; UK DN1430 584; UK DN1530 512. All consistent with Qty × Unit_Price. |
| Zero-price product lines | 154 lines, 225 units |
| Invoice code-months > 10× that code's median non-zero month | 3 of 2,827 (2 of 291 codes): UK DN8010 2023-10 46 vs median 2 (23×); UK DN8010 2024-02 45 (22.5×); UK D35152 2025-10 214 vs median 10 (21×) |
| Shipped ASIN-months > 10× median | 8 of 4,347: UK B01KTQ8HXG 2023-10 to 2024-01 (618–875 vs median 57, 11–15×; unmapped ASIN); DE B07S4MR18C 2024-12 55 vs 3; IT B071G3B66K 2025-10 13 vs 1; US B00BLM50EM 2023-09 87 vs 7; UK B01KTQ89GQ 2024-01 82 vs 8 |
| net_received ASIN-months > 10× median | 2 of 2,797: UK B01LX3PEMI 2023-11 259 vs 22; UK B082DWY9NN 2023-11 82 vs 8 |

The medians are taken over non-zero months only. Most ASIN medians over all months are 0, which makes a ratio test meaningless.

---

## What this means for the model

1. **UK and US can be used now.** They have 36 months on all three sources, about 47k and 64k invoiced units, totals within 2–4% of receipts, and inventory fields populated in 66–73% of ASIN-months. Mapping limits ASIN-level work to 75% (UK) and 85% (US) of invoice units until the map is completed.
2. **DE can be used with caution.** Totals reconcile (inv/rec 1.04) but invoices only start in 2024-04, giving about 2.3 years of history and only 7 ASINs with 24 or more months of sales rows.
3. **FR and IT cannot be used at ASIN level** until `amazon_asin_sku_map` has FR and IT rows (0 today). **ES** has only 7 invoiced months (customer `AMAZ2804` starts in 2026-01).
4. **Invoices are a good answer key against `net_received_units`, at lag 0,** for UK, US, DE, FR and ES. Pooled ASIN-month correlation is 0.85–0.90. At marketplace level the monthly error is large for UK and US (median about 23–26%, p90 about 1,200 units), so month-to-month timing noise is real. IT is not a clean answer key: 2.45× more units were invoiced than received.
5. **Best lag: 0.** Invoice month = receipt month wins on error in every marketplace and on ASIN-level correlation in all four mapped marketplaces. Against *shipped* units, lag 1 correlates better for UK, US and FR, which suggests stock bought one month is sold the next.
6. **Fix first:**
   - (a) Parse `INVOICES_TEMP.Date` with the ISO fallback, or all of 2024 disappears.
   - (b) Add FR and IT to the SKU map.
   - (c) Map the large unmapped UK and US products (DN1430/DN1530 floor lamps, U52107, UN1380, …).
   - (d) Fix the 5 DE/ES ASINs mapped to UK `D…` codes.
   - (e) Resolve the 6 US codes with 2 ASINs and the 3 NULL-code US rows.
   - (f) Identify Amazon by an explicit customer list, not a pattern (`AMAZ2990` is not Amazon; 5 Amazon accounts are not in category `AMAZ`).
7. **Surprise: the EU marketplaces are not separate stock pools.** FR receives 0.51 of what it sells, while IT receives 2.76× and ES 1.60× what they sell. DE invoices ship to Poland and Czechia. A per-marketplace balance model (stock + open PO vs demand) will be wrong for the EU unless DE, FR, IT and ES are modelled together. Pooled EU invoice-vs-receipt correlation is 0.93.
8. **Surprise: invoice data is lumpy.** The median ASIN has no invoice in 58% (UK) to 90% (ES) of months. Invoices come in consecutive-month runs separated by long gaps, so the target is intermittent and needs a zero-or-quantity treatment.
9. **Surprise: the balance inputs are NULL in many rows.** `sellable_units` and `open_po_units` are NULL in 27–88% of ASIN-months, and EU open-PO data only starts in 2024-03 (ES 2024-12). NULL looks like "zero stock" but this is not verified. The balance model can run reliably only for UK and US today.
10. **Surprise: there is only one forecast version (2026-10-11)**, so Amazon's forecast cannot be back-tested yet; it needs about 3 or more months of weekly pulls. Weekly sales and inventory are not loaded at all. Two more: the UK customer was briefly split into 24 FC-level accounts (2024-01 to 2024-08), and an invoicing-system change in 2024-01 altered the date, currency-label and credit-note conventions.

---

## Phase 1b

Run on 2026-10-16. Read-only: SELECTs only, over the same read-intent `DB_*` connection as Phase 1, with scratch scripts outside the repo. The question is how far `dbo.forecast_training_data` (FTD) can be trusted for Amazon, compared with the Phase 1 invoice view.

**How FTD is built** (`agents/demand-planning/database/forecast_training_data.sql`, run by `database/build_training_data.py`). The SQL matches the description given, with these differences:

- It groups by **item × bdm_name × bdm_code × region × territory × channel × month**, not item × BDM × region × month.
- Qty is cast to `DECIMAL(10,2)`.
- The DISCOUNT / Shipping Charge / SHIP exclusion tests the **untrimmed** `Item_Code`.
- The BDM-table region is collapsed with `MIN(Region)` per (Item_No, BDM). Where the BDM table has no match, region **falls back to the item prefix** (D/DN → 1 UK, E → 2 EU, U → 3 USA, A → 4 AU).
- The table was last built on **2026-10-08 18:36**, while invoices run to 2026-10-16. FTD's 2026-10 holds 870 Amazon units against 1,743 in invoices. All comparisons below stop at 2026-09.

**Reference list.** These are the Phase 1 §1b accounts: 48 in scope (UK 28, US 2, DE 6, FR 1, IT 10, ES 1), AU 5 and CA 1 out of scope, and five excluded (`AMAZSL1P`, `AMASSL1P`, `300031`, `30379`, `AMAZ2990`). Every `Customer_Category='AMAZ'` or `AMA%` account in INVOICES_TEMP is on this list.

> **Correction to Phase 1 (§1c):** only **three** accounts carry `Customer_Category = 'Other'`: `AMAZ2175_BWU6`, `AMAZ27050`, and some `AMAZSL1P` lines. The figure of "five" could not be reproduced.

"Units" below means positive Qty under the FTD filters unless stated. A **real product** line uses the Phase 1 code rule `^(DN|UN|EN|AN|DL|UL|EL|D|E|U|A)\d`.

### 1b-1. Customer list diff (`CUSTOMERS.CHANNEL` vs the Phase 1 list)

`CUSTOMERS` has 80 IDs with `CHANNEL = 'Amazon'`. 53 of them have invoice lines. Every invoiced one is on the reference list.

| Group | Customer_No | Territory / BDM (CUSTOMERS) | First – last invoice | Units (gross, FTD filter) | Net units | Phase 1 status |
|---|---|---|---|---|---|---|
| UK | `AMAZSL1` | UK&EU / Ankush Barot UK | 2018-01 – 2026-10 | 122,737 | 120,359 | in |
| UK FC-level | the 24 `AMAZBRS1 … AMAZB76` accounts | UK&EU / Ankush Barot UK | 2024-01 – 2024-09 | 24 – 527 each, 5,987 in total | 5,923 | in |
| US | `30003` | US / Ankush Barot USA | 2018-01 – 2026-10 | 142,997 | 136,274 | in |
| US FBA | `AMAZFB13` | US / **Ankush Barot** (the EU BDM) | 2026-08 – 2026-09 | 20 | 12 | in |
| DE | `AMAZ81000` + 5 small | UK&EU / Ankush Barot | 2024-04 – 2026-10 | 4,316 | 4,043 | in |
| FR | `AMAZ92110` | UK&EU / Ankush Barot | 2025-07 – 2026-10 | 1,858 | 1,774 | in |
| IT | `AMAZ20124` + 9 small | UK&EU / Ankush Barot | 2024-03 – 2026-10 | 4,283 | 4,110 | in |
| ES | `AMAZ2804` | UK&EU / Ankush Barot | 2026-01 – 2026-09 | 976 | 946 | in |
| **(a) Amazon in CUSTOMERS, not in the Phase 1 scope** | | | | | | |
| AU | `AMAZ2000, AMAZ2178_BWU2, AMAZ6105_PER4, AMAZ2175_BWU6, AMAZ3023_MEL5` | AUS / Ankush Barot AUS | 2020-12 – 2025-11 | 1,446 | 1,215 | out of scope |
| CA | `30115` | US / **Ankush Barot USA** | 2022-10 – 2024-11 | 78 | 56 | out of scope |
| Provision | `300031` | US / House USA | 2021-07 – 2024-01 | 90 (**all `ADJ`**) | −2 | excluded |
| Provision | `AMAZSL1P` | UK&EU / Ankush Barot | 2021-05 – 2024-01 | 45 (**all `ADJ`**) | −1 | excluded |
| Dormant | 27 IDs (`AMAZ6TE, AMAZBHX1/3/5, AMAZLTN1/2, AMAZXUKA, AMAZ255040, AMAZ2170, …`) | | no invoices | 0 | 0 | — |
| **`AMAZ2990`** | Amazing Products N.V. | CHANNEL = **B2B** (BDM Agnes Cousin) | 2018-02 – 2022-11 | 903 | 934 | excluded; FTD also excludes it ✔ |
| **(b) Phase 1 accounts not 'Amazon' in CUSTOMERS** | | | | | | |
| UK | `AMAZHP2`, `AMAZWS15`, `AMAZCV5` | **missing from CUSTOMERS**, so FTD channel is NULL | 2019-02 – 2021-02 | 9 + 2 + 0 = 11 | 11 | in |
| Excluded | `30379`, `AMASSL1P` | missing from CUSTOMERS | 2021 | 1 + 1 | 1 + 0 | excluded |

The other 45 in-scope accounts all have `CHANNEL = 'Amazon'`.

### 1b-2. Credit notes and returns (in-scope accounts, real product lines)

Values are `Row_Sum` in the invoice currency (GBP for UK, USD for US, EUR for EU). 2026 runs to 2026-10-16.

| Mkt | Year | Positive units | Credit rows | Credit units | Credit value | Credits as % of positive |
|---|---|---|---|---|---|---|
| UK | 2018 / 2019 / 2020 | 11,725 / 14,371 / 14,069 | 341 / 408 / 165 | −584 / −576 / −335 | −20.4k / −13.1k / −10.9k | 5.0 / 4.0 / 2.4 |
| UK | 2021 / 2022 / 2023 | 16,631 / 14,717 / 18,354 | 179 / 105 / 231 | −898 / −643 / −1,013 | −34.8k / −14.5k / −25.9k | 5.4 / 4.4 / 5.5 |
| UK | 2024 / 2025 / 2026 | 15,239 / 15,034 / 8,429 | 31 / 19 / 4 | −171 / −230 / −37 | −4.8k / −13.2k / −2.1k | 1.1 / 1.5 / 0.4 |
| US | 2018 / 2019 / 2020 | 11,544 / 13,129 / 18,598 | 31 / 14 / 78 | −194 / −174 / −1,104 | −2.6k / −6.6k / −38.5k | 1.7 / 1.3 / 5.9 |
| US | 2021 / 2022 / 2023 | 15,853 / 9,386 / 15,932 | 51 / 41 / 103 | −1,019 / −857 / **−2,296** | −34.4k / −39.3k / −38.5k | 6.4 / 9.1 / **14.4** |
| US | 2024 / 2025 / 2026 | 21,841 / 20,487 / 16,125 | 28 / 0 / 2 | −77 / 0 / −8 | −4.8k / 0 / −1.9k | 0.4 / 0.0 / 0.05 |
| DE | 2024 / 2025 / 2026 | 333 / 2,006 / 1,976 | 3 / 0 / 0 | −8 / 0 / 0 | −554 | 2.4 / 0 / 0 |
| FR | 2025 / 2026 | 765 / 1,093 | 0 / 4 | 0 / −11 | −475 | 0 / 1.0 |
| IT | 2024 / 2025 / 2026 | 772 / 1,608 / 1,902 | 0 / 4 / 0 | 0 / −20 / 0 | −1,398 | 0 / 1.2 / 0 |
| ES | 2026 | 976 | 0 | 0 | 0 | 0 |

- **Zero-Qty** product lines: UK 20, US 4, IT 1.
- **NULL-Qty** product lines: 0. All 6,334 in-scope rows with NULL or blank Qty are payments (`PYMT`), journals (`JE`), memo lines or CM discount lines, apart from one `ADJ` line.
- Under the FTD prefix filter (rather than real products), credit **values** are dominated by `ADJ` value adjustments of Qty −1. Examples: UK 2018-02-15 `ADJ` −£45,129 and US 2023-12-11 `ADJ` −$40,844.

**Do the credits reverse an invoice?** For each credit line, I looked for a positive line of the same item and marketplace in the same or previous month.

| Mkt | Era | Credit rows | Same qty, same month | Same qty, previous month | Item invoiced, different qty | No invoice of the item | Same qty matches ≥2 invoices (cancel and re-issue likely) |
|---|---|---|---|---|---|---|---|
| UK | 2018–23 | 1,429 (−4,049 u) | 667 (−1,364 u) | 198 (−873) | 331 (−1,240) | 233 (−572) | 711 (−1,518) |
| UK | 2024–26 | 54 (−438 u) | 23 (−216) | 8 (−48) | 20 (−164) | 3 (−10) | 25 (−206) |
| US | 2018–23 | 318 (−5,644 u) | 159 (−1,675) | 40 (−702) | 110 (−3,187) | 9 (−80) | 108 (−799) |
| US | 2024–26 | 30 (−85 u) | 21 (−54) | 2 (−10) | 7 (−21) | 0 | 22 (−62) |

No invoice number holds both a positive and a negative line for the same item. From 2024, every credit is a `CM` document (95 lines, −562 units).

The pattern differs by size:
- **Many small credits** have an equal-quantity positive line in the same month. That fits cancel-and-re-issue.
- **The large US credits are bulk returns, not reversals.** They are credited at a lower unit value than the invoice price.

Surprising number: US 2023-02 credits (−1,162) are larger than that month's gross (745), so the net month is negative.
```sql
SELECT Inv_No, Date, Qty, Row_Sum FROM dbo.INVOICES_TEMP
WHERE Customer_No='30003' AND TRIM(Item_Code)='UN1530'
  AND COALESCE(TRY_CONVERT(DATE,RIGHT(Date,10),103), TRY_CAST(RIGHT(Date,10) AS DATE)) BETWEEN '2023-01-01' AND '2023-03-31';
-- 142226 | 17/02/2023 | -525 | -5,250.00   ($10/unit; the invoices are $45/unit)
-- 142233 | 17/02/2023 | -215 | -1,075.00   ($5/unit)
-- US 30003 U-items 2023-02: gross 745, credits -1,162
```

### 1b-3. Rows the other filters drop or let through (in-scope accounts, positive Qty)

| Filter | Units | Rows | Detail |
|---|---|---|---|
| Item prefix outside DN/A/D/E/U (dropped) | 3,435 | 3,410 | All 11 such codes: `Q` carriage 2,741; `OS` 332; `Shortage claim` 189; `Sales Promo & Rebates` 78; `NOTE` 56; `Open AR` 22; `SP25201G` 8; `S35107A` 4; `COOP FEES` 3; `S25201G` 1; `SDE35050E` 1. The four `S…` codes (14 units) may be parts or sets (*not verified*). The rest are not products, so dropping them is correct. |
| `DISCOUNT` / `Shipping Charge` / `SHIP` (dropped) | 0 | 2 | Two `DISCOUNT` lines with Qty 0 or blank. `SHIP` and `Shipping Charge` never appear on Amazon accounts. No padded variants exist. |
| Date parse returns NULL (dropped) | 0 | 0 | 0 of 49,451 in-scope rows (0 of 344,086 in the whole table) |
| **Non-product codes that pass the prefix filter (wrongly kept)** | **290** | 290 | `ADJ` 260 (2018–2023, UK and US; value adjustments with Qty = 1 and Row_Sum £/$1.84M in total), `DS` 26 (2018), `ADMIN` 2, `Damage allowance` 2. **FTD's Amazon slice holds 429 such "units"** in total, including the provision accounts' `ADJ` lines. |
| Real product codes that FTD drops | 0 | 0 | |

```sql
SELECT SUM(monthly_qty) FROM dbo.forecast_training_data
WHERE channel='Amazon' AND item_code IN ('ADJ','ADMIN','DS','Damage allowance');   -- 429
```

### 1b-4. Reconciliation (months to 2026-09)

- **In-scope** = in-scope accounts with the exact FTD parse and filters.
- **FTD** = `forecast_training_data` with `channel='Amazon'`, summed over BDM and region.
- **Phase 1** = net real-product units, all months. This definition reproduces the Phase 1 window totals exactly: UK 47,718 and US 64,180.
- **Rebuilt** = invoices rebuilt with the FTD logic via `CUSTOMERS.CHANNEL`. It matches FTD **exactly in every item-month**, so FTD is not stale for closed months; the gaps come only from the account set.

| Year | In-scope | FTD | Phase 1 (net) | FTD − in-scope | In-scope − Phase 1 | FTD − Phase 1 |
|---|---|---|---|---|---|---|
| 2018 | 23,336 | 23,336 | 22,491 | 0 | 845 | 845 |
| 2019 | 27,564 | 27,555 | 26,750 | −9 | 814 | 805 |
| 2020 | 32,750 | 32,754 | 31,228 | 4 | 1,522 | 1,526 |
| 2021 | 32,538 | 32,655 | 30,567 | 117 | 1,971 | 2,088 |
| 2022 | 24,113 | 24,689 | 22,603 | 576 | 1,510 | 2,086 |
| 2023 | 34,296 | 34,865 | 30,977 | 569 | 3,319 | 3,888 |
| 2024 | 38,185 | 38,440 | 37,929 | 255 | 256 | 511 |
| 2025 | 39,901 | 40,037 | 39,650 | 136 | 251 | 387 |
| 2026 (to Sep) | 28,759 | 28,759 | 28,704 | 0 | 55 | 55 |
| **Total** | **281,442** | **283,090** | **270,899** | **+1,648** | **+10,543** | **+12,191** |

**Which customers explain the gaps:**
- **FTD − in-scope (+1,648):**
  - AU accounts add +1,446 (`AMAZ2000` 1,079, `AMAZ2178_BWU2` 326, `AMAZ6105_PER4` 17, `AMAZ2175_BWU6` 16, `AMAZ3023_MEL5` 8).
  - CA `30115` adds +78.
  - The provision accounts add +135 of `ADJ` (`300031` 90, `AMAZSL1P` 45).
  - `AMAZHP2` and `AMAZWS15` take away −11, because they are missing from CUSTOMERS.
- **In-scope − Phase 1 (+10,543):** credits that FTD removes (about 10,250 units, nearly all UK and US before 2024), plus the 290 non-product "units" from 1b-3.

**Top 10 item-months, FTD − in-scope.** All are AU items:

| Item-month | Units | Account |
|---|---|---|
| AN1430 2023-05 | +51 | `AMAZ2000` |
| AN1530 2022-02 | +46 | `AMAZ2000` |
| AN1510 2022-04 | +40 | `AMAZ2000` |
| AN1430 2022-11 | +39 | `AMAZ2000` |
| AN1430 2023-06 | +39 | `AMAZ2000` |
| AN1430 2023-08 | +36 | `AMAZ2000` |
| AN1430 2023-01 | +36 | `AMAZ2000` |
| AN1530 2022-10 | +33 | `AMAZ2000` |
| AN1180 2025-09 | +32 | `AMAZ2178_BWU2` |
| AN1180 2025-07 | +32 | `AMAZ2178_BWU2` |

**Top 10 item-months, in-scope (gross) − Phase 1 (net).** All are US credits on `30003`:

| Item-month | Gross | Net | Difference |
|---|---|---|---|
| UN1530 2023-02 | 390 | −350 | 740 |
| UN1530 2022-03 | 619 | 142 | 477 |
| UN1530 2020-10 | 182 | −111 | 293 |
| UN1530 2023-01 | 276 | 45 | 231 |
| UN1530 2021-05 | 331 | 120 | 211 |
| UN1430 2021-01 | 0 | −210 | 210 |
| UN1530 2020-12 | 750 | 566 | 184 |
| UN1530 2022-02 | 185 | 9 | 176 |
| DN1370 2023-12 | 232 | 61 | 171 |
| UN1530 2021-01 | 164 | 27 | 137 |

DN1370 2023-12 is a UK item on `AMAZSL1`.

### 1b-5. Date parse

Whole INVOICES_TEMP table. The session runs as `us_english` with DATEFORMAT `mdy`.

| Raw format | Rows | 103 NULL | TRY_CAST NULL | Both non-NULL and **different** | Final NULL | Final range |
|---|---|---|---|---|---|---|
| `dd/mm/yyyy` | 256,897 | 0 | 156,853 | **93,094** | 0 | 2018-01-01 – 2023-12-31 |
| `Ddd/mm/yyyy` | 57,370 | 0 | 37,032 | **18,757** | 0 | 2025-01-01 – 2026-10-16 |
| `yyyy-mm-dd` | 29,819 | 29,819 | 0 | 0 | 0 | 2024-01-01 – 2024-12-31 |

**Sample:** 30 rows from 2024 and 30 from 2025, over in-scope accounts in a fixed hash order.
- **2024:** all 30 are ISO text (e.g. `2024-04-18`). 103 returns NULL; TRY_CAST and the final date equal the text.
- **2025:** all 30 are `D` + dd/mm/yyyy, and 103 is correct on every row.
  - 16 rows have day > 12, so TRY_CAST returns NULL.
  - On 13 of the 14 rows with day ≤ 12, **TRY_CAST swaps day and month**. Examples: `D04/02/2025` → 103 gives 2025-02-04, TRY_CAST gives 2025-04-02. `D10/09/2025` → 2025-09-10 vs 2025-10-09.
  - `D06/06/2025` gives the same date either way.

Across the table, **111,851 rows** get a different date from TRY_CAST than from the 103 parse. The final value is right on every row only because `COALESCE` tries 103 first, and 103 never fails on slash dates. The day/month ambiguity is therefore latent, not active. Reversing the COALESCE order, or using TRY_CAST alone, would misdate about a third of the table.

### 1b-6. Fan-out

| Check | Result |
|---|---|
| Amazon rows in FTD | 6,013 |
| Distinct item × bdm_name × bdm_code × region × month | 6,013, so **unique** (0 duplicate keys; no Amazon key is shared with another channel) |
| Distinct item × month | 5,940. So 73 item-months are split across more than one BDM (e.g. UK and EU), which is expected. |
| Multi-region (Item_No, BDM) pairs in `dbo.BDM` | 11, **all under BDM `SW`**. Five are Amazon items (`D52107, D53060, D91707, DN1350, DN1380`), but no Amazon customer has BDM `SW`. Amazon customers map to `AMUK`, `AKB`, `AKB1`, `AKB2` and `HU`. **No fan-out for Amazon.** |
| `Territory.Department` duplicates | 0 |
| `CUSTOMERS.ID` duplicates | 1 (`JERSJE27`, a B2B customer, not Amazon). This should double that customer's 49 units in FTD. *Not verified in FTD*, which has no Customer_No. |

The `region` column is **not mostly from the BDM table** for Amazon. 152,906 of 283,960 units (54%) take the prefix fallback. `ADJ` lines under the UK and US BDMs get region `4 AU` (399 units), and `Damage allowance` gets `1 UK`.

### 1b-7. Marketplace identification

| Mkt | Accounts (Customer_No) | FTD `bdm_name` | Ship-to countries in invoices (positive units) | Notes |
|---|---|---|---|---|
| UK | `AMAZSL1`, the 24 FC accounts, `AMAZHP2/WS15/CV5` | Ankush Barot UK (`AMUK`) | UK only | Clean |
| US | `30003`, `AMAZFB13` | Ankush Barot USA (`AKB`) | US only | **`AMAZFB13` sits under BDM "Ankush Barot" (`AKB1`, the EU bucket).** Its 20 units appear in FTD as EU-BDM rows with region 3 USA. **CA `30115` sits in `AKB`**, so FTD's US bucket includes Canada (78 units). |
| DE | `AMAZ81000`, `AMAZ44145/47495/55040/66340/75177` | Ankush Barot (`AKB1`) | `AMAZ81000`: Germany 2,636, **Poland 1,621**, Czechia 32, Austria 7, UK 7. `AMAZ55040` and `AMAZ66340` ship to Poland only. | DE-billed, but 38% of units ship to PL/CZ |
| FR | `AMAZ92110` | Ankush Barot (`AKB1`) | France 1,624, Spain 130, Italy 88, UK 16 | 22 units billed in GBP |
| IT | `AMAZ20124` + 9 small | Ankush Barot (`AKB1`) | Italy, plus UK 26 on `AMAZ20124` | |
| ES | `AMAZ2804` | Ankush Barot (`AKB1`) | Spain only | |
| AU / CA / provisions | as in 1b-1 | AKB2 / **AKB** / HU and AKB1 | | |

Every in-scope account maps to one *billing* marketplace by its name, so none is unassignable by account. These can't be pinned to a single *ship-to* marketplace:
- `AMAZ81000` (DE/PL/CZ)
- `AMAZ55040` and `AMAZ66340` (named DE, ship to PL)
- `AMAZ92110` (FR/ES/IT)

**In FTD, DE, FR, IT and ES all share BDM `AKB1`, region `2 EU`**, so they cannot be separated. FTD can give UK (`AMUK`), US + CA (`AKB`) and pooled EU + US FBA (`AKB1`).

### 1b-8. Net versus gross vs `net_received_units` (in-scope accounts, real products; receipts from `vw_amazon_vendor_inventory_monthly_sku`)

| Mkt | Period | Gross | Net | Received | Gross ÷ received | Net ÷ received | Correlation gross / net | Mean absolute error gross / net | Months where gross ≠ net |
|---|---|---|---|---|---|---|---|---|---|
| UK | 2024 | 15,239 | 15,068 | 17,121 | 0.890 | 0.880 | **0.667** / 0.645 | **467** / 477 | 6 |
| UK | 2025 | 15,034 | 14,804 | 14,925 | 1.007 | 0.992 | **0.880** / 0.871 | **325** / 331 | 9 |
| US | 2024 | 21,841 | 21,764 | 21,438 | 1.019 | 1.015 | **0.754** / 0.749 | **473** / 477 | 3 |
| US | 2025 | 20,487 | 20,487 | 22,107 | 0.927 | 0.927 | 0.512 / 0.512 | 514 / 514 | 0 |
| UK | Phase 1 window (36 months) | 48,734 | 47,718 | 48,650 | 1.002 | 0.981 | **0.733** / 0.729 | 445 / **443** | |
| US | Phase 1 window (36 months) | 64,486 | 64,180 | 67,011 | 0.962 | 0.958 | **0.688** / 0.681 | **484** / 488 | |

Gross correlates as well as net or slightly better in every period. The gap is tiny because credits were 0–1.5% from 2024 on. The two differ materially only **before 2024** (credits 4–14%), and no receipts data exists then to test which is right.

### What this means for the answer key

1. **FTD can be used for Amazon item-level history for UK and US, but not as it stands.** The rebuilt invoices match it exactly in every item-month to 2026-09. Its Amazon channel adds AU (1,446 units), CA (78, inside the US BDM), and provision `ADJ` lines (135). It misses `AMAZHP2` and `AMAZWS15` (11 units, missing from CUSTOMERS). Filtering FTD by `bdm_name` gives UK cleanly but US only with Canada mixed in.
2. **FTD cannot give DE, FR, IT or ES separately.** All four sit in one EU BDM bucket (`AKB1` / `2 EU`), which also carries the US FBA account (20 units). Its `region` comes from the prefix fallback for 54% of Amazon units, so it is not a marketplace field. An EU-pooled series is possible; per-marketplace EU needs `Customer_No`.
3. **FTD keeps 429 "units" of non-product codes** (`ADJ` 399, `DS` 26, `ADMIN` 2, `Damage allowance` 2), because the A/D prefix test lets them through. They are small in total but each one is a false 1-unit order.
4. **The answer key should be gross (Qty > 0), and FTD's gross is right in this respect.** For 2024–2026 gross and net differ by under 1.5% and gross matches receipts as well or slightly better. Before 2024, the large credits are mostly bulk returns credited at a lower value (e.g. US `UN1530` −525 at $10 vs $45 invoiced). They are not order reversals and would make order months negative under net. Small same-quantity credits look like cancel-and-re-issue, so they also argue for gross. A returns series could be kept separately as a feature.
5. **Caveat on gross:** for 2018–2023, roughly 1,500 UK and 800 US credited units match two or more equal invoices, which suggests re-issue. Gross may overstate those years by about 1.7% (UK) and 0.9% (US). *Not verified* line by line, because credit notes carry no reference to the original invoice.
6. **Recommended Amazon account list:** the 48 Phase 1 in-scope accounts, built as an explicit `Customer_No` → marketplace list rather than from `CUSTOMERS.CHANNEL`. Exclude `AMAZ2990`, the provisions `300031`, `AMAZSL1P` and `AMASSL1P`, and intercompany `30379`.
7. **Accounts needing a business decision:**
   - `AMAZFB13`: US FBA. Treat as US? It sits under an EU BDM.
   - `30115`: Canada. Exclude, or count as US?
   - `AMAZ81000`, `AMAZ55040`, `AMAZ66340`: DE-billed but shipping to PL/CZ. Count as DE, or pool the EU?
   - `AMAZ92110`: FR with ES/IT ship-to.
   - The five AU accounts: confirm out of scope.
   - The four `S…` codes (14 units): products or not?
8. **Date parsing is correct but fragile.** The 103-first `COALESCE` must stay as is. TRY_CAST alone would swap day and month on 111,851 rows.
9. **Fix first, before FTD is used for Amazon:**
   - (a) Select by explicit `Customer_No` (or add it to the grain) so marketplaces can be separated and AU, CA and the provisions dropped.
   - (b) Use a real product-code rule instead of the bare A/D/E/U prefix, so `ADJ`/`DS`/`ADMIN`/`Damage allowance` are excluded.
   - (c) Add `AMAZHP2`, `AMAZWS15` and `AMAZCV5` to CUSTOMERS, or handle them in the account list.
   - (d) Rebuild FTD after month close: the 2026-10-08 build holds half of October.
   - (e) Remove the `JERSJE27` duplicate in CUSTOMERS (not Amazon, but it inflates FTD).
10. **Not changed by Phase 1b:** Phase 1's lag-0 receipts finding and its correlations stand, because the net figures are reproduced exactly. Switching to gross changes the UK and US window totals by +2.1% and +0.5%.
