#!/usr/bin/env python3
"""
Regression: worktrees gitignore build/test artifacts (parallel-merge fix)
=========================================================================

Found live in the 001 benchmark: parallel coders each ran pytest, committed the
binary ``.coverage`` (+ ``__pycache__``), and the sequential merge-back hit a
binary conflict — 2 of 3 wave subtasks aborted to serial. There was no
.gitignore in the project. Every worktree must ignore these artifacts so a wave
merges cleanly.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))

from core.worktree import _ensure_artifact_gitignore  # noqa: E402


def test_creates_gitignore_when_absent(tmp_path):
    _ensure_artifact_gitignore(tmp_path)
    gi = (tmp_path / ".gitignore").read_text()
    for pat in (".coverage", "__pycache__/", ".pytest_cache/", ".mypy_cache/"):
        assert pat in gi


def test_appends_when_existing_lacks_artifacts(tmp_path):
    (tmp_path / ".gitignore").write_text("# project\n.env\n")
    _ensure_artifact_gitignore(tmp_path)
    gi = (tmp_path / ".gitignore").read_text()
    assert ".env" in gi  # preserved
    assert ".coverage" in gi  # added


def test_idempotent_no_double_append(tmp_path):
    _ensure_artifact_gitignore(tmp_path)
    once = (tmp_path / ".gitignore").read_text()
    _ensure_artifact_gitignore(tmp_path)
    twice = (tmp_path / ".gitignore").read_text()
    assert once == twice


def test_skips_when_already_has_coverage(tmp_path):
    (tmp_path / ".gitignore").write_text(".coverage\nfoo\n")
    _ensure_artifact_gitignore(tmp_path)
    assert (tmp_path / ".gitignore").read_text() == ".coverage\nfoo\n"  # untouched


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
