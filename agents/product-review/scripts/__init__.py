"""Operator tools that *use* the daylight_sentiment package -- not library code.

This file exists only so the scripts can be run as modules from the repo root:

    python -m scripts.translate_reviews --dry-run

Running them that way puts the repo root on sys.path (so ``daylight_sentiment``
resolves) with no per-file path hacks, matching how the pipeline itself is
invoked (``python -m daylight_sentiment.cli``). Nothing should import from
this package.
"""
