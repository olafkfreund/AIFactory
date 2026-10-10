"""A completed build is landed by the merger, not only by the PR endgame (Factory#2586).

The endgame opens a PR only on a clean, QA-approved build, and on the co-mount
path nothing else pushes the branch. Five live tasks sat for a week with 2-10
commits that existed only in a worktree. These tests pin the WIRING: the
completion path must call ``merger.process_one`` -- a helper that is correct
but never called fixes nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
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


@pytest.mark.asyncio
async def test_fix_push_lfs_failure_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1690: the PR-fix push uploads LFS objects first; a failed upload fails the fix."""
    from server.services import pr_endgame

    spec_dir = _make_spec(tmp_path, "011-lfs-fix", commits=["a1"])
    wt = tmp_path / "wt"
    wt.mkdir()
    end = AsyncMock()
    ctx = {"worktree": str(wt), "branch": "b", "base": "main", "repo": "o/r"}
    with (
        patch.object(pr_endgame, "is_auto_pr_enabled", return_value=True),
        patch.object(pr_endgame, "gather_pr_context", return_value=ctx),
        patch.object(pr_endgame, "resolve_pr_reviewer", return_value="aifactory"),
        patch.object(pr_endgame, "run_pr_endgame", end),
    ):
        await _finish(
            spec_dir,
            tmp_path,
            completed=True,
            merger=lambda *_a, **_k: {},
        )
    fix_fn = end.call_args.kwargs["fix_fn"]

    real_which = shutil.which
    monkeypatch.setattr(
        "core.child_env.shutil.which",
        lambda c, *a, **k: "/x/git-lfs" if c == "git-lfs" else real_which(c, *a, **k),
    )
    calls: list[list[str]] = []

    def rec(argv: list[str], *_a: Any, **_k: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(argv, 0, "https://github.com/o/r\n", "")
        if "--get-regexp" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "")
        if "lfs" in argv and argv[argv.index("lfs") + 1] == "push":
            return subprocess.CompletedProcess(argv, 2, "", "boom")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", rec)
    with patch("qa.correction.apply_correction", new=AsyncMock()):
        # fix_fn uses asyncio.run internally, so it needs a thread without a loop.
        result = await asyncio.to_thread(fix_fn, [])

    assert result is False
    assert any("lfs" in c and c[c.index("lfs") + 1] == "push" for c in calls)
    assert not any(c[:2] == ["git", "push"] for c in calls)
