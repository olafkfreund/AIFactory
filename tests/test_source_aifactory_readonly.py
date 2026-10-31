"""Source .aifactory grants stay read-only in agent worktrees (#1985)."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))

from security.hooks import git_dir_write_hook  # noqa: E402


def _blocked(cwd: Path, tool: str, path: str, project_dir: Path | None = None) -> bool:
    result = asyncio.run(
        git_dir_write_hook(
            {"tool_name": tool, "tool_input": {"file_path": path}, "cwd": str(cwd)},
            project_dir=str(project_dir) if project_dir else None,
        )
    )
    return result.get("decision") == "block"


@pytest.mark.parametrize("tool", ["Write", "Edit"])
def test_source_spec_is_readonly_from_worktrees(tmp_path: Path, tool: str):
    proj = tmp_path / "proj"
    source = proj / ".aifactory" / "specs" / "001"
    source.mkdir(parents=True)
    review_state = source / "review_state.json"

    for location in ("worktrees/tasks", "github/pr/worktrees"):
        worktree = proj / ".aifactory" / location / "001"
        (worktree / ".aifactory" / "specs" / "001").mkdir(parents=True)
        (worktree / "source-link.json").symlink_to(review_state)

        assert _blocked(worktree, tool, str(review_state))
        assert _blocked(worktree, tool, "source-link.json")
        if location == "worktrees/tasks":
            assert _blocked(worktree, tool, "../../../specs/001/review_state.json")
        assert not _blocked(worktree, tool, "src/a.py")
        assert not _blocked(
            worktree, tool, ".aifactory/specs/001/implementation_plan.json"
        )

    assert not _blocked(proj, tool, str(review_state))


@pytest.mark.parametrize("bound", [False, True])
def test_cwd_moved_by_cd_inside_worktree(tmp_path: Path, bound: bool):
    """The hook cwd follows `cd`; a subdir cwd must not lock the worktree (#1985)."""
    proj = tmp_path / "proj"
    review_state = proj / ".aifactory" / "specs" / "001" / "review_state.json"
    review_state.parent.mkdir(parents=True)
    worktree = proj / ".aifactory" / "worktrees" / "tasks" / "001"
    plan = worktree / ".aifactory" / "specs" / "001" / "implementation_plan.json"
    plan.parent.mkdir(parents=True)
    sub = worktree / "apps" / "web"
    sub.mkdir(parents=True)
    root = worktree if bound else None

    assert not _blocked(sub, "Write", str(worktree / "tests" / "a.py"), root)
    assert not _blocked(sub, "Write", str(plan), root)
    assert not _blocked(sub, "Edit", "../../.aifactory/specs/001/progress.txt", root)
    assert _blocked(sub, "Write", str(review_state), root)


def test_bound_project_dir_holds_when_cwd_leaves_worktree(tmp_path: Path):
    proj = tmp_path / "proj"
    review_state = proj / ".aifactory" / "specs" / "001" / "review_state.json"
    review_state.parent.mkdir(parents=True)
    worktree = proj / ".aifactory" / "worktrees" / "tasks" / "001"
    worktree.mkdir(parents=True)

    assert _blocked(proj, "Write", str(review_state), worktree)
    assert _blocked(proj, "Write", ".aifactory/specs/001/review_state.json", worktree)
    assert not _blocked(proj, "Write", str(worktree / "a.py"), worktree)


def test_create_client_grants_source_aifactory_readonly(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / "proj"
    (proj / ".aifactory").mkdir(parents=True)
    worktree = proj / ".aifactory" / "worktrees" / "tasks" / "001"
    spec_dir = worktree / ".aifactory" / "specs" / "001"
    spec_dir.mkdir(parents=True)
    captured = {}

    def capture(*, options):
        captured["options"] = options
        return MagicMock()

    with (
        patch("core.client.ClaudeSDKClient", side_effect=capture),
        patch("core.client.require_auth_token", return_value="sk-ant-oat01-test"),
        patch("core.client.HookMatcher", SimpleNamespace),
    ):
        from core.client import create_client

        create_client(
            project_dir=worktree,
            spec_dir=spec_dir,
            model="claude-sonnet-4-5-20250929",
            agent_type="coder",
        )

    (bound,) = [
        h
        for m in captured["options"].hooks["PreToolUse"]
        for h in m.hooks
        if getattr(h, "func", None) is git_dir_write_hook
    ]
    assert bound.keywords == {"project_dir": str(worktree)}
    allowed = json.loads(Path(captured["options"].settings).read_text())["permissions"][
        "allow"
    ]
    source = f"{proj / '.aifactory'}/**"
    assert f"Read({source})" in allowed
    assert f"Write({source})" not in allowed
    assert f"Edit({source})" not in allowed
