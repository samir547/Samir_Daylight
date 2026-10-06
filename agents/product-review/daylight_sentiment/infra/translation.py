"""Shared translation logic: language detection (offline, zero API cost) + LLM translation +
narrow DB write-back. Used both by daylight_sentiment.cli (as an automatic phase on every
regular run) and by translate_reviews.py (a standalone CLI for ad hoc backfills / dry-run
testing without a full pipeline run).

Callers pass in an already-constructed analyzer/DB-writer (duck-typed: analyzer needs only
a ._call(prompt: str) -> dict method; writer needs only a ._connect() method returning a
pyodbc connection) rather than this module importing the concrete classes. That keeps
run_translation_pass() usable with whichever provider a run already built, with no second
API client constructed. SettingsWriter below is the stock writer for callers that only
have a DatabaseSettings in hand.

Lived at the repo root as translation_lib.py until 2026-09-17 (ISSUE-08: the old monolith
imported this module, so this module couldn't import anything back). Post-refactor,
infra/db is a leaf module, so REVIEWS_TABLE is imported from there instead of duplicated.

Cost control, by construction:
  1. Language detection (langdetect, fully offline) -- zero API calls, zero cost, for every
     review. Only rows detected in TARGET_LANGUAGES proceed to step 2.
  2. Translation -- one paid call per matched review, only for the confirmed subset, and
     only for rows that haven't been translated yet (review_text_en IS NULL).

Detection skips (assumes not-a-target-language) for reviews under WORD_MIN words --
langdetect is unreliable on short text, verified against real reviews from this dataset:
"Not working" comes back 99.99% confident Afrikaans; "Excellent" comes back 99.99% confident
Catalan. Confidence scores don't help (both near-100%); only a length floor does. WORD_MIN=5
was chosen because every short English review tested stayed correctly excluded at this
threshold, while every real non-English review tested at >=5 words was detected correctly.

TARGET_LANGUAGES = Samir's 4 requested languages (fr/de/it/es) plus nl/sv/pl/cs, confirmed
via sample inspection of detect_review_languages.py's reconnaissance run (2026-09-11,
2,933 reviews scanned) to be genuine correct detections, not false positives. Deliberately
excludes what that scan also flagged (ca/da/ro/af/no/tl/cy/pt) -- those were majority
false-positive English text on inspection of the printed samples.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from langdetect import detect, DetectorFactory, LangDetectException

from daylight_sentiment.config import DatabaseSettings
from daylight_sentiment.infra.db import REVIEWS_TABLE, connect

logger = logging.getLogger("translation_lib")

DetectorFactory.seed = 0
WORD_MIN = 5

TARGET_LANGUAGES = {
    "fr": "French", "de": "German", "it": "Italian", "es": "Spanish",
    "nl": "Dutch", "sv": "Swedish", "pl": "Polish", "cs": "Czech",
}

TRANSLATION_PROMPT = """Translate the following product/service review into natural, fluent English.
Preserve the tone and meaning as closely as possible. Do not add commentary, notes, or
explanations -- translation only.

Review ({language_name}):
{text}

Respond ONLY with JSON (no markdown, no explanation):
{{"translation": "..."}}"""


def detect_language(text: str) -> Optional[str]:
    """ISO 639-1 code, or None for short text (see WORD_MIN) or on any detection failure --
    both cases are treated as "not a target language, don't translate" by callers."""
    if len((text or "").split()) < WORD_MIN:
        return None
    try:
        return detect(text)
    except LangDetectException:
        return None


class SettingsWriter:
    """Minimal writer for run_translation_pass(): wraps DatabaseSettings in the
    duck-typed ``._connect()`` shape it expects."""

    def __init__(self, settings: DatabaseSettings):
        self._settings = settings

    def _connect(self):
        return connect(self._settings)


def ensure_translation_columns(conn) -> None:
    """Idempotent -- safe to call on every run, same pattern as the guarded ALTERs in
    daylight_sentiment.infra.db.connection.MIGRATIONS for model_number/review_country."""
    cur = conn.cursor()
    cur.execute(f"""
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('dbo.{REVIEWS_TABLE}') AND name = 'review_text_en')
    ALTER TABLE dbo.{REVIEWS_TABLE} ADD review_text_en NVARCHAR(MAX) NULL;
""")
    cur.execute(f"""
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('dbo.{REVIEWS_TABLE}') AND name = 'detected_language')
    ALTER TABLE dbo.{REVIEWS_TABLE} ADD detected_language NVARCHAR(10) NULL;
""")
    conn.commit()


def fetch_untranslated(conn) -> List[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(f"""
        SELECT uid, review_text
        FROM dbo.{REVIEWS_TABLE}
        WHERE review_text IS NOT NULL AND review_text_en IS NULL
    """)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def translate_one(analyzer: Any, text: str, language_name: str, max_chars: int = 6000) -> str:
    """analyzer needs only a ._call(prompt: str) -> dict method -- both GroqSentimentAnalyzer
    and ClaudeSentimentAnalyzer in daylight_sentiment.infra.analyzers already expose this, so
    whichever provider a run used for sentiment is reused here automatically, with no second
    API client constructed."""
    prompt = TRANSLATION_PROMPT.format(language_name=language_name, text=text[:max_chars])
    raw = analyzer._call(prompt)
    return str(raw.get("translation", "")).strip()


def run_translation_pass(analyzer: Any, writer: Any, max_chars: int = 6000,
                          limit: Optional[int] = None, dry_run: bool = False
                          ) -> Dict[str, int]:
    """Full detect -> translate -> write-back pass. Resumable by construction: only rows
    where review_text_en IS NULL are ever considered, so an interrupted or repeated run
    just picks up whatever's still untranslated -- no separate checkpoint file needed.

    Write-back is a narrow two-column UPDATE (review_text_en, detected_language ONLY),
    never the main pipeline's full MERGE across ~19 columns -- this can structurally never
    touch sentiment/rating/confidence/etc. for any row, regardless of what it does or
    doesn't fetch.
    """
    conn = writer._connect()
    try:
        ensure_translation_columns(conn)
        rows = fetch_untranslated(conn)
    finally:
        conn.close()

    logger.info("Found %d untranslated review(s) with text", len(rows))

    matched = []
    for r in rows:
        lang = detect_language(r["review_text"])
        if lang in TARGET_LANGUAGES:
            matched.append({**r, "detected_language": lang})

    by_lang = {lang: sum(1 for m in matched if m["detected_language"] == lang)
               for lang in TARGET_LANGUAGES}
    logger.info("%d match a target language: %s", len(matched), by_lang)

    if limit:
        matched = matched[:limit]
        logger.info("limit set: processing first %d only", len(matched))

    if dry_run:
        logger.info("dry_run set: stopping before any translation calls or DB writes")
        return {"matched": len(matched), "translated": 0, "failed": 0}

    if not matched:
        return {"matched": 0, "translated": 0, "failed": 0}

    conn = writer._connect()
    ok = failed = 0
    try:
        for i, m in enumerate(matched, start=1):
            try:
                translation = translate_one(
                    analyzer, m["review_text"], TARGET_LANGUAGES[m["detected_language"]], max_chars)
                cur = conn.cursor()
                cur.execute(
                    f"UPDATE dbo.{REVIEWS_TABLE} SET review_text_en = ?, detected_language = ? "
                    f"WHERE uid = ?",
                    translation, m["detected_language"], m["uid"])
                conn.commit()
                ok += 1
                logger.info("[%d/%d] %s (%s) -> translated (%d chars)",
                            i, len(matched), m["uid"], m["detected_language"], len(translation))
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.error("[%d/%d] %s failed: %s (will retry next run -- "
                             "review_text_en stays NULL)", i, len(matched), m["uid"], exc)
    finally:
        conn.close()

    logger.info("Translation pass done: %d translated, %d failed (of %d matched)",
                ok, failed, len(matched))
    return {"matched": len(matched), "translated": ok, "failed": failed}
