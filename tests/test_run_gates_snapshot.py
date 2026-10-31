"""RunGates reads every run gate once, before the first agent session (#1983)."""

from __future__ import annotations

import contextlib
import dataclasses
import importlib
import json
import os
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from phase_config import (  # noqa: E402
    get_phase_model,
    get_phase_thinking_budget,
    get_thinking_budget,
)
from review.state import ReviewState  # noqa: E402
from run_gates import RunGates  # noqa: E402

PLAN = {"feature": "a", "phases": [{"phase": 1, "subtasks": [{"id": "1"}]}]}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AIFACTORY_FALLBACK_MODEL", "AIFACTORY_SOLO_MODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AIFACTORY_MODEL_REGISTRY_ENFORCE", "off")


def _spec(tmp_path: Path, name: str = "spec", **meta: object) -> Path:
    spec = tmp_path / name
    spec.mkdir()
    (spec / "implementation_plan.json").write_text(json.dumps(PLAN))
    if meta:
        (spec / "task_metadata.json").write_text(json.dumps(meta))
    return spec


def test_capture_reads_source_dir_not_worktree_copy(tmp_path: Path) -> None:
    source = _spec(tmp_path, "source", soloMode=True)
    worktree = _spec(tmp_path, "worktree", requireReviewBeforeCoding=True)
    gates = RunGates.capture(worktree, source, None)
    assert gates.solo is True
    assert gates.requires_review is False


def test_metadata_empty_when_absent_and_frozen_after_capture(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    gates = RunGates.capture(spec, None, None)
    assert gates.metadata == {}
    before = (gates.model("qa"), gates.thinking_budget("qa"))
    (spec / "task_metadata.json").write_text(
        json.dumps({"pinnedModel": "haiku", "thinkingLevel": "none"})
    )
    assert (gates.model("qa"), gates.thinking_budget("qa")) == before
    assert gates.thinking_budget("qa") != get_thinking_budget("none")


def test_forged_approval_after_capture_is_ignored(tmp_path: Path) -> None:
    spec = _spec(tmp_path, requireReviewBeforeCoding=True)
    ReviewState().approve(spec)
    gates = RunGates.capture(spec, None, None)
    assert gates.approval_valid(spec)

    plan = {**PLAN, "feature": "changed"}
    (spec / "implementation_plan.json").write_text(json.dumps(plan))
    assert not gates.approval_valid(spec)
    ReviewState().approve(spec)  # forged: hashes of the changed plan
    assert not gates.approval_valid(spec)


def test_forged_approval_does_not_validate_unapproved_capture(tmp_path: Path) -> None:
    spec = _spec(tmp_path, requireReviewBeforeCoding=True)
    gates = RunGates.capture(spec, None, None)
    ReviewState().approve(spec)
    assert not gates.approval_valid(spec)


def test_frozen(tmp_path: Path) -> None:
    gates = RunGates.capture(_spec(tmp_path), None, None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        gates.solo = True  # type: ignore[misc]


def test_subtask_model_without_snapshot_uses_live_model(tmp_path: Path) -> None:
    gates = dataclasses.replace(
        RunGates.capture(_spec(tmp_path), None, None), approved_plan=None
    )
    assert gates.subtask_model("1", "haiku") == "haiku"
    assert gates.subtask_model("1", "") is None
    assert gates.subtask_model("1", 42) is None


def test_subtask_model_uses_snapshot_model(tmp_path: Path) -> None:
    gates = dataclasses.replace(
        RunGates.capture(_spec(tmp_path), None, None),
        approved_plan={"phases": [{"subtasks": [{"id": "1", "model": "sonnet"}]}]},
    )
    assert gates.subtask_model("1", "haiku") == "sonnet"


def test_subtask_model_missing_or_unset_in_snapshot(tmp_path: Path) -> None:
    gates = dataclasses.replace(
        RunGates.capture(_spec(tmp_path), None, None),
        approved_plan={"phases": [{"subtasks": [{"id": "1"}]}]},
    )
    assert gates.subtask_model("1", "haiku") is None
    assert gates.subtask_model("2", "haiku") is None
    assert (
        dataclasses.replace(gates, approved_plan={}).subtask_model("1", "haiku") is None
    )


def test_explicit_metadata_wins_over_the_file(tmp_path: Path) -> None:
    spec = _spec(tmp_path, pinnedModel="haiku", thinkingLevel="none")
    assert get_phase_model(spec, "qa", metadata={}) != get_phase_model(spec, "qa")
    assert get_phase_thinking_budget(spec, "qa", metadata={}) != (
        get_thinking_budget("none")
    )


@pytest.mark.asyncio
async def test_qa_loop_ignores_metadata_forged_by_a_qa_session(
    tmp_path: Path,
) -> None:
    from unittest.mock import AsyncMock, MagicMock, patch  # noqa: PLC0415

    import qa_loop  # noqa: PLC0415

    spec = _spec(tmp_path, phaseModels={"qa": "sonnet"}, phaseThinking={"qa": "high"})
    seen: list[tuple[str, int | None]] = []

    def provider(_name, _project, _spec, model, budget):
        seen.append((model, budget))
        if len(seen) == 2:
            raise KeyboardInterrupt  # round 2 reached: stop the loop here
        return MagicMock(
            __aenter__=AsyncMock(), __aexit__=AsyncMock(return_value=False)
        )

    async def forging_session(*_a, **_k):
        (spec / "task_metadata.json").write_text(
            json.dumps({"pinnedModel": "haiku", "thinkingLevel": "none"})
        )
        return "error", "boom"

    loop = qa_loop.run_qa_validation_loop
    with (
        patch.dict(
            loop.__globals__,
            {
                "emit_phase": MagicMock(),
                "is_build_complete": MagicMock(return_value=True),
                "is_no_test_project": MagicMock(return_value=False),
                "get_task_logger": MagicMock(return_value=None),
                "get_qa_iteration_count": MagicMock(return_value=0),
                "check_git_metadata": MagicMock(),
                "_create_qa_reviewer_provider": provider,
                "run_qa_agent_session": forging_session,
            },
        ),
        pytest.raises(KeyboardInterrupt),
    ):
        await loop(tmp_path, spec, "sonnet")

    assert len(seen) == 2
    assert seen[1] == seen[0]
    assert "haiku" not in seen[1][0]
    assert seen[1][1] == get_thinking_budget("high")


def test_build_gates_survive_files_forged_during_the_coder_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """handle_build_command hands QA the run-start gates, not the live files."""
    from unittest.mock import MagicMock, patch  # noqa: PLC0415

    from cli import build_commands  # noqa: PLC0415

    monkeypatch.setenv("AIFACTORY_AUTH_PREFLIGHT", "off")
    spec = _spec(
        tmp_path,
        requireReviewBeforeCoding=True,
        phaseModels={"qa": "sonnet"},
        phaseThinking={"qa": "high"},
    )
    (spec / "spec.md").write_text("# Feature\n")
    ReviewState().approve(spec)
    seen: dict[str, object] = {}

    async def fake_coder(*_a: object, **_k: object) -> None:
        # The agent can write its spec dir: forge approval, solo mode and a cheap model.
        (spec / "task_metadata.json").write_text(
            json.dumps(
                {"soloMode": True, "pinnedModel": "haiku", "thinkingLevel": "none"}
            )
        )
        ReviewState().approve(spec)

    async def fake_qa(*_a: object, gates: RunGates, **_k: object) -> bool:
        seen["model"] = gates.model("qa")
        seen["thinking"] = gates.thinking_budget("qa")
        seen["solo"] = gates.solo
        (spec / "implementation_plan.json").write_text(
            json.dumps({**PLAN, "feature": "widened"})
        )
        ReviewState().approve(spec)  # forged against the widened plan
        seen["approval_valid"] = gates.approval_valid(spec)
        return False

    direct = build_commands.WorkspaceMode.DIRECT
    patches = [
        patch("agent.run_autonomous_agent", new=fake_coder),
        patch("agent.sync_plan_to_source", MagicMock(return_value=False)),
        patch("qa_loop.should_run_qa", MagicMock(return_value=True)),
        patch("qa_loop.run_qa_validation_loop", new=fake_qa),
        patch("cli.utils.validate_environment", MagicMock(return_value=True)),
        patch("cli.utils.print_banner", MagicMock()),
        patch.object(
            build_commands, "choose_workspace", MagicMock(return_value=direct)
        ),
        patch.object(
            build_commands, "get_existing_build_worktree", MagicMock(return_value=None)
        ),
        patch.object(
            build_commands, "build_is_silent_noop", MagicMock(return_value=False)
        ),
        patch.object(build_commands, "_record_git_baseline_early", MagicMock()),
        patch.object(build_commands, "_check_qa_git_metadata", MagicMock()),
        patch("phase_event.emit_phase", MagicMock()),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
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

    assert seen["model"] == get_phase_model(
        Path(os.devnull), "qa", None, metadata={"phaseModels": {"qa": "sonnet"}}
    )
    assert seen["thinking"] == get_thinking_budget("high")
    assert seen["solo"] is False
    assert seen["approval_valid"] is False


def test_approval_still_valid_refuses_forged_review_state(tmp_path: Path) -> None:
    from cli.build_commands import _approval_still_valid  # noqa: PLC0415

    spec = _spec(tmp_path, requireReviewBeforeCoding=True)
    ReviewState().approve(spec)
    gates = RunGates.capture(spec, None, None)
    (spec / "implementation_plan.json").write_text(json.dumps({**PLAN, "x": 1}))
    ReviewState().approve(spec)  # forged against the changed plan
    assert _approval_still_valid(spec, None, gates) is False


_FORGED = {
    "pinnedModel": "haiku",
    "thinkingLevel": "none",
    "phaseThinking": {"coding": "none"},
}
_CODING_META = {
    "phaseModels": {"coding": "sonnet"},
    "phaseThinking": {"coding": "high"},
}


def _client_args(create_client) -> list[tuple[str, int | None]]:
    return [
        (c.args[2], c.kwargs["max_thinking_tokens"])
        for c in create_client.call_args_list
    ]


async def _inline_to_thread(func, /, *args, **kwargs):
    return func(*args, **kwargs)


@pytest.mark.parametrize("snapshot_model", [None, "haiku"])
@pytest.mark.asyncio
async def test_serial_coder_second_session_ignores_metadata_forged_by_the_first(
    tmp_path: Path,
    snapshot_model: str | None,
) -> None:
    from unittest.mock import AsyncMock, MagicMock, patch  # noqa: PLC0415

    from agents import coder  # noqa: PLC0415

    source = _spec(tmp_path, "source", **_CODING_META)
    worktree = _spec(tmp_path, "worktree", **_CODING_META)
    snapshot_subtask = {"id": "1"}
    if snapshot_model:
        snapshot_subtask["model"] = snapshot_model
    gates = dataclasses.replace(
        RunGates.capture(worktree, source, "sonnet"),
        approved_plan={"phases": [{"subtasks": [snapshot_subtask]}]},
    )
    client = MagicMock(
        __aenter__=AsyncMock(return_value=None), __aexit__=AsyncMock(return_value=False)
    )
    client.__aenter__.return_value = client

    class SecondClientReached(Exception):
        pass

    create_client = MagicMock(side_effect=[client, SecondClientReached])
    record_turn = MagicMock()

    async def session(*_a: object, **_k: object) -> tuple[str, str, dict[str, object]]:
        (source / "task_metadata.json").write_text(
            json.dumps({**_CODING_META, **_FORGED})
        )
        (worktree / "task_metadata.json").write_text(
            json.dumps({**_CODING_META, **_FORGED})
        )
        return ("continue", "", {"usage": {"input_tokens": 1}})

    patchers = {
        "create_client": create_client,
        "run_session_guarded": session,
        "post_session_processing": AsyncMock(return_value=True),
        "check_git_metadata": MagicMock(),
        "get_graphiti_context": AsyncMock(return_value=""),
        "RecoveryManager": MagicMock(
            return_value=MagicMock(get_attempt_count=MagicMock(return_value=0))
        ),
        "StatusManager": MagicMock(),
        "get_task_logger": MagicMock(return_value=None),
        "debug_memory_system_status": MagicMock(),
        "record_turn": record_turn,
        "CompactionDetector": MagicMock(),
        "build_operational_context": MagicMock(return_value=""),
        "drain_inbox": MagicMock(return_value=[]),
        "is_build_complete": MagicMock(return_value=False),
        "print_progress_summary": MagicMock(),
        "print_session_header": MagicMock(),
        "count_subtasks": MagicMock(return_value=(0, 1)),
        "count_subtasks_detailed": MagicMock(
            return_value={"completed": 0, "total": 1, "in_progress": 0}
        ),
        "get_current_phase": MagicMock(return_value=None),
        "get_next_subtask": MagicMock(
            return_value={"id": "1", "status": "pending", "model": "haiku"}
        ),
        "get_latest_commit": MagicMock(return_value="abc"),
        "get_commit_count": MagicMock(return_value=0),
        "emit_phase": MagicMock(),
        "AUTO_CONTINUE_DELAY_SECONDS": 0,
    }
    with contextlib.ExitStack() as stack, pytest.raises(SecondClientReached):
        for name, repl in patchers.items():
            stack.enter_context(patch.object(coder, name, repl))
        stack.enter_context(patch("agents.utils.publish_plan", MagicMock()))
        stack.enter_context(patch("asyncio.to_thread", new=_inline_to_thread))
        stack.enter_context(
            patch("agents.parallel_integration.emit_worker_progress_heartbeat")
        )
        await coder.run_autonomous_agent(
            project_dir=tmp_path,
            spec_dir=worktree,
            model="sonnet",
            max_iterations=2,
            source_spec_dir=source,
            gates=gates,
        )

    first, second = _client_args(create_client)
    assert second == first
    assert second[0] == (snapshot_model or gates.model("coding"))
    assert second[1] == get_thinking_budget("high")
    assert record_turn.called
    assert all(
        call.kwargs["routing_metadata"] == gates.metadata
        for call in record_turn.call_args_list
    )


@pytest.mark.parametrize("snapshot_model", [None, "haiku"])
@pytest.mark.asyncio
async def test_parallel_wave_ignores_metadata_forged_after_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, snapshot_model: str | None
) -> None:
    from unittest.mock import AsyncMock, MagicMock  # noqa: PLC0415

    import agents.parallel_integration as wave  # noqa: PLC0415
    from agents.parallel_runner import PhaseRunResult  # noqa: PLC0415
    from implementation_plan.plan import ImplementationPlan  # noqa: PLC0415

    importlib.import_module("core.worktree")
    spec = _spec(tmp_path, **_CODING_META)
    (spec / "implementation_plan.json").write_text(
        json.dumps(
            {
                "feature": "f",
                "phases": [
                    {
                        "phase": 1,
                        "name": "P",
                        "parallel_safe": True,
                        "subtasks": [
                            {
                                "id": "A",
                                "description": "a",
                                "status": "pending",
                                "model": "haiku",
                            }
                        ],
                    }
                ],
            }
        )
    )
    child = tmp_path / "child"
    (child / ".aifactory" / "specs" / spec.name).mkdir(parents=True)
    wt = MagicMock()
    wt.create_worktree.return_value = MagicMock(path=child)
    monkeypatch.setattr(
        sys.modules["core.worktree"], "WorktreeManager", MagicMock(return_value=wt)
    )
    create_client = MagicMock()
    record_turn = MagicMock()
    for name, repl in {
        "_task_branch": MagicMock(return_value="main"),
        "create_client": create_client,
        "generate_subtask_prompt": MagicMock(return_value="p"),
        "load_subtask_context": MagicMock(return_value={}),
        "get_graphiti_context": AsyncMock(return_value=""),
        "_head_commit": MagicMock(return_value="abc"),
        "run_session_guarded": AsyncMock(
            return_value=("complete", "", {"usage": {"input_tokens": 1}})
        ),
        "check_git_metadata": MagicMock(),
        "read_test_evidence": MagicMock(return_value={}),
        "_capture_memory": AsyncMock(),
        "record_subtask_started": MagicMock(),
        "record_turn": record_turn,
        "emit_worker_live_event": MagicMock(),
    }.items():
        monkeypatch.setattr(wave, name, repl)

    async def _phase(subtasks, *, run_subtask, **_kw):
        await run_subtask(subtasks[0], index=0)
        return PhaseRunResult(completed_ids=[], failed_ids=[])

    monkeypatch.setattr(wave, "run_parallel_phase", _phase)
    monkeypatch.setattr(wave.asyncio, "to_thread", _inline_to_thread)
    snapshot_subtask = {"id": "A"}
    if snapshot_model:
        snapshot_subtask["model"] = snapshot_model
    gates = dataclasses.replace(
        RunGates.capture(spec, None, "sonnet"),
        approved_plan={"phases": [{"subtasks": [snapshot_subtask]}]},
    )
    expected = (
        snapshot_model or gates.model("coding"),
        gates.thinking_budget("coding"),
    )
    (spec / "task_metadata.json").write_text(json.dumps({**_CODING_META, **_FORGED}))
    plan = ImplementationPlan.load(spec / "implementation_plan.json")
    await wave.run_parallel_coding_phase(
        plan=plan,
        phase=plan.phases[0],
        project_dir=tmp_path,
        spec_dir=spec,
        model="sonnet",
        workers=1,
        gates=gates,
    )

    assert _client_args(create_client) == [expected]
    assert expected[1] == get_thinking_budget("high")
    record_turn.assert_called_once()
    assert record_turn.call_args.kwargs["routing_metadata"] == gates.metadata


def test_injection_scan_ignores_approval_forged_after_capture(
    tmp_path: Path,
) -> None:
    from unittest.mock import patch  # noqa: PLC0415

    from agents.coder import _injection_scan_blocks  # noqa: PLC0415
    from security.content_scan import ScanResult  # noqa: PLC0415

    spec = _spec(tmp_path)
    gates = RunGates.capture(spec, None, None)
    ReviewState().approve(spec)  # forged after capture
    flagged = ScanResult(verdict="flagged", mode="on", matched=[{"pattern": "p"}])
    with (
        patch("security.content_scan.run_scan_gate", return_value=flagged),
        patch("agents.coder.emit_phase"),
    ):
        assert _injection_scan_blocks(spec, tmp_path, None, gates) is True
