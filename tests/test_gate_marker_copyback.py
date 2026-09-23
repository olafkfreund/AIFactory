"""#1550: on the co-mount Job path the gate marker comes home before anything reads it.

The Job writes ``.trailing_gates_done`` into the task worktree's spec dir; under
kubejob no generic sync copies it to the main spec dir, so the merger (#1566),
which runs inside ``run_terminal_completion``, used to find no gate evidence. The
completion path now copies it home first, reusing #1249's newer-only copier.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server.services import completion_orchestration as orch  # noqa: E402

_LOG = logging.getLogger(__name__)
_MARKER = ".trailing_gates_done"
_SPEC = "008-save-success"


def _dirs(project: Path) -> tuple[Path, Path]:
    main = project / ".aifactory" / "specs" / _SPEC
    main.mkdir(parents=True)
    (main / "implementation_plan.json").write_text(
        json.dumps({"phases": [{"name": "build", "subtasks": []}]})
    )
    memory = main / "memory"
    memory.mkdir()
    (memory / "build_commits.json").write_text(
        json.dumps({"commits": [{"hash": "d9768aa", "subtask_id": "1.1"}]})
    )
    worktree = (
        project
        / ".aifactory"
        / "worktrees"
        / "tasks"
        / _SPEC
        / ".aifactory"
        / "specs"
        / _SPEC
    )
    return main, worktree


async def _finish(main: Path, project: Path, merger: Any) -> None:
    with (
        patch("server.services.completion.emit_terminal_completion"),
        patch(
            "pfactory.tfactory_client.maybe_auto_handoff_tfactory",
            new=AsyncMock(return_value={"sent": False}),
        ),
        patch("server.services.merger.process_one", new=merger),
    ):
        await orch.run_terminal_completion(
            spec_dir=main,
            project_path=project,
            spec_id=_SPEC,
            task_id=f"proj:{_SPEC}",
            backend_path=_ROOT / "apps" / "backend",
            is_terminal=True,
            is_completed=True,
            terminal_status="completed",
            logger=_LOG,
        )


@pytest.mark.asyncio
async def test_marker_is_home_before_the_merger_reads_it(tmp_path: Path) -> None:
    main, worktree = _dirs(tmp_path)
    worktree.mkdir(parents=True)
    (worktree / _MARKER).write_text("abc\npytest: passed\n")
    seen: list[bool] = []

    def merger(*_a: Any, **_k: Any) -> dict[str, Any]:
        seen.append((main / _MARKER).is_file())
        return {"action": "opened", "pr": 1}

    await _finish(main, tmp_path, merger)

    assert seen == [True], "the merger ran before the gate marker came home"
    assert (main / _MARKER).read_text() == "abc\npytest: passed\n"


@pytest.mark.asyncio
async def test_a_newer_main_marker_is_not_overwritten(tmp_path: Path) -> None:
    main, worktree = _dirs(tmp_path)
    worktree.mkdir(parents=True)
    (worktree / _MARKER).write_text("old\npytest: failed\n")
    os.utime(worktree / _MARKER, (1_000_000, 1_000_000))
    (main / _MARKER).write_text("new\npytest: passed\n")

    await _finish(main, tmp_path, lambda *_a, **_k: {"action": "skipped"})

    assert (main / _MARKER).read_text() == "new\npytest: passed\n"


@pytest.mark.asyncio
async def test_no_worktree_is_a_no_op(tmp_path: Path) -> None:
    main, _worktree = _dirs(tmp_path)  # worktree never created

    await _finish(main, tmp_path, lambda *_a, **_k: {"action": "skipped"})

    assert not (main / _MARKER).exists()


@pytest.mark.asyncio
async def test_a_failing_copy_never_breaks_completion(tmp_path: Path) -> None:
    main, worktree = _dirs(tmp_path)
    worktree.mkdir(parents=True)
    (worktree / _MARKER).write_text("abc\npytest: passed\n")

    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError("disk gone")

    with patch.object(orch, "sync_spec_file_from_worktree", boom):
        await _finish(main, tmp_path, lambda *_a, **_k: {"action": "skipped"})

    assert not (main / _MARKER).exists()
