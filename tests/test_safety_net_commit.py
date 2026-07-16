"""Safety-net commit of uncommitted agent changes (#611 g).

The agent commits its own work, but if it leaves files uncommitted a later
bookkeeping abort / worktree teardown can lose them. `commit_uncommitted_changes`
captures them defensively.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from agents.utils import commit_uncommitted_changes  # noqa: E402


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(["init"], tmp_path)
    _git(["config", "user.email", "t@example.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)
    (tmp_path / "seed.txt").write_text("seed")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "seed"], tmp_path)
    return tmp_path


def _porcelain(cwd: Path) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True
    ).stdout.strip()


def test_returns_none_on_clean_tree(repo: Path) -> None:
    assert commit_uncommitted_changes(repo, "ac1") is None


def test_commits_uncommitted_files(repo: Path) -> None:
    (repo / "app").mkdir()
    (repo / "app" / "main.py").write_text("app = object()\n")
    sha = commit_uncommitted_changes(repo, "ac1")
    assert sha is not None
    # The tree is now clean and the file is tracked at HEAD.
    assert _porcelain(repo) == ""
    tracked = subprocess.run(
        ["git", "ls-files", "app/main.py"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    assert tracked == "app/main.py"
    # Subtask id appears in the commit message.
    msg = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert "ac1" in msg and "safety-net" in msg


def test_defensive_on_non_git_dir(tmp_path: Path) -> None:
    # Not a git repo → returns None, never raises.
    assert commit_uncommitted_changes(tmp_path / "nope", "ac1") is None


# --- harness bookkeeping (.aifactory-status etc.) must NOT trigger a commit ---


def test_bookkeeping_only_churn_makes_no_commit(repo: Path) -> None:
    """The ccstatusline / security files churn every subtask; a safety-net commit
    of them clutters the branch (and once tracked, self-perpetuates). Only
    bookkeeping dirty -> no commit."""
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    (repo / ".aifactory-status").write_text("status churn")
    (repo / ".aifactory-security.json").write_text("{}")

    assert commit_uncommitted_changes(repo, "1.1") is None
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    assert before == after, "no commit should have been made for bookkeeping-only churn"


def test_real_code_is_committed_without_bookkeeping(repo: Path) -> None:
    """Uncommitted code is still rescued, but the bookkeeping files are left out
    of the commit so they never become tracked."""
    (repo / "feature.py").write_text("def f():\n    return 1\n")
    (repo / ".aifactory-status").write_text("status churn")

    commit = commit_uncommitted_changes(repo, "2.1")
    assert commit is not None
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=repo, capture_output=True, text=True
    ).stdout.split()
    assert "feature.py" in tracked
    assert ".aifactory-status" not in tracked
