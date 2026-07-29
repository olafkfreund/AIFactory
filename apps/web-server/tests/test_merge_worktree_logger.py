"""Regression: POST /api/tasks/{id}/worktree/merge raised HTTP 500.

merge_worktree clears internal merge-blocking files and logs each removal via
``logger`` — but, unlike its sibling handlers, never bound ``logger``. With a
merge-blocker present that path raised ``NameError: name 'logger' is not
defined`` → 500 on Approve (PR + merge). This drives the function far enough to
execute that log line and asserts it returns a dict instead of raising.
"""

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import patch


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "t@t.t"], path)
    _git(["config", "user.name", "t"], path)
    (path / "seed.txt").write_text("seed\n")
    _git(["add", "."], path)
    _git(["commit", "-m", "seed"], path)


def test_merge_worktree_does_not_nameerror_on_blocker(tmp_path):
    from server.routes import worktree_merge

    project_id, spec_id = "proj-1", "041-feat"
    project_path = tmp_path / "repo"
    _init_repo(project_path)
    # spec dir + a worktree git repo (HEAD must resolve for rev-parse).
    (project_path / ".aifactory" / "specs" / spec_id).mkdir(parents=True)
    worktree_path = project_path / ".aifactory" / "worktrees" / "tasks" / spec_id
    _init_repo(worktree_path)
    # #1073: the worktree must be on the TASK branch, not on the base branch.
    # It was on `main` -- same as the project -- and merge now refuses that
    # outright, because a worktree sitting on the base branch is the kubejob
    # case where the work lives elsewhere and merging would be a no-op merge of
    # main into itself. Without a real task branch this test no longer reaches
    # the blocker-clearing line it exists to exercise.
    _git(["checkout", "-q", "-b", f"aifactory/{spec_id}"], worktree_path)
    # A merge-blocking file → the logger.info path executes (the bug site).
    (project_path / ".aifactory-status").write_text("x")

    projects_file = tmp_path / "projects.json"
    projects_file.write_text(json.dumps({project_id: {"path": str(project_path)}}))

    with patch.object(worktree_merge, "get_projects_file", return_value=projects_file):
        result = asyncio.run(
            worktree_merge.merge_worktree(f"{project_id}:{spec_id}", _access={})
        )

    # The point: it returns a normal result dict (no NameError → no 500), and
    # the blocker was cleared on the way through.
    assert isinstance(result, dict)
    assert "success" in result
    assert not (project_path / ".aifactory-status").exists()
