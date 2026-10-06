"""Reconnaissance-only: detects review language (French/German/Italian/Spanish/other) for every
review in dbo.sentiment_analysis_reviews, using a fully offline detector (langdetect) -- makes
ZERO Claude/Groq API calls. Run this FIRST, before building or running any translation step, to
see exactly how many reviews would actually need translating and in which languages, so that
step (a later, separate script) can be scoped and costed precisely instead of guessed at.

Requires: pip install langdetect --break-system-packages
(Offline, pure-algorithmic detector -- no network calls at detection time, no API key needed.)

Detection is skipped (assumed English) for reviews under WORD_MIN words. langdetect is unreliable
on short text -- verified against real short reviews from this dataset: "Not working" comes back
99.99% confident Afrikaans, "Excellent" comes back 99.99% confident Catalan. Confidence scores
don't help (both were near-100%); only a length floor does. WORD_MIN=5 was chosen because every
short English review tested (Not working / Excellent / Nice / Perfect / Not received / Love it! /
Great / Great great great) stayed correctly assumed-English at this threshold, while every real
non-English review tested at >=5 words (French, German, Spanish, Swedish) was detected correctly.

Usage:
    python -m scripts.detect_review_languages                       # full scan, prints summary + samples
    python -m scripts.detect_review_languages --review-type product
    python -m scripts.detect_review_languages --samples 10           # more example uids per language
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from typing import Any, Dict, List, Optional

from daylight_sentiment.config import Config, DatabaseSettings, LLMProvider
from daylight_sentiment.infra.db import REVIEWS_TABLE, connect
from daylight_sentiment.infra.translation import WORD_MIN, detect_language as _shared_detect_language

# Samir explicitly asked about these four; everything else non-English is still reported
# below, since restricting *detection itself* to only these four would silently miss real
# candidates -- e.g. Swedish reviews are already known to exist in this dataset from earlier
# spot-checks (Magnificent Pro, U25090) and Samir's list may not be exhaustive.
REQUESTED_LANGUAGES = {"fr": "French", "de": "German", "it": "Italian", "es": "Spanish"}


def fetch_reviews(settings: DatabaseSettings, review_type: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = connect(settings)
    try:
        cur = conn.cursor()
        if review_type:
            cur.execute(
                f"SELECT uid, review_type, review_source, product_name, review_text "
                f"FROM dbo.{REVIEWS_TABLE} WHERE review_text IS NOT NULL AND review_type = ?",
                review_type)
        else:
            cur.execute(
                f"SELECT uid, review_type, review_source, product_name, review_text "
                f"FROM dbo.{REVIEWS_TABLE} WHERE review_text IS NOT NULL")
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()
    return rows


def detect_language(text: str) -> str:
    """ISO 639-1 code, or 'en' for short text (see WORD_MIN) or on any detection failure.
    Thin wrapper around daylight_sentiment.infra.translation.detect_language, which returns None for those same
    cases (its callers only check "is this a target language", no need for an 'en' bucket) --
    this script maps None to 'en' specifically for its own by-language reporting below."""
    return _shared_detect_language(text) or "en"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--review-type", choices=["product", "service"], default=None,
                    help="Restrict to one review type (default: both)")
    p.add_argument("--samples", type=int, default=3,
                    help="How many example uids to print per non-English language (default: 3)")
    args = p.parse_args()

    config = Config.from_env(llm_provider=LLMProvider.CLAUDE)  # provider is irrelevant here -- no LLM calls made
    reviews = fetch_reviews(config.db, args.review_type)
    print(f"Scanned {len(reviews)} reviews (zero API calls made -- offline detection only)\n")

    by_lang: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in reviews:
        lang = detect_language(r["review_text"])
        by_lang[lang].append(r)

    total_non_english = sum(len(v) for k, v in by_lang.items() if k != "en")
    print(f"English:                     {len(by_lang.get('en', []))}")
    print(f"Non-English (any language):  {total_non_english}\n")

    print("--- Samir's requested languages ---")
    requested_total = 0
    for code, name in REQUESTED_LANGUAGES.items():
        rows = by_lang.get(code, [])
        requested_total += len(rows)
        print(f"  {name} ({code}): {len(rows)}")
        for r in rows[: args.samples]:
            preview = (r["review_text"] or "")[:70].replace("\n", " ")
            print(f"      {r['uid']}  {preview!r}")
    print(f"  TOTAL matching Samir's 4 languages: {requested_total}\n")

    other_non_english = {k: v for k, v in by_lang.items() if k != "en" and k not in REQUESTED_LANGUAGES}
    if other_non_english:
        print("--- Other non-English languages found (not in Samir's list -- worth a decision) ---")
        for code, rows in sorted(other_non_english.items(), key=lambda x: -len(x[1])):
            print(f"  {code}: {len(rows)}")
            for r in rows[: args.samples]:
                preview = (r["review_text"] or "")[:70].replace("\n", " ")
                print(f"      {r['uid']}  {preview!r}")

    print(f"\nEstimated Claude calls to translate ONLY Samir's 4 languages: {requested_total}")
    print(f"Estimated Claude calls to translate EVERY non-English review:  {total_non_english}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
