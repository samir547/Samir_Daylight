"""Checkpoint persistence (ISSUE-04 decomposition): loading resume state and
appending per-review results as they complete.

Only rows with a successful analysis are trusted on load -- a failed run's
entries are retried, never treated as done. This is the same guard
ReviewRepository applies on write, for the same reason: a failure must never
mask or overwrite good data.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional


def load_checkpoint(path: Path) -> Dict[str, Dict[str, Any]]:
    """Replay a checkpoint file into {uid: record}, last record per uid wins
    (rerun flags append fresh entries for reopened uids)."""
    done: Dict[str, Dict[str, Any]] = {}
    if not path.exists():
        return done
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn final line from an interrupted run
            # Only trust successful rows; retry anything that errored.
            if rec.get("uid") and rec.get("analysis") is not None:
                done[rec["uid"]] = rec
    return done


class CheckpointWriter:
    """Append-or-truncate JSONL writer, flushed per record so an interrupted
    run loses at most the in-flight review."""

    def __init__(self, path: Path, resume: bool):
        # Resuming appends; a fresh run truncates. load_checkpoint() keeps the
        # last record per uid, so appended reruns correctly supersede.
        self._fh = open(path, "a" if resume else "w", encoding="utf-8")

    def write(self, record: Dict[str, Any]) -> None:
        self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "CheckpointWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def open_checkpoint(path: Optional[Path], resume: bool) -> Optional[CheckpointWriter]:
    return CheckpointWriter(path, resume) if path else None
