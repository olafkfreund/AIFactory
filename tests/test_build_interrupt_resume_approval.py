"""Interactive Ctrl+C resume must re-check plan approval (#1946)."""

from __future__ import annotations

import json
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


def _make_spec(tmp_path: Path) -> Path:
    spec_dir = tmp_path / "spec"
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
