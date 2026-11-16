"""A coder's QA pre-approval cannot bypass required review (#1987)."""

import contextlib
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from review.state import ReviewState  # noqa: E402
from run_gates import RunGates  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIFACTORY_AUTH_PREFLIGHT", "off")
    monkeypatch.setenv("AIFACTORY_MODEL_REGISTRY_ENFORCE", "off")
    monkeypatch.delenv("AIFACTORY_SOLO_MODE", raising=False)


def _spec(tmp_path: Path, requires_review: bool) -> Path:
    spec = tmp_path / "spec"
    spec.mkdir()
    (spec / "spec.md").write_text("# Feature\n")
    (spec / "task_metadata.json").write_text(
        json.dumps({"requireReviewBeforeCoding": requires_review})
    )
    (spec / "implementation_plan.json").write_text(
        json.dumps(
            {
                "feature": "a",
                "phases": [
                    {"phase": 1, "subtasks": [{"id": "1", "status": "completed"}]}
                ],
                "qa_signoff": {"status": "approved"},
            }
        )
    )
    if requires_review:
        ReviewState().approve(spec)
    return spec


@pytest.mark.parametrize("requires_review", [True, False])
def test_build_runs_qa_for_required_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, requires_review: bool
) -> None:
    from cli import build_commands  # noqa: PLC0415

    spec = _spec(tmp_path, requires_review)
    qa = AsyncMock(return_value=False)
    direct = build_commands.WorkspaceMode.DIRECT
    patches = [
        patch("agent.run_autonomous_agent", new=AsyncMock()),
        patch("agent.sync_plan_to_source", return_value=False),
        patch("qa_loop.run_qa_validation_loop", new=qa),
        patch("cli.utils.validate_environment", return_value=True),
        patch("cli.utils.print_banner"),
        patch.object(build_commands, "choose_workspace", return_value=direct),
        patch.object(build_commands, "get_existing_build_worktree", return_value=None),
        patch.object(build_commands, "build_is_silent_noop", return_value=False),
        patch.object(build_commands, "_record_git_baseline_early"),
        patch.object(build_commands, "_check_qa_git_metadata"),
        patch("phase_event.emit_phase"),
    ]
    with contextlib.ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        with contextlib.suppress(SystemExit):
            build_commands.handle_build_command(
                project_dir=tmp_path,
                spec_dir=spec,
                model="sonnet",
                max_iterations=1,
                verbose=False,
                force_isolated=False,
                force_direct=True,
                auto_continue=True,
                skip_qa=False,
                force_bypass_approval=False,
            )

    assert qa.await_count == int(requires_review)


@pytest.mark.asyncio
@pytest.mark.parametrize("requires_review", [True, False])
async def test_loop_clears_preapproval_before_reviewer(
    tmp_path: Path, requires_review: bool
) -> None:
    import qa_loop  # noqa: PLC0415

    spec = _spec(tmp_path, requires_review)
    gates = RunGates.capture(spec, None, "sonnet")

    async def reviewer(*_args: object, **_kwargs: object) -> None:
        assert "qa_signoff" not in json.loads(
            (spec / "implementation_plan.json").read_text()
        )
        raise KeyboardInterrupt

    session = AsyncMock(side_effect=reviewer)
    provider = MagicMock(
        __aenter__=AsyncMock(), __aexit__=AsyncMock(return_value=False)
    )
    loop = qa_loop.run_qa_validation_loop
    with patch.dict(
        loop.__globals__,
        {
            "emit_phase": MagicMock(),
            "is_build_complete": MagicMock(return_value=True),
            "is_no_test_project": MagicMock(return_value=False),
            "get_task_logger": MagicMock(return_value=None),
            "get_qa_iteration_count": MagicMock(return_value=0),
            "_create_qa_reviewer_provider": MagicMock(return_value=provider),
            "run_qa_agent_session": session,
        },
    ):
        if requires_review:
            with pytest.raises(KeyboardInterrupt):
                await loop(tmp_path, spec, "sonnet", gates=gates)
        else:
            assert await loop(tmp_path, spec, "sonnet", gates=gates) is True
    assert session.await_count == int(requires_review)
