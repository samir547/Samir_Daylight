"""Amazon order forecast: predicts what Amazon will order from Daylight, monthly per ASIN.

Layers (see README.md). The forecast layer (series, baselines, backtest) works
in ASIN space and never imports the SKU map. The map is used only in SQL views
(sql/002_views.sql) that join stored forecasts to product codes.
"""
