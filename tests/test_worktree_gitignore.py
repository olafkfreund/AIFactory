#!/usr/bin/env python3
"""
Regression: worktrees gitignore build/test artifacts (parallel-merge fix)
=========================================================================

Found live in the 001 benchmark: parallel coders each ran pytest, committed the
binary ``.coverage`` (+ ``__pycache__``), and the sequential merge-back hit a
binary conflict — 2 of 3 wave subtasks aborted to serial. There was no
.gitignore in the project. Every worktree must ignore these artifacts so a wave
merges cleanly.

#1172: "already done" used to be keyed on the literal string ``.coverage``
appearing anywhere in the file, so any project that ignores ``.coverage`` on
its own — most Python repos, including ``aifactory-demo`` — received nothing
and reported success. "Done" now means git actually ignores every pattern, so
these tests assert the *effect* (``git check-ignore``) and not the file text.
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))

from core.worktree import (  # noqa: E402
    _ARTIFACT_RULES,
    _ensure_artifact_gitignore,
)

PROBES = [probe for _, probe in _ARTIFACT_RULES]


@pytest.fixture
def repo(tmp_path):
    """A real git repo — ``git check-ignore`` is the thing under test."""
    subprocess.run(["git", "init", "-q", "."], cwd=tmp_path, check=True)
    return tmp_path


def unignored(path):
    """Probe paths git does NOT ignore at ``path``. Empty list == fully covered."""
    proc = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        cwd=path,
        input="\n".join(PROBES),
        capture_output=True,
        text=True,
    )
    assert proc.returncode in (0, 1), proc.stderr
    ignored = set(proc.stdout.splitlines())
    return [p for p in PROBES if p not in ignored]


def test_creates_gitignore_when_absent(repo):
    _ensure_artifact_gitignore(repo)
    assert unignored(repo) == []


def test_appends_when_existing_lacks_artifacts(repo):
    (repo / ".gitignore").write_text("# project\n.env\n")
    _ensure_artifact_gitignore(repo)
    assert ".env" in (repo / ".gitignore").read_text()  # preserved
    assert unignored(repo) == []


def test_1172_acts_when_gitignore_already_mentions_coverage(repo):
    """The #1172 defect: a lone ``.coverage`` line used to short-circuit it."""
    (repo / ".gitignore").write_text(".coverage\n")
    before = (repo / ".gitignore").read_text()
    _ensure_artifact_gitignore(repo)
    assert (repo / ".gitignore").read_text() != before, "silently no-op'd (#1172)"
    assert unignored(repo) == []


def test_covers_aifactory_runtime_dir(repo):
    """#1106 moved the status file to ``.aifactory/status.json``."""
    _ensure_artifact_gitignore(repo)
    assert ".aifactory/status.json" not in unignored(repo)


def test_idempotent_no_double_append(repo):
    (repo / ".gitignore").write_text(".coverage\n")
    _ensure_artifact_gitignore(repo)
    once = (repo / ".gitignore").read_text()
    _ensure_artifact_gitignore(repo)
    assert (repo / ".gitignore").read_text() == once


def test_preserves_unrelated_content_verbatim(repo):
    """A managed project's file is not ours to rewrite — only append to."""
    original = "# my project\n*.log\nsecrets.env\n!keep.log\n"
    (repo / ".gitignore").write_text(original)
    _ensure_artifact_gitignore(repo)
    after = (repo / ".gitignore").read_text()
    assert after.startswith(original)
    assert "auto-added by AIFactory" in after[len(original) :]
    assert unignored(repo) == []


def test_writes_nothing_when_already_covered_by_info_exclude(repo):
    """Ignored by other means is still ignored — do not touch the repo's file."""
    (repo / ".git" / "info" / "exclude").write_text(
        "".join(f"{pattern}\n" for pattern, _ in _ARTIFACT_RULES)
    )
    _ensure_artifact_gitignore(repo)
    assert not (repo / ".gitignore").exists()


def test_appends_only_the_missing_rules(repo):
    (repo / ".gitignore").write_text(".coverage\n.coverage.*\nnode_modules/\n")
    _ensure_artifact_gitignore(repo)
    appended = (repo / ".gitignore").read_text().split("auto-added by AIFactory")[1]
    assert "node_modules/" not in appended
    assert "__pycache__/" in appended
    assert unignored(repo) == []


def test_alternate_spelling_counts_as_covered(repo):
    """A project's ``*.pyc`` covers our ``*.py[cod]`` — do not restate it."""
    (repo / ".gitignore").write_text("*.pyc\n")
    _ensure_artifact_gitignore(repo)
    appended = (repo / ".gitignore").read_text().split("auto-added by AIFactory")[1]
    assert "*.py[cod]" not in appended
    assert unignored(repo) == []


def test_linked_worktree_keeps_artifacts_out_of_git_add(tmp_path):
    """The real shape, asserting the real effect.

    The call site is a LINKED worktree (``.git`` is a file), cut from a base
    branch whose TRACKED .gitignore is the #1172 shape — one ``.coverage``
    line. What actually has to be true is that a coder who runs pytest and then
    ``git add -A`` stages their work and nothing else; that is the merge-back
    collision the control exists to prevent.
    """
    base = tmp_path / "base"
    base.mkdir()

    def git(*args, cwd=base):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    git("init", "-q", "-b", "main", ".")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (base / ".gitignore").write_text(".coverage\n")
    (base / "app.py").write_text("x = 1\n")
    git("add", "-A")
    git("commit", "-qm", "init")

    link = tmp_path / "task-worktree"
    git("worktree", "add", "-q", "-b", "aifactory/task", str(link), "main")
    assert (link / ".git").is_file(), "not a linked worktree"
    assert (link / ".gitignore").read_text() == ".coverage\n"

    _ensure_artifact_gitignore(link)
    assert unignored(link) == []

    (link / ".coverage").write_bytes(b"\x00binary")
    (link / "__pycache__").mkdir()
    (link / "__pycache__" / "app.cpython-313.pyc").write_bytes(b"\x00")
    (link / ".aifactory").mkdir()
    (link / ".aifactory" / "status.json").write_text("{}")
    (link / "feature.py").write_text("y = 2\n")
    git("add", "-A", cwd=link)

    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=link,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert staged == [".gitignore", "feature.py"], staged


def test_best_effort_outside_a_repo(tmp_path):
    """No repo means no verdict from git: write the full block, never raise."""
    _ensure_artifact_gitignore(tmp_path)
    written = (tmp_path / ".gitignore").read_text()
    for pattern, _ in _ARTIFACT_RULES:
        assert pattern in written


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
