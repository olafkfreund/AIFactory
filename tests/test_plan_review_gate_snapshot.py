"""Plan-review gate must not trust files the planner can write (#1922).

The post-planner gate in ``run_autonomous_agent`` decides from a snapshot taken
before the first agent session. A planner that rewrites ``task_metadata.json``
or forges ``review_state.json`` must not be able to skip the pause.
"""

from __future__ import annotations

import contextlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

from review.state import ReviewState  # noqa: E402

PLAN_A = {
    "feature": "a",
    "phases": [
        {
            "phase": 1,
            "name": "p",
            "subtasks": [{"id": "1", "description": "d", "status": "pending"}],
        }
    ],
}
PLAN_B = {**PLAN_A, "feature": "b"}


def _write_plan(spec_dir: Path, plan: dict) -> None:
    (spec_dir / "implementation_plan.json").write_text(json.dumps(plan, indent=2))


def _write_meta(spec_dir: Path, meta: dict) -> None:
    (spec_dir / "task_metadata.json").write_text(json.dumps(meta))


def _make_spec(tmp_path: Path, meta: dict | None = None) -> Path:
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "spec.md").write_text("# Feature\nDo a small thing.\n")
    _write_meta(
        spec_dir,
        {"requireReviewBeforeCoding": True} if meta is None else meta,
    )
    return spec_dir


async def _drive(
    spec_dir: Path, tmp_path: Path, planner: Callable[[Path], None]
) -> AsyncMock:
    """Run run_autonomous_agent; ``planner`` acts as the first agent session."""
    import agents.coder as coder

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    async def session(*_a, **_k):
        planner(spec_dir)
        return ("continue", "", {})

    session_mock = AsyncMock(side_effect=session)
    patchers = {
        "create_client": MagicMock(return_value=fake_client),
        "get_solo_prompt": MagicMock(return_value="SOLO_PROMPT"),
        "generate_planner_prompt": MagicMock(return_value="PLANNER_PROMPT"),
        "generate_subtask_prompt": MagicMock(return_value="SUBTASK_PROMPT"),
        "run_session_guarded": session_mock,
        "check_git_metadata": MagicMock(),
        "get_graphiti_context": AsyncMock(return_value=""),
        "RecoveryManager": MagicMock(),
        "StatusManager": MagicMock(),
        "get_task_logger": MagicMock(return_value=None),
        "debug_memory_system_status": MagicMock(),
        "record_turn": MagicMock(),
        "CompactionDetector": MagicMock(),
        "build_operational_context": MagicMock(return_value=""),
        "drain_inbox": MagicMock(return_value=[]),
        "is_build_complete": MagicMock(return_value=False),
        "print_build_complete_banner": MagicMock(),
        "print_progress_summary": MagicMock(),
        "print_session_header": MagicMock(),
        "count_subtasks": MagicMock(return_value=(0, 1)),
        "count_subtasks_detailed": MagicMock(
            return_value={"completed": 0, "total": 1, "in_progress": 0}
        ),
        "get_current_phase": MagicMock(return_value=None),
        "get_next_subtask": MagicMock(return_value=None),
        "get_latest_commit": MagicMock(return_value="abc"),
        "get_commit_count": MagicMock(return_value=0),
        "infer_provider_from_model": MagicMock(return_value="claude"),
        "get_phase_model": MagicMock(return_value="claude-sonnet-4-5"),
        "get_phase_thinking_budget": MagicMock(return_value=None),
        "AUTO_CONTINUE_DELAY_SECONDS": 0,
    }
    with contextlib.ExitStack() as stack:
        for name, repl in patchers.items():
            stack.enter_context(patch.object(coder, name, repl))
        await coder.run_autonomous_agent(
            project_dir=tmp_path,
            spec_dir=spec_dir,
            model="claude-sonnet-4-5",
            max_iterations=2,
        )
    return session_mock


def _paused(spec_dir: Path, session_mock: AsyncMock) -> bool:
    plan = json.loads((spec_dir / "implementation_plan.json").read_text())
    return (
        plan.get("status") == "human_review"
        and plan.get("reviewReason") == "plan_review"
        and session_mock.await_count == 1
    )


def _proceeded(spec_dir: Path, session_mock: AsyncMock) -> bool:
    """The gate let the build through: the planner's plan was never stamped.

    get_next_subtask is mocked to None, so no coder session follows the gate.
    """
    plan = json.loads((spec_dir / "implementation_plan.json").read_text())
    return (
        "status" not in plan
        and "reviewReason" not in plan
        and session_mock.await_count == 1
    )


def _approve_then_remove_plan(spec_dir: Path, plan: dict) -> None:
    """Pre-planner approval bound to ``plan``; the plan is gone so a planner runs."""
    _write_plan(spec_dir, plan)
    ReviewState().approve(spec_dir)
    (spec_dir / "implementation_plan.json").unlink()


def _planner(plan: dict, forge: Callable[[Path], None] | None = None):
    def run(spec_dir: Path) -> None:
        _write_plan(spec_dir, plan)
        if forge:
            forge(spec_dir)

    return run


@pytest.fixture(autouse=True)
def _solo_off(monkeypatch):
    monkeypatch.setenv("AIFACTORY_SOLO_MODE", "off")


@pytest.mark.asyncio
class TestPostPlannerGate:
    async def test_planner_clears_require_review_still_paused(self, tmp_path):
        spec_dir = _make_spec(tmp_path)
        m = await _drive(
            spec_dir, tmp_path, _planner(PLAN_A, lambda d: _write_meta(d, {}))
        )
        assert _paused(spec_dir, m)

    async def test_planner_writes_hashless_approval_still_paused(self, tmp_path):
        spec_dir = _make_spec(tmp_path)

        def forge(d: Path) -> None:
            (d / "review_state.json").write_text(json.dumps({"approved": True}))

        m = await _drive(spec_dir, tmp_path, _planner(PLAN_A, forge))
        assert _paused(spec_dir, m)

    async def test_planner_writes_correctly_hashed_approval_still_paused(
        self, tmp_path
    ):
        spec_dir = _make_spec(tmp_path)

        def forge(d: Path) -> None:
            ReviewState().approve(d)

        m = await _drive(spec_dir, tmp_path, _planner(PLAN_A, forge))
        assert _paused(spec_dir, m)

    async def test_approval_for_different_plan_still_paused(self, tmp_path):
        spec_dir = _make_spec(tmp_path)
        _approve_then_remove_plan(spec_dir, PLAN_A)
        m = await _drive(spec_dir, tmp_path, _planner(PLAN_B))
        assert _paused(spec_dir, m)

    async def test_approval_for_exact_plan_still_paused(self, tmp_path):
        # No on-disk approval is trusted after the planner runs: a human cannot
        # have approved a plan written seconds ago.
        spec_dir = _make_spec(tmp_path)
        _approve_then_remove_plan(spec_dir, PLAN_A)
        m = await _drive(spec_dir, tmp_path, _planner(PLAN_A))
        assert _paused(spec_dir, m)

    async def test_planner_enabling_review_does_not_pause(self, tmp_path):
        spec_dir = _make_spec(tmp_path, {"requireReviewBeforeCoding": False})
        m = await _drive(
            spec_dir,
            tmp_path,
            _planner(
                PLAN_A,
                lambda d: _write_meta(d, {"requireReviewBeforeCoding": True}),
            ),
        )
        assert _proceeded(spec_dir, m)

    async def test_solo_on_not_paused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AIFACTORY_SOLO_MODE", "true")
        spec_dir = _make_spec(tmp_path)
        m = await _drive(spec_dir, tmp_path, _planner(PLAN_A))
        assert _proceeded(spec_dir, m)

    async def test_skip_planning_fast_path_not_paused(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AIFACTORY_SOLO_MODE")  # the env override wins over metadata
        spec_dir = _make_spec(
            tmp_path, {"requireReviewBeforeCoding": True, "skipPlanning": True}
        )
        m = await _drive(spec_dir, tmp_path, _planner(PLAN_A))
        assert _proceeded(spec_dir, m)


# ---------------------------------------------------------------------------
# Restart without approval: the plan exists, so no planner runs and the coder
# gate is not reached. run.py's pre-flight is the guard.
# ---------------------------------------------------------------------------
def _run_build(spec_dir: Path, tmp_path: Path, agent_mock: AsyncMock) -> None:
    from cli import build_commands

    patches = [
        patch("agent.run_autonomous_agent", new=agent_mock),
        patch("agent.sync_plan_to_source", MagicMock(return_value=False)),
        patch("qa_loop.should_run_qa", MagicMock(return_value=False)),
        patch("qa_loop.is_qa_approved", MagicMock(return_value=False)),
        patch("qa_loop.run_qa_validation_loop", new=AsyncMock(return_value=True)),
        patch("cli.utils.validate_environment", MagicMock(return_value=True)),
        patch("cli.utils.print_banner", MagicMock()),
        # the mocked agent builds nothing; skip the no-op-build exit(1)
        patch.object(build_commands, "count_subtasks", MagicMock(return_value=(1, 1))),
        patch.object(
            build_commands,
            "choose_workspace",
            MagicMock(return_value=build_commands.WorkspaceMode.DIRECT),
        ),
        patch.object(
            build_commands, "get_existing_build_worktree", MagicMock(return_value=None)
        ),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        build_commands.handle_build_command(
            project_dir=tmp_path,
            spec_dir=spec_dir,
            model="claude-sonnet-4-5",
            max_iterations=1,
            verbose=False,
            force_isolated=False,
            force_direct=True,
            auto_continue=True,
            skip_qa=True,
            force_bypass_approval=False,
        )


class TestRestartWithoutApproval:
    @pytest.mark.parametrize("case", ["absent", "hashless", "different_plan"])
    def test_unapproved_restart_blocked(self, tmp_path, monkeypatch, case):
        monkeypatch.setenv("AIFACTORY_SOLO_MODE", "off")
        spec_dir = _make_spec(tmp_path)
        _write_plan(spec_dir, PLAN_A)
        if case == "hashless":
            (spec_dir / "review_state.json").write_text(json.dumps({"approved": True}))
        elif case == "different_plan":
            ReviewState().approve(spec_dir)
            _write_plan(spec_dir, PLAN_B)

        agent_mock = AsyncMock(return_value=None)
        with pytest.raises(SystemExit) as exc:
            _run_build(spec_dir, tmp_path, agent_mock)
        assert exc.value.code == 0
        agent_mock.assert_not_awaited()

    def test_valid_approval_restart_runs_agent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AIFACTORY_SOLO_MODE", "off")
        spec_dir = _make_spec(tmp_path)
        _write_plan(spec_dir, PLAN_A)
        ReviewState().approve(spec_dir)

        agent_mock = AsyncMock(return_value=None)
        _run_build(spec_dir, tmp_path, agent_mock)
        agent_mock.assert_awaited()

    def test_should_pass_force_false_when_review_required(self, tmp_path):
        from server.services.task_phase import should_pass_force

        spec_dir = _make_spec(tmp_path)
        assert should_pass_force(spec_dir, False) is False
        assert should_pass_force(spec_dir, True) is True
