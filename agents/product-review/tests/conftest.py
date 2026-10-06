"""Make the repo root importable so tests can import the pipeline module directly.

These are characterization tests (ISSUE-13): they pin down *current* behavior
as a safety net for the refactor. Where current behavior looks odd, the test
documents it rather than "fixing" it -- see comments marked CHARACTERIZATION.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
