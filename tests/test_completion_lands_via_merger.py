"""A completed build is landed by the merger, not only by the PR endgame (Factory#2586).

The endgame opens a PR only on a clean, QA-approved build, and on the co-mount
path nothing else pushes the branch. Five live tasks sat for a week with 2-10
commits that existed only in a worktree. These tests pin the WIRING: the
completion path must call ``merger.process_one`` -- a helper that is correct
but never called fixes nothing.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server.services.completion_orchestration import (  # noqa: E402
    run_terminal_completion,
)

_LOG = logging.getLogger(__name__)

_EMIT_TARGET = "server.services.completion.emit_terminal_completion"
_HANDOFF_TARGET = "pfactory.tfactory_client.maybe_auto_handoff_tfactory"
_MERGER_TARGET = "server.services.merger.process_one"


def _make_spec(project_path: Path, spec_id: str, *, commits: list[str]) -> Path:
    spec_dir = project_path / ".aifactory" / "specs" / spec_id
    spec_dir.mkdir(parents=True)
    (spec_dir / "implementation_plan.json").write_text(
        json.dumps({"phases": [{"name": "build", "subtasks": []}]})
    )
    memory = spec_dir / "memory"
    memory.mkdir()
    (memory / "build_commits.json").write_text(
        json.dumps({"commits": [{"hash": h, "subtask_id": "1.1"} for h in commits]})
    )
    return spec_dir


async def _finish(
    spec_dir: Path,
    project_path: Path,
    *,
    completed: bool,
    merger: Any,
) -> str:
    with (
        patch(_EMIT_TARGET),
        patch(_HANDOFF_TARGET, new=AsyncMock(return_value={"sent": False})),
        patch(_MERGER_TARGET, new=merger),
    ):
        status: str = await run_terminal_completion(
            spec_dir=spec_dir,
            project_path=project_path,
            spec_id=spec_dir.name,
            task_id=f"proj:{spec_dir.name}",
            backend_path=_ROOT / "apps" / "backend",
            is_terminal=True,
            is_completed=completed,
            terminal_status="completed" if completed else "failed",
            logger=_LOG,
        )
    return status


@pytest.mark.asyncio
async def test_completed_build_is_landed_by_the_merger(tmp_path: Path) -> None:
    spec_dir = _make_spec(tmp_path, "008-save-success", commits=["d9768aa"])
    calls: list[tuple[Any, ...]] = []

    def merger(*args: Any, **_kwargs: Any) -> dict[str, Any]:
        calls.append(args)
        return {"action": "opened", "pr": 51}

    await _finish(spec_dir, tmp_path, completed=True, merger=merger)

    assert calls == [("proj", tmp_path, spec_dir)]


@pytest.mark.asyncio
async def test_failed_build_is_left_to_the_sweep(tmp_path: Path) -> None:
    """A broken build does not open a PR the moment it fails."""
    spec_dir = _make_spec(tmp_path, "009-failed", commits=["abc1234"])
    calls: list[tuple[Any, ...]] = []

    def merger(*args: Any, **_kwargs: Any) -> dict[str, Any]:
        calls.append(args)
        return {}

    await _finish(spec_dir, tmp_path, completed=False, merger=merger)

    assert calls == []


@pytest.mark.asyncio
async def test_build_that_wrote_nothing_is_not_landed(tmp_path: Path) -> None:
    """The #1070 evidence gate downgrades it to failed before the merger runs."""
    spec_dir = _make_spec(tmp_path, "019-nothing", commits=[])
    calls: list[tuple[Any, ...]] = []

    def merger(*args: Any, **_kwargs: Any) -> dict[str, Any]:
        calls.append(args)
        return {}

    status = await _finish(spec_dir, tmp_path, completed=True, merger=merger)

    assert status == "failed"
    assert calls == []


@pytest.mark.asyncio
async def test_a_raising_merger_never_changes_the_outcome(tmp_path: Path) -> None:
    spec_dir = _make_spec(tmp_path, "010-boom", commits=["807a40e"])

    def merger(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("gh exploded")

    status = await _finish(spec_dir, tmp_path, completed=True, merger=merger)

    assert status == "completed"
