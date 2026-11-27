"""A resumed task worktree starts at the fetched WIP commit."""

import subprocess
from pathlib import Path

import pytest
from core.worktree import WorktreeError, WorktreeManager

SPEC = "042-resume"
BRANCH = f"aifactory/{SPEC}"
WIP_REF = f"refs/remotes/origin/{BRANCH}"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.check_output(  # noqa: S603 - fixed test git commands
        ["git", *args],  # noqa: S607
        cwd=cwd,
        text=True,
    ).strip()


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AIFACTORY_GIT_BASELINE_DIR", str(tmp_path / "baseline"))
    remote = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    project = tmp_path / "project"
    _git(tmp_path, "clone", str(remote), str(project))
    _git(project, "config", "user.email", "test@example.com")
    _git(project, "config", "user.name", "Test")
    _git(project, "commit", "--allow-empty", "-m", "base")
    _git(project, "push", "origin", "main")
    _git(project, "switch", "-c", BRANCH)
    (project / "feature.txt").write_text("work in progress", encoding="utf-8")
    _git(project, "add", "feature.txt")
    _git(project, "commit", "-m", "pause")
    _git(project, "push", "origin", BRANCH)
    _git(project, "switch", "main")
    _git(project, "branch", "-D", BRANCH)
    return project


@pytest.mark.parametrize("job", [False, True])
def test_question_resume_uses_wip_commit(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, *, job: bool
) -> None:
    manager = WorktreeManager(checkout)
    base = _git(checkout, "rev-parse", "HEAD")
    if job:
        monkeypatch.setenv("AIFACTORY_TASK_QUESTION_COUNT", "1")
        resumed = manager.get_or_create_worktree(SPEC)
    else:
        resumed = manager.create_worktree(SPEC, resume_start_point=WIP_REF)
    assert _git(resumed.path, "branch", "--show-current") == BRANCH
    assert _git(resumed.path, "rev-parse", "HEAD") == _git(
        checkout, "rev-parse", WIP_REF
    )
    assert (resumed.path / "feature.txt").read_text(
        encoding="utf-8"
    ) == "work in progress"
    assert _git(checkout, "branch", "--show-current") == "main"
    assert _git(checkout, "rev-parse", "HEAD") == base


def test_missing_question_resume_ref_fails_before_creating_worktree(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(checkout, "update-ref", "-d", WIP_REF)
    monkeypatch.setenv("AIFACTORY_TASK_QUESTION_COUNT", "1")
    manager = WorktreeManager(checkout)
    with pytest.raises(WorktreeError, match="Could not resolve question WIP ref"):
        manager.get_or_create_worktree(SPEC)
    assert not manager.get_worktree_path(SPEC).exists()
    assert not _git(checkout, "branch", "--list", BRANCH)


def test_without_resume_point_worktree_uses_base(checkout: Path) -> None:
    fresh = WorktreeManager(checkout).create_worktree(SPEC)
    assert _git(fresh.path, "rev-parse", "HEAD") == _git(checkout, "rev-parse", "main")
    assert not (fresh.path / "feature.txt").exists()
