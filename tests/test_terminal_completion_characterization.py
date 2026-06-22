"""Characterization tests for AgentService terminal-completion orchestration (#556).

These pin the CURRENT observable fire-once behaviour of
``AgentService._update_plan_status`` BEFORE the completion orchestration is
extracted into its own module. They are intentionally behaviour-describing (not
prescriptive) so the later extraction can be proven behaviour-preserving.

Invariants captured:
  - A terminal COMPLETED build writes the two fire-once markers
    (``.terminal_completion_emitted`` + ``.terminal_side_effects_done``) and
    emits the RFC-0001 completion event exactly once.
  - Re-running the COMPLETED transition does NOT re-emit (markers gate it),
    regardless of the ``emit_events`` flag — gating is NOT tied to emit_events
    (the #71 regression).
  - FAILED writes the completion marker (and emits status=failed) but NOT the
    side-effects marker (side-effects are COMPLETED-only).
  - A non-terminal transition (human_review) writes neither marker and does not
    emit a terminal completion event.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

from server.services.agent_service import AgentService  # noqa: E402

_EMIT_TARGET = "server.services.completion.emit_terminal_completion"


def _make_service(tmp_path: Path) -> AgentService:
    # Bypass the settings-dependent __init__; the method only needs backend_path,
    # which is a property reading settings.BACKEND_PATH.
    svc = AgentService.__new__(AgentService)
    svc.settings = SimpleNamespace(BACKEND_PATH=str(tmp_path / "backend"))
    return svc


def _make_spec(project_path: Path, spec_id: str) -> Path:
    spec_dir = project_path / ".aifactory" / "specs" / spec_id
    spec_dir.mkdir(parents=True)
    plan = {
        "phases": [{"name": "build", "subtasks": []}],
        "status": "in_progress",
    }
    (spec_dir / "implementation_plan.json").write_text(json.dumps(plan))
    return spec_dir


_COMPLETION_MARKER = ".terminal_completion_emitted"
_SIDE_EFFECTS_MARKER = ".terminal_side_effects_done"


@pytest.mark.asyncio
async def test_completed_writes_markers_and_emits_once(tmp_path: Path) -> None:
    spec_id, task_id = "spec-001", "proj:spec-001"
    spec_dir = _make_spec(tmp_path, spec_id)
    svc = _make_service(tmp_path)

    with patch(_EMIT_TARGET) as mock_emit:
        await svc._update_plan_status(
            tmp_path, spec_id, "completed", task_id, emit_events=False
        )

    assert (spec_dir / _COMPLETION_MARKER).exists()
    assert (spec_dir / _SIDE_EFFECTS_MARKER).exists()
    assert mock_emit.call_count == 1
    # The completion event carries status="completed".
    assert mock_emit.call_args.kwargs.get("status") == "completed"


@pytest.mark.asyncio
async def test_completed_is_fire_once_across_both_emit_events_paths(
    tmp_path: Path,
) -> None:
    spec_id, task_id = "spec-002", "proj:spec-002"
    _make_spec(tmp_path, spec_id)
    svc = _make_service(tmp_path)

    with patch(_EMIT_TARGET) as mock_emit:
        # The _monitor_process terminal path uses emit_events=False; a mid-run
        # caller uses True. Both must collapse to a single emission.
        await svc._update_plan_status(
            tmp_path, spec_id, "completed", task_id, emit_events=False
        )
        await svc._update_plan_status(
            tmp_path, spec_id, "completed", task_id, emit_events=True
        )

    assert mock_emit.call_count == 1


@pytest.mark.asyncio
async def test_failed_emits_completion_but_no_side_effects(tmp_path: Path) -> None:
    spec_id, task_id = "spec-003", "proj:spec-003"
    spec_dir = _make_spec(tmp_path, spec_id)
    svc = _make_service(tmp_path)

    with patch(_EMIT_TARGET) as mock_emit:
        await svc._update_plan_status(
            tmp_path, spec_id, "failed", task_id, emit_events=False
        )

    assert (spec_dir / _COMPLETION_MARKER).exists()
    assert not (spec_dir / _SIDE_EFFECTS_MARKER).exists()
    assert mock_emit.call_count == 1
    assert mock_emit.call_args.kwargs.get("status") == "failed"


@pytest.mark.asyncio
async def test_non_terminal_transition_emits_nothing(tmp_path: Path) -> None:
    spec_id, task_id = "spec-004", "proj:spec-004"
    spec_dir = _make_spec(tmp_path, spec_id)
    svc = _make_service(tmp_path)

    with patch(_EMIT_TARGET) as mock_emit:
        await svc._update_plan_status(
            tmp_path, spec_id, "human_review", task_id, emit_events=True
        )

    assert not (spec_dir / _COMPLETION_MARKER).exists()
    assert not (spec_dir / _SIDE_EFFECTS_MARKER).exists()
    assert mock_emit.call_count == 0
