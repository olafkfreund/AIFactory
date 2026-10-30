"""Interactive Ctrl+C resume must re-check plan approval (#1946)."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from cli import build_commands  # noqa: E402
from review.state import ReviewState  # noqa: E402

PLAN = {
    "feature": "a",
    "phases": [
        {
            "phase": 1,
            "name": "p",
            "subtasks": [{"id": "1", "description": "d", "status": "pending"}],
        }
    ],
}


def _make_spec(tmp_path: Path, name: str = "spec") -> Path:
    spec_dir = tmp_path / name
    spec_dir.mkdir()
    (spec_dir / "spec.md").write_text("# Feature\nDo a small thing.\n")
    (spec_dir / "task_metadata.json").write_text(
        json.dumps({"requireReviewBeforeCoding": True})
    )
    (spec_dir / "implementation_plan.json").write_text(json.dumps(PLAN, indent=2))
    return spec_dir


def _setup_interrupt_mocks(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    import agent

    agent_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(build_commands, "select_menu", lambda **_k: "skip")
    monkeypatch.setattr(build_commands, "BuildState", MagicMock())
    monkeypatch.setattr(build_commands, "StatusManager", MagicMock())
    monkeypatch.setattr(build_commands, "print_paused_banner", MagicMock())
    monkeypatch.setattr(agent, "run_autonomous_agent", agent_mock)
    return agent_mock


def test_widened_plan_blocks_interrupt_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_dir = _make_spec(tmp_path)
    ReviewState().approve(spec_dir)
    (spec_dir / "implementation_plan.json").write_text(
        json.dumps({**PLAN, "required_commands": ["npx"]}, indent=2)
    )
    agent_mock = _setup_interrupt_mocks(monkeypatch)

    build_commands._handle_build_interrupt(
        spec_dir=spec_dir,
        project_dir=tmp_path,
        worktree_manager=None,
        working_dir=tmp_path,
        model="claude-sonnet-4-5",
        max_iterations=1,
        verbose=False,
    )

    agent_mock.assert_not_awaited()


def test_unchanged_approved_plan_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_dir = _make_spec(tmp_path)
    ReviewState().approve(spec_dir)
    agent_mock = _setup_interrupt_mocks(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        build_commands._handle_build_interrupt(
            spec_dir=spec_dir,
            project_dir=tmp_path,
            worktree_manager=None,
            working_dir=tmp_path,
            model="claude-sonnet-4-5",
            max_iterations=1,
            verbose=False,
        )

    assert exc.value.code == 0
    agent_mock.assert_awaited_once()


WIDENED = {**PLAN, "required_commands": ["npx"]}


def _interrupt(tmp_path: Path, spec_dir: Path, source_spec_dir: Path | None) -> None:
    build_commands._handle_build_interrupt(
        spec_dir=spec_dir,
        project_dir=tmp_path,
        worktree_manager=None,
        working_dir=tmp_path,
        model="claude-sonnet-4-5",
        max_iterations=1,
        verbose=False,
        source_spec_dir=source_spec_dir,
    )


def _source_and_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """An approved source spec and its worktree copy, as setup_workspace makes."""
    source = _make_spec(tmp_path, "source")
    ReviewState().approve(source)
    worktree = tmp_path / "worktree"
    shutil.copytree(source, worktree)
    return source, worktree


def test_forged_worktree_approval_blocks_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, worktree = _source_and_worktree(tmp_path)
    (worktree / "implementation_plan.json").write_text(json.dumps(WIDENED, indent=2))
    ReviewState().approve(worktree)  # the agent re-approves its own widened plan
    agent_mock = _setup_interrupt_mocks(monkeypatch)

    _interrupt(tmp_path, worktree, source)

    agent_mock.assert_not_awaited()


def test_flipped_worktree_metadata_blocks_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, worktree = _source_and_worktree(tmp_path)
    (worktree / "implementation_plan.json").write_text(json.dumps(WIDENED, indent=2))
    (worktree / "task_metadata.json").write_text(
        json.dumps({"requireReviewBeforeCoding": False})
    )
    agent_mock = _setup_interrupt_mocks(monkeypatch)

    _interrupt(tmp_path, worktree, source)

    agent_mock.assert_not_awaited()


def test_approved_source_resumes_unchanged_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, worktree = _source_and_worktree(tmp_path)
    agent_mock = _setup_interrupt_mocks(monkeypatch)

    with pytest.raises(SystemExit):
        _interrupt(tmp_path, worktree, source)

    agent_mock.assert_awaited_once()
