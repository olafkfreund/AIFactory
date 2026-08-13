"""Regression test for issue #926.

The git project handlers (squash / worktree / release) read
``get_settings().projects_file`` — an attribute ``Settings`` does not define —
so every call raised ``AttributeError`` (swallowed by a broad ``except``) and
returned ``{"success": false, "error": "Failed to load project: 'Settings'
object has no attribute 'projects_file'"}``. The routers ARE mounted (via
projects.py), so the endpoints were live but 100% broken.

Fix: resolve the project path through ``resolve_project_path`` — the same
single source of truth the sibling project routes use.
"""

import asyncio
import subprocess

import pytest
# Registry helpers live in the leaf server/project_store.py (#1302); patch the
# owner so the patch reaches routes/git.py, which imports from the owner.
from server import project_store as projects_mod
from server.routes.git import CreateWorktreeRequest, create_worktree


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_create_worktree_resolves_project(tmp_path, monkeypatch):
    """A previously-broken handler now resolves a known project and proceeds."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")

    monkeypatch.setattr(
        projects_mod, "load_projects", lambda: {"p1": {"path": str(repo)}}
    )

    result = asyncio.run(create_worktree("p1", CreateWorktreeRequest(name="t1")))

    assert result["success"] is True, result
    assert result["worktreePath"].endswith(".aifactory/worktrees/tasks/t1")
    # Regression guard: must never resurface the old settings.projects_file error.
    assert "projects_file" not in str(result)


def test_unknown_project_still_404s(monkeypatch):
    """resolve_project_path preserves the prior 404-on-missing behaviour."""
    from fastapi import HTTPException

    monkeypatch.setattr(projects_mod, "load_projects", lambda: {})

    with pytest.raises(HTTPException) as exc:
        asyncio.run(create_worktree("nope", CreateWorktreeRequest(name="t1")))
    assert exc.value.status_code == 404
