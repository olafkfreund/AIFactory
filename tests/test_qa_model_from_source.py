"""#1969: model and thinking selection reads source_spec_dir, not the worktree copy.

The worktree copy of ``task_metadata.json`` is agent-writable. Each test puts a
haiku / no-thinking choice there and checks no session resolves it.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import phase_config  # noqa: E402
import qa_loop  # noqa: E402

HAIKU = phase_config.resolve_model_id("haiku")
NO_THINKING = phase_config.get_thinking_budget("none")  # None; defaults give ints
WORKTREE_META = {
    "phaseModels": {"qa": "haiku", "qa_fixer": "haiku", "coding": "haiku"},
    "phaseThinking": {"qa": "none", "qa_fixer": "none", "coding": "none"},
}


class _StopError(BaseException):
    """Ends a driven run; BaseException so no ``except Exception`` swallows it."""


@pytest.fixture(autouse=True)
def _no_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIFACTORY_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("AIFACTORY_ROUTING_POLICY", raising=False)


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "task_metadata.json").write_text("{}")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "task_metadata.json").write_text(json.dumps(WORKTREE_META))
    return source, worktree


def _recorders() -> tuple[dict[str, MagicMock], list[object]]:
    """Real phase_config lookups that record every value they return."""
    seen: list[object] = []

    def wrap(fn):  # noqa: ANN001, ANN202
        def rec(*a, **kw):  # noqa: ANN002, ANN003, ANN202
            seen.append(fn(*a, **kw))
            return seen[-1]

        return MagicMock(side_effect=rec)

    return {
        "get_phase_model": wrap(phase_config.get_phase_model),
        "get_phase_thinking_budget": wrap(phase_config.get_phase_thinking_budget),
    }, seen


def _client() -> MagicMock:
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


async def _drive_qa(
    tmp_path: Path, reviewer: AsyncMock, extra: dict | None = None
) -> list[object]:
    source, worktree = _dirs(tmp_path)
    # Human feedback: the fixer-first path runs before the loop.
    (worktree / "QA_FIX_REQUEST.md").write_text("fix it\n")
    recorders, seen = _recorders()
    loop_globals = qa_loop.run_qa_validation_loop.__globals__
    with (
        patch.dict(
            loop_globals,
            {
                **recorders,
                "is_build_complete": MagicMock(return_value=True),
                "is_qa_approved": MagicMock(return_value=False),
                "is_no_test_project": MagicMock(return_value=False),
                "get_task_logger": MagicMock(return_value=None),
                "emit_phase": MagicMock(),
                "get_qa_iteration_count": MagicMock(return_value=0),
                "check_git_metadata": MagicMock(),
                "request_review": MagicMock(),
                "record_started": MagicMock(),
                "resolve_review": MagicMock(),
                "record_iteration": MagicMock(),
                "get_qa_signoff_status": MagicMock(return_value=None),
                "get_iteration_history": MagicMock(return_value=[]),
                "has_recurring_issues": MagicMock(return_value=(False, [])),
                "create_client": MagicMock(return_value=_client()),
                "run_qa_fixer_session": AsyncMock(return_value=("fixed", "")),
                "_create_qa_reviewer_provider": MagicMock(return_value=_client()),
                "run_qa_agent_session": reviewer,
                **(extra or {}),
            },
        ),
        pytest.raises(_StopError),
    ):
        await qa_loop.run_qa_validation_loop(
            project_dir=tmp_path,
            spec_dir=worktree,
            model="sonnet",
            source_spec_dir=source,
        )
    return seen


@pytest.mark.asyncio
async def test_qa_fixers_and_reviewer_come_from_source(tmp_path: Path) -> None:
    # fixer-first, reviewer (rejects), fixer in the loop, reviewer again (stop).
    reviewer = AsyncMock(side_effect=[("rejected", "issues"), _StopError()])
    seen = await _drive_qa(tmp_path, reviewer)

    assert reviewer.await_count == 2
    assert len(seen) == 8  # model + thinking for 2 fixers and 2 reviewers
    assert HAIKU not in seen
    assert NO_THINKING not in seen


@pytest.mark.asyncio
async def test_qa_failover_provider_comes_from_source(tmp_path: Path) -> None:
    reviewer = AsyncMock(return_value=("error", "stalled"))
    failover = MagicMock(side_effect=_StopError())
    seen = await _drive_qa(
        tmp_path,
        reviewer,
        {"next_provider": failover, "MAX_CONSECUTIVE_ERRORS": 1},
    )

    failover.assert_called_once()
    assert seen.count(HAIKU) == 0
    assert len(seen) == 5  # fixer-first 2, reviewer 2, failover model 1


@pytest.mark.asyncio
async def test_parallel_coder_model_comes_from_source(tmp_path: Path) -> None:
    import agents.parallel_integration as wave

    source, worktree = _dirs(tmp_path)
    recorders, seen = _recorders()
    budget = recorders["get_phase_thinking_budget"]
    real_budget = budget.side_effect

    def budget_then_stop(*a, **kw):  # noqa: ANN002, ANN003, ANN202
        real_budget(*a, **kw)
        raise _StopError

    budget.side_effect = budget_then_stop
    with (
        patch.object(wave, "get_phase_model", recorders["get_phase_model"]),
        patch.object(wave, "get_phase_thinking_budget", budget),
        patch.object(wave, "_task_branch", MagicMock(return_value="main")),
        pytest.raises(_StopError),
    ):
        await wave.run_parallel_coding_phase(
            plan=MagicMock(),
            phase=MagicMock(),
            project_dir=tmp_path,
            spec_dir=worktree,
            model="sonnet",
            workers=2,
            source_spec_dir=source,
        )

    assert len(seen) == 2
    assert HAIKU not in seen
    assert NO_THINKING not in seen


def test_build_passes_source_spec_dir_to_qa(tmp_path: Path) -> None:
    from cli import build_commands

    source, worktree = _dirs(tmp_path)
    qa = AsyncMock(side_effect=_StopError())
    review_state = MagicMock()
    review_state.is_approval_valid.return_value = True
    patches = [
        patch("agent.run_autonomous_agent", new=AsyncMock(return_value=None)),
        patch("agent.sync_plan_to_source", MagicMock(return_value=False)),
        patch("qa_loop.should_run_qa", MagicMock(return_value=True)),
        patch("qa_loop.run_qa_validation_loop", qa),
        patch("cli.utils.validate_environment", MagicMock(return_value=True)),
        patch("cli.utils.print_banner", MagicMock()),
        patch.object(
            build_commands,
            "choose_workspace",
            MagicMock(return_value=build_commands.WorkspaceMode.ISOLATED),
        ),
        patch.object(
            build_commands,
            "setup_workspace",
            MagicMock(return_value=(tmp_path, None, worktree)),
        ),
        patch.object(
            build_commands, "get_existing_build_worktree", MagicMock(return_value=None)
        ),
        patch.object(
            build_commands, "build_is_silent_noop", MagicMock(return_value=False)
        ),
        patch.object(
            build_commands,
            "ReviewState",
            MagicMock(load=MagicMock(return_value=review_state)),
        ),
    ]
    with contextlib.ExitStack() as stack, pytest.raises(_StopError):
        for p in patches:
            stack.enter_context(p)
        build_commands.handle_build_command(
            project_dir=tmp_path,
            spec_dir=source,
            model="claude-sonnet-4-5",
            max_iterations=1,
            verbose=False,
            force_isolated=True,
            force_direct=False,
            auto_continue=True,
            skip_qa=False,
            force_bypass_approval=False,
        )

    assert qa.await_args.kwargs["spec_dir"] == worktree
    assert qa.await_args.kwargs["source_spec_dir"] == source
