"""#1070: a build that produced nothing must not be declared complete.

A ``factory:low`` task spent 898k tokens, wrote a plan document and no source
file, exited 0, advanced to ``human_review`` (= built, awaiting a person) and
handed off to TFactory — which then verified a branch identical to main.

``memory/build_commits.json`` was written, was empty, and gated nothing.

These tests pin the evidence gate: before terminal completion is declared,
the build must show at least one commit (the recovery ledger, or the build
worktree's own count against the base branch). A MEASURED zero is a build
failure, not a review request, and is never handed off. An UNKNOWABLE answer
(no ledger, no worktree) still completes — refusing a build we merely could
not measure would be worse than the bug (the #984 rule).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from pfactory import tfactory_client as tc  # noqa: E402
from server.services import task_control  # noqa: E402
from server.services.completion_orchestration import (  # noqa: E402
    run_terminal_completion,
)

_LOG = logging.getLogger(__name__)

_EMIT_TARGET = "server.services.completion.emit_terminal_completion"
_HANDOFF_TARGET = "pfactory.tfactory_client.maybe_auto_handoff_tfactory"
_SIDE_EFFECTS_MARKER = ".terminal_side_effects_done"


def _make_spec(project_path: Path, spec_id: str) -> Path:
    spec_dir = project_path / ".aifactory" / "specs" / spec_id
    spec_dir.mkdir(parents=True)
    (spec_dir / "implementation_plan.json").write_text(
        json.dumps({"phases": [{"name": "build", "subtasks": []}]})
    )
    return spec_dir


def _write_ledger(spec_dir: Path, commits: list[str]) -> None:
    """Write the RecoveryManager ledger the build keeps in its memory dir."""
    memory = spec_dir / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "build_commits.json").write_text(
        json.dumps(
            {
                "commits": [{"hash": h, "subtask_id": "1.1"} for h in commits],
                "last_good_commit": commits[-1] if commits else None,
            }
        )
    )


async def _complete(spec_dir: Path, project_path: Path, spec_id: str):
    """Drive the COMPLETED terminal transition; return (emit_mock, handoff_mock)."""
    with (
        patch(_EMIT_TARGET) as emit,
        patch(_HANDOFF_TARGET, new=AsyncMock(return_value={"sent": True})) as handoff,
    ):
        await run_terminal_completion(
            spec_dir=spec_dir,
            project_path=project_path,
            spec_id=spec_id,
            task_id=f"proj:{spec_id}",
            backend_path=_ROOT / "apps" / "backend",
            is_terminal=True,
            is_completed=True,
            terminal_status="completed",
            logger=_LOG,
        )
    return emit, handoff


# --------------------------------------------------------------------------
# The ledger is a commit-count source (the kubejob path has no local worktree)
# --------------------------------------------------------------------------


def test_empty_ledger_counts_as_zero_commits(tmp_path: Path) -> None:
    spec_dir = _make_spec(tmp_path, "096-x")
    _write_ledger(spec_dir, [])
    assert tc.build_commit_count(spec_dir, "096-x") == 0


def test_populated_ledger_counts_its_commits(tmp_path: Path) -> None:
    spec_dir = _make_spec(tmp_path, "097-x")
    _write_ledger(spec_dir, ["aaa1111", "bbb2222"])
    assert tc.build_commit_count(spec_dir, "097-x") == 2


def test_missing_ledger_stays_unknowable(tmp_path: Path) -> None:
    spec_dir = _make_spec(tmp_path, "098-x")
    assert tc.build_commit_count(spec_dir, "098-x") is None


def test_corrupt_ledger_stays_unknowable(tmp_path: Path) -> None:
    spec_dir = _make_spec(tmp_path, "099-x")
    (spec_dir / "memory").mkdir()
    (spec_dir / "memory" / "build_commits.json").write_text("{not json")
    assert tc.build_commit_count(spec_dir, "099-x") is None


# --------------------------------------------------------------------------
# The gate itself
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_evidence_is_a_failure_not_a_review_request(tmp_path: Path) -> None:
    """The #1070 shape: exit 0, empty ledger. Must NOT report completed."""
    spec_id = "096-add-an-is-palindrome-helper-py"
    spec_dir = _make_spec(tmp_path, spec_id)
    _write_ledger(spec_dir, [])

    emit, handoff = await _complete(spec_dir, tmp_path, spec_id)

    assert emit.call_args.kwargs.get("status") == "failed"
    assert not (spec_dir / _SIDE_EFFECTS_MARKER).exists()
    handoff.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_evidence_marks_the_task_as_needing_attention(
    tmp_path: Path,
) -> None:
    """The board must not read 'built, awaiting a person' for an empty build."""
    spec_id = "096-y"
    spec_dir = _make_spec(tmp_path, spec_id)
    _write_ledger(spec_dir, [])

    await _complete(spec_dir, tmp_path, spec_id)

    control = task_control.read_control(spec_dir)
    assert control.get("status") == "human_review"
    assert control.get("reviewReason") == "errors"


@pytest.mark.asyncio
async def test_evidence_completes_and_hands_off(tmp_path: Path) -> None:
    spec_id = "095-url-safe-slug-service-python-f"
    spec_dir = _make_spec(tmp_path, spec_id)
    _write_ledger(spec_dir, ["c0ffee1"])

    emit, handoff = await _complete(spec_dir, tmp_path, spec_id)

    assert emit.call_args.kwargs.get("status") == "completed"
    assert (spec_dir / _SIDE_EFFECTS_MARKER).exists()
    handoff.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknowable_evidence_still_completes(tmp_path: Path) -> None:
    """Fail OPEN: no ledger and no worktree means unmeasured, not empty."""
    spec_id = "094-unmeasurable"
    spec_dir = _make_spec(tmp_path, spec_id)

    emit, handoff = await _complete(spec_dir, tmp_path, spec_id)

    assert emit.call_args.kwargs.get("status") == "completed"
    assert (spec_dir / _SIDE_EFFECTS_MARKER).exists()
    handoff.assert_awaited_once()
