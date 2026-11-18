"""Real-git coverage for guarded orphan registration cleanup (#2134)."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
for _p in (_REPO / "apps" / "web-server", _REPO / "apps" / "backend"):
    if str(_p) not in sys.path:
        sys.path.append(str(_p))

from server.services import build_backend, stale_tasks, worktree_cleanup  # noqa: E402

_SPEC = "042-go"
_PROJECT_ID = "registered-project-id"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - test-controlled git arguments
        ["git", "-C", str(cwd), *args],  # noqa: S607 - use the installed git
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    project = tmp_path / "different-directory-name"
    project.mkdir()
    _git(project, "init", "-b", "main")
    _git(project, "config", "user.email", "t@example.com")
    _git(project, "config", "user.name", "Test")
    _git(project, "commit", "--allow-empty", "-m", "base")
    return project


def _orphan(project: Path, path: Path, status: str = "done") -> Path:
    spec_dir = project / ".aifactory" / "specs" / _SPEC
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "task_control.json").write_text(json.dumps({"status": status}))
    _git(project, "worktree", "add", "--detach", str(path), "HEAD")
    admin = Path((path / ".git").read_text().strip().removeprefix("gitdir: "))
    shutil.rmtree(path)
    return admin


@pytest.mark.parametrize("kind", ["tasks", "builds"])
@pytest.mark.parametrize("status", sorted(stale_tasks.TERMINAL_STATES))
def test_terminal_registration_removed(project: Path, kind: str, status: str) -> None:
    path = project / ".aifactory" / "worktrees" / kind / _SPEC
    admin = _orphan(project, path, status)
    assert (
        worktree_cleanup.cleanup_orphaned_worktrees(project, _PROJECT_ID, set()) == []
    )
    assert not admin.exists()
    assert build_backend.orphaned_worktree_registrations(project) == []


def test_deleted_spec_and_second_run(
    project: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = project / ".aifactory" / "worktrees" / "tasks" / _SPEC
    admin = _orphan(project, path)
    shutil.rmtree(project / ".aifactory" / "specs" / _SPEC)
    with caplog.at_level(logging.INFO, logger=worktree_cleanup.__name__):
        assert (
            worktree_cleanup.cleanup_orphaned_worktrees(project, _PROJECT_ID, set())
            == []
        )
        assert (
            worktree_cleanup.cleanup_orphaned_worktrees(project, _PROJECT_ID, set())
            == []
        )
    assert not admin.exists()
    assert [r.getMessage() for r in caplog.records] == [
        "removed=1 deferred=0",
    ]  # second run: nothing to do, silent


@pytest.mark.parametrize("status", ["running", "unknown", "review", "human_review", ""])
def test_nonterminal_untouched(project: Path, status: str) -> None:
    path = project / ".aifactory" / "worktrees" / "tasks" / _SPEC
    admin = _orphan(project, path, status)
    assert worktree_cleanup.cleanup_orphaned_worktrees(project, _PROJECT_ID, set()) == [
        str(path)
    ]
    assert admin.exists()


@pytest.mark.parametrize("content", ["{broken", "[]", "{}", '{"status": []}'])
def test_unreadable_or_unknown_control_does_not_use_legacy(
    project: Path, content: str
) -> None:
    path = project / ".aifactory" / "worktrees" / "tasks" / _SPEC
    admin = _orphan(project, path)
    spec_dir = project / ".aifactory" / "specs" / _SPEC
    (spec_dir / "task_control.json").write_text(content)
    (spec_dir / "implementation_plan.json").write_text('{"status": "done"}')
    assert worktree_cleanup.cleanup_orphaned_worktrees(project, _PROJECT_ID, set()) == [
        str(path)
    ]
    assert admin.exists()


def test_legacy_terminal_status(project: Path) -> None:
    path = project / ".aifactory" / "worktrees" / "tasks" / _SPEC
    admin = _orphan(project, path)
    spec_dir = project / ".aifactory" / "specs" / _SPEC
    (spec_dir / "task_control.json").rename(spec_dir / "implementation_plan.json")
    assert (
        worktree_cleanup.cleanup_orphaned_worktrees(project, _PROJECT_ID, set()) == []
    )
    assert not admin.exists()


@pytest.mark.parametrize(
    "relative",
    [
        "outside/042-go",
        ".aifactory/worktrees/other/042-go",
        ".aifactory/worktrees/tasks/nested/042-go",
        ".aifactory/worktrees/tasks/unsafe spec",
    ],
)
def test_unsafe_path_untouched(
    project: Path, relative: str, caplog: pytest.LogCaptureFixture
) -> None:
    path = project / relative
    admin = _orphan(project, path)
    assert worktree_cleanup.cleanup_orphaned_worktrees(project, _PROJECT_ID, set()) == [
        str(path)
    ]
    assert admin.exists()
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_symlink_escape_untouched(project: Path) -> None:
    path = project / ".aifactory" / "worktrees" / "tasks" / _SPEC
    admin = _orphan(project, path)
    path.symlink_to(project.parent, target_is_directory=True)
    assert worktree_cleanup.cleanup_orphaned_worktrees(project, _PROJECT_ID, set()) == [
        str(path)
    ]
    assert admin.exists()


def test_locked_untouched(project: Path) -> None:
    path = project / ".aifactory" / "worktrees" / "tasks" / _SPEC
    admin = _orphan(project, path)
    (admin / "locked").write_text("keep")
    assert worktree_cleanup.cleanup_orphaned_worktrees(project, _PROJECT_ID, set()) == [
        str(path)
    ]
    assert admin.exists()


@pytest.mark.parametrize(
    "active_id,protected",
    [
        (f"{_PROJECT_ID}:{_SPEC}", True),
        (_SPEC, True),
        (f"other-project:{_SPEC}", False),
    ],
)
def test_active_ids(project: Path, active_id: str, protected: bool) -> None:
    path = project / ".aifactory" / "worktrees" / "tasks" / _SPEC
    admin = _orphan(project, path)
    remaining = worktree_cleanup.cleanup_orphaned_worktrees(
        project, _PROJECT_ID, {active_id}
    )
    assert remaining == ([str(path)] if protected else [])
    assert admin.exists() == protected


def test_standalone_clone_survives(project: Path) -> None:
    path = project / ".aifactory" / "worktrees" / "tasks" / _SPEC
    admin = _orphan(project, path)
    _git(project, "clone", "--local", str(project), str(path))
    (path / "only-copy.txt").write_text("keep this work")
    assert (
        worktree_cleanup.cleanup_orphaned_worktrees(project, _PROJECT_ID, set()) == []
    )
    assert not admin.exists()
    assert (path / ".git").is_dir()
    assert (path / "only-copy.txt").read_text() == "keep this work"
    assert _git(path, "rev-parse", "HEAD") == _git(project, "rev-parse", "HEAD")


def test_project_is_itself_a_linked_worktree(project: Path) -> None:
    linked = project.parent / "linked-project"
    _git(project, "worktree", "add", "--detach", str(linked), "HEAD")
    path = linked / ".aifactory" / "worktrees" / "tasks" / _SPEC
    admin = _orphan(linked, path)
    assert worktree_cleanup.cleanup_orphaned_worktrees(linked, _PROJECT_ID, set()) == []
    assert not admin.exists()
    assert (linked / ".git").is_file()
    assert _git(linked, "rev-parse", "HEAD") == _git(project, "rev-parse", "HEAD")


def test_failed_removal_is_returned_and_logged(
    project: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = project / ".aifactory" / "worktrees" / "tasks" / _SPEC
    admin = _orphan(project, path)

    def fail_removal(target: Path) -> None:
        assert target == admin
        raise OSError("cannot remove registration")

    monkeypatch.setattr(worktree_cleanup.shutil, "rmtree", fail_removal)
    with caplog.at_level(logging.INFO, logger=worktree_cleanup.__name__):
        assert worktree_cleanup.cleanup_orphaned_worktrees(
            project, _PROJECT_ID, set()
        ) == [str(path)]
    assert admin.exists()
    assert [record.levelno for record in caplog.records] == [
        logging.ERROR,
        logging.INFO,
    ]
    assert caplog.records[-1].getMessage() == "removed=0 deferred=1"


def test_unreadable_active_state_skips_cleanup() -> None:
    import asyncio  # noqa: PLC0415

    from server.services import agent_kubejob  # noqa: PLC0415

    # None (active set unreadable) must return before resolving any project.
    assert asyncio.run(agent_kubejob._report_orphaned_worktrees("p:s", None)) == []
