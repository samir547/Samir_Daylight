"""Standalone CLI for daylight_sentiment/infra/translation.py -- for an ad hoc backfill or to
test --limit/--dry-run without running the full sentiment pipeline.

For regular/automatic translation of new reviews, this now happens automatically as part of
every `python -m daylight_sentiment.cli` run (see run_translation() in cli.py, guarded by
--skip-translation) -- you generally don't need this script anymore unless you specifically
want to run translation on its own, separate from a sentiment run.

Usage:
    python -m scripts.translate_reviews --dry-run       # detect + count only, no LLM calls, no writes
    python -m scripts.translate_reviews --limit 20      # smoke test on the first 20 matches
    python -m scripts.translate_reviews                 # full run (identical logic to the pipeline's)
    python -m scripts.translate_reviews --provider groq # reuse the cheaper/default provider instead
"""
from __future__ import annotations

import argparse
import logging

from daylight_sentiment.config import Config, LLMProvider
from daylight_sentiment.infra.analyzers import build_analyzer
from daylight_sentiment.infra.translation import SettingsWriter, run_translation_pass

# ISSUE-12: shared logging setup -- same format as the pipeline, and this
# script's output now reaches multi_source_sentiment.log too.
from daylight_sentiment.logging_setup import configure_logging

configure_logging(level=logging.INFO)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--provider", choices=[e.value for e in LLMProvider], default="claude",
                    help="Which provider's analyzer to use for translation (default: claude)")
    p.add_argument("--dry-run", action="store_true",
                    help="Detect + count only -- no translation calls, no writes")
    p.add_argument("--limit", type=int, default=None,
                    help="Only process the first N matched reviews (smoke test)")
    args = p.parse_args()

    config = Config.from_env(llm_provider=LLMProvider(args.provider))
    analyzer = build_analyzer(config)
    writer = SettingsWriter(config.db)

    stats = run_translation_pass(
        analyzer, writer, max_chars=config.max_review_chars, limit=args.limit, dry_run=args.dry_run)
    print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
