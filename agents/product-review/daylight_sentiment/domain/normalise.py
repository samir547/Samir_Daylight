"""Normalisation of model responses onto the fixed classification schema,
plus batched-response alignment. Pure functions -- no I/O.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from daylight_sentiment.domain.taxonomy import ASPECTS, SECTORS, SENTIMENTS

# primary_sector is NVARCHAR(50) in the DB -- cap any model-proposed label
# well under that.
_SECTOR_LABEL_MAX_LEN = 40
_SECTOR_LABEL_STRIP_RE = re.compile(r"[^a-z0-9_]+")


def _sanitise_sector_label(raw_value: str) -> str:
    """Turn a free-text sector the model proposed into a short snake_case slug.

    SECTORS is a seed list, not a hard cap (see taxonomy.py) -- a value
    outside it isn't necessarily wrong, it may be a real use case Samir's
    evidence audit never saw. This slugifies it into something safe to store
    and aggregate on, rather than discarding it to "general" the way ASPECTS
    (still a closed set) does.
    """
    slug = raw_value.strip().lower().replace("-", "_").replace(" ", "_")
    slug = _SECTOR_LABEL_STRIP_RE.sub("", slug)
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug[:_SECTOR_LABEL_MAX_LEN]


def _normalise(raw: Dict[str, Any], review_type: str) -> Dict[str, Any]:
    """Coerce a model response onto the fixed schema."""
    sentiment = str(raw.get("sentiment", "neutral")).strip().lower()
    if sentiment not in SENTIMENTS:
        sentiment = "neutral"

    try:
        confidence = float(raw.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = min(max(confidence, 0.0), 1.0)

    themes = raw.get("key_themes") or []
    if isinstance(themes, str):
        themes = [themes]
    themes = [str(t).strip().lower() for t in themes if str(t).strip()][:6]

    out: Dict[str, Any] = {"sentiment": sentiment, "confidence": confidence,
                           "key_themes": themes}

    if review_type == "product":
        sector = str(raw.get("primary_sector", "general")).strip().lower()
        if sector not in SECTORS:
            # Not one of the seeded segments -- let it through as a new,
            # sanitised label instead of collapsing it to "general". Only
            # "general", or an empty/unusable label, actually falls back.
            sector = _sanitise_sector_label(sector)
        out["primary_sector"] = sector or "general"
    else:
        aspect = str(raw.get("primary_aspect", "general_experience")).strip().lower()
        out["primary_aspect"] = aspect if aspect in ASPECTS else "general_experience"
    return out


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def _align_batch(raw: Dict[str, Any], reviews: List[Dict[str, Any]],
                 review_type: str) -> List[Optional[Dict[str, Any]]]:
    """Map a batched response back onto the input order via the echoed id.

    Returns a slot per input review; None marks a review the model dropped, so
    the caller can retry just those individually rather than the whole batch.
    """
    results = raw.get("results")
    if isinstance(raw, list):
        results = raw
    if not isinstance(results, list):
        return [None] * len(reviews)

    out: List[Optional[Dict[str, Any]]] = [None] * len(reviews)
    leftovers: List[Dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("id")) - 1
        except (TypeError, ValueError):
            leftovers.append(item)
            continue
        if 0 <= idx < len(reviews) and out[idx] is None:
            out[idx] = _normalise(item, review_type)
        else:
            leftovers.append(item)

    # A model that returned the right count but omitted/duplicated ids still
    # carries usable data -- fill remaining slots positionally, in order.
    for item in leftovers:
        for i in range(len(out)):
            if out[i] is None:
                out[i] = _normalise(item, review_type)
                break
    return out
