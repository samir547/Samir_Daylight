"""Shared logging configuration (ISSUE-12).

Before this, logging setup lived inside the pipeline's main() and every
standalone script re-declared its own logging.basicConfig -- five
independent configs, only one of which wrote to multi_source_sentiment.log.
Everything now calls configure_logging(): same format everywhere, and the
shared log file gets every script's output by default.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

from daylight_sentiment.config import BASE

LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
DEFAULT_LOG_FILE = BASE / "multi_source_sentiment.log"


def configure_logging(level: int = logging.INFO,
                      log_file: Optional[Path] = DEFAULT_LOG_FILE,
                      stream: bool = True) -> None:
    """Configure root logging once, idempotently (basicConfig is a no-op if
    handlers are already installed). Pass log_file=None for console-only."""
    handlers: list = []
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    if stream:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=level, format=LOG_FORMAT, handlers=handlers)
