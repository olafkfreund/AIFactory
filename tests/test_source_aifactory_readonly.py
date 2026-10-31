"""Source .aifactory grants stay read-only in agent worktrees (#1985)."""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))

from security.hooks import git_dir_write_hook  # noqa: E402


def _blocked(cwd: Path, tool: str, path: str) -> bool:
    result = asyncio.run(
        git_dir_write_hook(
            {"tool_name": tool, "tool_input": {"file_path": path}, "cwd": str(cwd)}
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
    ):
        from core.client import create_client

        create_client(
            project_dir=worktree,
            spec_dir=spec_dir,
            model="claude-sonnet-4-5-20250929",
            agent_type="coder",
        )

    allowed = json.loads(Path(captured["options"].settings).read_text())["permissions"][
        "allow"
    ]
    source = f"{proj / '.aifactory'}/**"
    assert f"Read({source})" in allowed
    assert f"Write({source})" not in allowed
    assert f"Edit({source})" not in allowed
