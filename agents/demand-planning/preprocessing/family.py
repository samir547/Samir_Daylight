"""
Product Family enrichment (reporting tag, not a modelling input).

Joins [dbo].[MASTER RODUCT TABLE].[PRODUCT FAMILY] onto item_code and appends a
`family` column to output frames. Item codes with no master-product row are
tagged config.FAMILY_UNKNOWN ('Unknown') rather than dropped or guessed, so the
known ~34% coverage gap (overwhelmingly AU A-prefix SKUs) stays visible in every
artifact.

Applied as a post-model Python merge on the output frames (not in the SQL
staging build). Rationale: the family tag is pure reporting metadata that never
enters the model, so it does not belong in forecast_training_data; doing it here
keeps it decoupled from the heavier staging rebuild and lets us log exactly the
unmatched item_codes that are actually in this run's forecastable scope, rather
than the full master-list gap.
"""
from __future__ import annotations

import logging

import pandas as pd

import config

logger = logging.getLogger(__name__)


def build_family_map(master_df: pd.DataFrame) -> pd.Series:
    """item_code → family Series, indexed by (stripped) item_code."""
    m = master_df.copy()
    m["item_code"] = m["item_code"].astype(str).str.strip()
    m = m.drop_duplicates(subset=["item_code"], keep="first")
    return m.set_index("item_code")["family"]


def enrich(df: pd.DataFrame, family_map: pd.Series) -> pd.DataFrame:
    """
    Add a `family` column keyed on item_code. Unmatched item_codes (and any
    blank/whitespace family values) become config.FAMILY_UNKNOWN.

    Returns a copy; a no-op (adds an all-Unknown column) if df has no item_code.
    """
    out = df.copy()
    if "item_code" not in out.columns:
        out["family"] = config.FAMILY_UNKNOWN
        return out

    key = out["item_code"].astype(str).str.strip()
    fam = key.map(family_map)
    fam = fam.where(fam.notna() & (fam.astype(str).str.strip() != ""), config.FAMILY_UNKNOWN)
    out["family"] = fam.values
    return out


def unmatched_in_scope(item_codes, family_map: pd.Series) -> pd.DataFrame:
    """
    Return the item_codes in `item_codes` that have no (or blank) family, as a
    one-column frame ready for output/unmatched_family_log.csv.

    `item_codes` is the run's actual forecastable/active set — NOT the full
    master gap — so the log reflects only what this run could not tag.
    """
    codes = sorted({str(c).strip() for c in item_codes if str(c).strip()})
    matched = family_map.copy()
    matched = matched[matched.astype(str).str.strip() != ""]
    matched_keys = set(matched.index)
    missing = [c for c in codes if c not in matched_keys]
    return pd.DataFrame({"item_code": missing})
