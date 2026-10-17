"""Tests for git metadata baseline recording and verification (#1746, #1747)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from core.git_metadata import (
    _baseline_path,
    check_git_metadata,
    record_baseline,
    snapshot,
)

_BACKEND_DIR = Path(__file__).resolve().parent.parent / "apps" / "backend"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed test helper argv
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "Test")
    f = r / "f"
    f.write_text("init\n")
    _git(r, "add", "f")
    _git(r, "commit", "-q", "-m", "init")
    return r


def test_record_baseline_and_check_passes(repo: Path) -> None:
    record_baseline(repo)
    check_git_metadata(repo)
    snap = snapshot(repo)
    bpath = _baseline_path(str(snap["repo"]))
    assert bpath.exists()
    assert (bpath.stat().st_mode & 0o777) == 0o600


def test_config_merge_driver_append_raises_without_leaking_value(repo: Path) -> None:
    record_baseline(repo)
    with open(repo / ".git" / "config", "a", encoding="utf-8") as fh:
        fh.write('[merge "evil"]\n\tdriver = touch x\n')

    with pytest.raises(RuntimeError) as exc_info:
        check_git_metadata(repo)
    msg = str(exc_info.value)
    assert "merge.evil.driver" in msg
    assert "touch x" not in msg


def test_info_attributes_write_raises(repo: Path) -> None:
    record_baseline(repo)
    attrs = repo / ".git" / "info" / "attributes"
    attrs.parent.mkdir(parents=True, exist_ok=True)
    attrs.write_text("* merge=evil\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match=r"info/attributes"):
        check_git_metadata(repo)


def test_benign_git_operations_pass(repo: Path) -> None:
    record_baseline(repo)
    _git(repo, "config", "user.name", "other")
    _git(repo, "branch", "x")
    lock = repo / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    lock.unlink()

    check_git_metadata(repo)


def test_filter_lfs_clean_change_raises(repo: Path) -> None:
    _git(repo, "config", "filter.lfs.clean", "git-lfs clean -- %f")
    record_baseline(repo)
    _git(repo, "config", "filter.lfs.clean", "evil-clean %f")

    with pytest.raises(RuntimeError, match=r"filter\.lfs\.clean"):
        check_git_metadata(repo)


def test_missing_baseline_records_late_then_compares(
    repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING"):
        check_git_metadata(repo)
    assert "git metadata baseline taken late" in caplog.text

    with open(repo / ".git" / "config", "a", encoding="utf-8") as fh:
        fh.write('[merge "evil"]\n\tdriver = touch x\n')

    with pytest.raises(RuntimeError, match=r"merge\.evil\.driver"):
        check_git_metadata(repo)


def test_corrupt_baseline_file_raises(repo: Path) -> None:
    record_baseline(repo)
    snap = snapshot(repo)
    bpath = _baseline_path(str(snap["repo"]))
    bpath.write_text("{", encoding="utf-8")

    with pytest.raises(RuntimeError):
        check_git_metadata(repo)


def test_cli_rebaseline_allows_subsequent_check(repo: Path) -> None:
    record_baseline(repo)
    with open(repo / ".git" / "config", "a", encoding="utf-8") as fh:
        fh.write('[merge "evil"]\n\tdriver = touch x\n')

    with pytest.raises(RuntimeError):
        check_git_metadata(repo)

    env = dict(os.environ)
    res = subprocess.run(  # noqa: S603 - fixed CLI test invocation
        [sys.executable, "-m", "core.git_metadata", "--rebaseline", str(repo)],
        cwd=_BACKEND_DIR,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert str(repo) in res.stdout
    check_git_metadata(repo)


def test_linked_worktree_shares_baseline(repo: Path, tmp_path: Path) -> None:
    record_baseline(repo)
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "wt-branch", str(wt))

    check_git_metadata(wt)
    check_git_metadata(repo)

    _git(wt, "config", "merge.wt.driver", "true")
    with pytest.raises(RuntimeError, match=r"merge\.wt\.driver"):
        check_git_metadata(repo)
    with pytest.raises(RuntimeError, match=r"merge\.wt\.driver"):
        check_git_metadata(wt)
