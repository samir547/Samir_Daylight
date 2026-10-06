"""Characterization: _load_checkpoint -- only successful rows are trusted, the
last record per uid wins, torn/blank lines are tolerated."""
import json

from daylight_sentiment.application.checkpoint import load_checkpoint as _load_checkpoint


def _write(path, records, torn_tail=None):
    lines = [json.dumps(r) for r in records]
    if torn_tail is not None:
        lines.append(torn_tail)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_missing_file_gives_empty(tmp_path):
    assert _load_checkpoint(tmp_path / "nope.jsonl") == {}


def test_successful_rows_loaded(tmp_path):
    p = tmp_path / "c.jsonl"
    _write(p, [{"uid": "a", "analysis": {"sentiment": "positive"}}])
    done = _load_checkpoint(p)
    assert set(done) == {"a"}


def test_errored_rows_ignored_so_they_retry(tmp_path):
    # The property that protects the DB from failed reruns: analysis=None rows
    # are never treated as done.
    p = tmp_path / "c.jsonl"
    _write(p, [
        {"uid": "ok", "analysis": {"sentiment": "positive"}},
        {"uid": "failed", "analysis": None, "error": "boom"},
        {"uid": "", "analysis": {"sentiment": "positive"}},  # blank uid also ignored
    ])
    assert set(_load_checkpoint(p)) == {"ok"}


def test_last_record_per_uid_wins(tmp_path):
    # --rerun-* appends fresh entries to the same file; replay keeps the last.
    p = tmp_path / "c.jsonl"
    _write(p, [
        {"uid": "a", "analysis": {"sentiment": "negative"}},
        {"uid": "a", "analysis": {"sentiment": "positive"}},
    ])
    assert _load_checkpoint(p)["a"]["analysis"]["sentiment"] == "positive"


def test_torn_final_line_and_blanks_skipped(tmp_path):
    p = tmp_path / "c.jsonl"
    _write(p, [{"uid": "a", "analysis": {"sentiment": "neutral"}}],
           torn_tail='{"uid": "b", "anal')
    p.write_text(p.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
    assert set(_load_checkpoint(p)) == {"a"}
