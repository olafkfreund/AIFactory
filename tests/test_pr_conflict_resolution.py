"""Slice 1 of #543: resolve_pr_conflicts — rebase + delegate-to-fixer + continue.

Pure unit tests with a scripted fake runner + fake fixer. No real git/LLM. The
helper is not yet wired into the live merge path; these lock its contract.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

from core.git_metadata import baseline_file, record_baseline  # noqa: E402
from server.services.pr_endgame import CmdResult, resolve_pr_conflicts  # noqa: E402


class FakeRunner:
    """Returns scripted CmdResults by matching a substring of the joined argv.

    ``script`` maps a marker (e.g. "rebase --continue") -> CmdResult; the first
    marker that is a substring of the command wins. Unmatched commands succeed.
    Records every command for assertions.
    """

    def __init__(self, script: dict[str, CmdResult]):
        self.script = script
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], cwd: str | None = None) -> CmdResult:
        self.calls.append(argv)
        joined = " ".join(argv)
        for marker, result in self.script.items():
            if marker in joined:
                return result
        return CmdResult(0, "", "")

    def ran(self, marker: str) -> bool:
        return any(marker in " ".join(c) for c in self.calls)


_OK = CmdResult(0, "", "")
_FAIL = CmdResult(1, "", "boom")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 - fixed git argv
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "test@example.com")
    _git(origin, "config", "user.name", "Test")
    (origin / "file").write_text("content\n")
    _git(origin, "add", "file")
    _git(origin, "commit", "-q", "-m", "initial")
    worktree = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(worktree))
    return worktree


def test_clean_rebase_removes_new_baseline(clone: Path, tmp_path: Path) -> None:
    baseline = baseline_file(clone)
    assert not baseline.exists()

    res = resolve_pr_conflicts(str(clone), "main", fixer=lambda _f, _w: False)

    assert res.resolved is True
    assert not baseline.exists()
    assert list((tmp_path / "git-baselines").iterdir()) == []


def test_clean_rebase_preserves_existing_baseline(clone: Path) -> None:
    record_baseline(clone)
    baseline = baseline_file(clone)
    original = baseline.read_bytes()

    res = resolve_pr_conflicts(str(clone), "main", fixer=lambda _f, _w: False)

    assert res.resolved is True
    assert baseline.read_bytes() == original


def test_clean_rebase_does_not_call_fixer():
    called = []
    runner = FakeRunner({"git rebase origin/main": _OK})
    res = resolve_pr_conflicts(
        "/wt", "main", fixer=lambda f, w: called.append(f) or True, runner=runner
    )
    assert res.resolved is True
    assert called == []  # fixer untouched on a clean rebase
    assert not runner.ran("rebase --abort")


def test_conflict_resolved_by_fixer_then_continue():
    runner = FakeRunner(
        {
            "git rebase origin/main": _FAIL,  # conflict
            "diff --no-ext-diff --no-textconv --name-only --diff-filter=U": CmdResult(
                0, "app.py\napi.py", ""
            ),
            "rebase --continue": _OK,
        }
    )
    seen = {}

    def fixer(files, wt):
        seen["files"] = files
        return True

    res = resolve_pr_conflicts("/wt", "main", fixer=fixer, runner=runner)
    assert res.resolved is True
    assert res.conflicted_files == ["app.py", "api.py"]
    assert seen["files"] == ["app.py", "api.py"]
    assert runner.ran("git add -A")
    assert not runner.ran("rebase --abort")


def test_fixer_fails_aborts_rebase():
    runner = FakeRunner(
        {
            "git rebase origin/main": _FAIL,
            "diff --no-ext-diff --no-textconv --name-only --diff-filter=U": CmdResult(
                0, "app.py", ""
            ),
        }
    )
    res = resolve_pr_conflicts("/wt", "main", fixer=lambda f, w: False, runner=runner)
    assert res.resolved is False
    assert res.conflicted_files == ["app.py"]
    assert runner.ran("rebase --abort")


def test_continue_failure_aborts_rebase():
    runner = FakeRunner(
        {
            "git rebase origin/main": _FAIL,
            "diff --no-ext-diff --no-textconv --name-only --diff-filter=U": CmdResult(
                0, "app.py", ""
            ),
            "rebase --continue": _FAIL,
        }
    )
    res = resolve_pr_conflicts("/wt", "main", fixer=lambda f, w: True, runner=runner)
    assert res.resolved is False
    assert runner.ran("rebase --abort")


def test_fetch_failure_is_unresolved():
    runner = FakeRunner({"git fetch origin main": _FAIL})
    res = resolve_pr_conflicts("/wt", "main", fixer=lambda f, w: True, runner=runner)
    assert res.resolved is False
    assert "fetch failed" in res.reason


def test_rebase_fails_with_no_conflicted_files_is_unresolved():
    runner = FakeRunner(
        {
            "git rebase origin/main": _FAIL,
            "diff --no-ext-diff --no-textconv --name-only --diff-filter=U": CmdResult(
                0, "", ""
            ),
        }
    )
    res = resolve_pr_conflicts("/wt", "main", fixer=lambda f, w: True, runner=runner)
    assert res.resolved is False
    assert runner.ran("rebase --abort")
