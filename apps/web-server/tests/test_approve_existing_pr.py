"""CFactory#457: Approve must merge a task whose PR already exists.

The cockpit's Approve is two calls: ``create-pr`` then ``merge``. It broke on
two normal situations:

- the PR was already open: ``gh pr create`` failed with "already exists", so
  the merge step never ran;
- the task worktree had been cleaned up: both handlers refused up front with
  "No worktree found", although ``merge`` can merge the PR by branch name
  (#1076) and branch resolution does not need a worktree (#1073).

``gh`` and ``git`` are faked at their call sites, so these tests pin behaviour,
not helper names.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.responses import JSONResponse
from server.routes import pr, worktree_merge

PROJECT_ID = "proj"
SPEC_ID = "001-task"
TASK_ID = f"{PROJECT_ID}:{SPEC_ID}"
BRANCH = "aifactory/001-task"


class FakeGh:
    """Answers ``gh pr list`` from a configured PR and records every call."""

    def __init__(self, pr_found: dict[str, Any] | None, merge_ok: bool = True):
        self.pr_found = pr_found
        self.merge_ok = merge_ok
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **_: Any) -> dict[str, Any]:
        self.calls.append(list(args))
        if args[:2] == ["pr", "list"]:
            return {
                "success": True,
                "output": json.dumps([self.pr_found] if self.pr_found else []),
            }
        if args[:2] == ["pr", "view"]:
            url = (self.pr_found or {}).get("url", "")
            return {"success": True, "output": json.dumps({"url": url})}
        if args[:2] == ["pr", "merge"]:
            return {
                "success": self.merge_ok,
                "output": "",
                "error": "" if self.merge_ok else "refused",
            }
        if args[:2] == ["pr", "create"]:
            return {
                "success": False,
                "error": "a pull request for branch already exists",
            }
        return {"success": True, "output": ""}

    def did(self, *prefix: str) -> bool:
        return any(c[: len(prefix)] == list(prefix) for c in self.calls)


class FakeGit:
    """Every subprocess call succeeds; records the argv."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: list[str], *_: Any, **__: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        out = "main\n" if argv[:3] == ["git", "rev-parse", "--abbrev-ref"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    def pushed(self) -> bool:
        return any(c[:2] == ["git", "push"] for c in self.calls)


def _project(tmp_path: Path, *, worktree: bool) -> tuple[Path, Path]:
    project_path = tmp_path / "repo"
    (project_path / ".aifactory" / "specs" / SPEC_ID).mkdir(parents=True)
    if worktree:
        (project_path / ".aifactory" / "worktrees" / "tasks" / SPEC_ID).mkdir(
            parents=True
        )
    projects_file = tmp_path / "projects.json"
    projects_file.write_text(json.dumps({PROJECT_ID: {"path": str(project_path)}}))
    return project_path, projects_file


def _body(result: Any) -> dict[str, Any]:
    if isinstance(result, JSONResponse):
        parsed: dict[str, Any] = json.loads(bytes(result.body))
        return parsed
    assert isinstance(result, dict)
    return result


def _create_pr(tmp_path: Path, gh: FakeGh, git: FakeGit, *, worktree: bool) -> Any:
    project_path, projects_file = _project(tmp_path, worktree=worktree)
    repo_dir = (
        (project_path / ".aifactory" / "worktrees" / "tasks" / SPEC_ID)
        if worktree
        else None
    )
    with (
        patch.object(pr, "get_projects_file", return_value=projects_file),
        patch.object(pr, "task_repo_dir", return_value=repo_dir),
        patch.object(pr, "resolve_task_branch", return_value=(BRANCH, None)),
        patch("server.routes.github.run_gh_command", gh),
        patch("server.services.approval.run_gh_command", gh),
        patch("server.routes.github._use_provider_api", return_value=False),
        patch("subprocess.run", git),
    ):
        return asyncio.run(pr.create_pr_from_task(TASK_ID, _access={}))


def _merge(tmp_path: Path, gh: FakeGh, git: FakeGit, *, worktree: bool) -> Any:
    _, projects_file = _project(tmp_path, worktree=worktree)
    with (
        patch.object(worktree_merge, "get_projects_file", return_value=projects_file),
        patch.object(
            worktree_merge, "resolve_task_branch", return_value=(BRANCH, None)
        ),
        patch("server.services.approval.run_gh_command", gh),
        patch("subprocess.run", git),
    ):
        return asyncio.run(worktree_merge.merge_worktree(TASK_ID, _access={}))


OPEN_PR = {"number": 50, "state": "OPEN", "url": "https://github.com/o/r/pull/50"}
MERGED_PR = {"number": 50, "state": "MERGED", "url": "https://github.com/o/r/pull/50"}


# --- create-pr ---------------------------------------------------------------


def test_create_pr_returns_the_existing_open_pr(tmp_path: Path) -> None:
    gh, git = FakeGh(OPEN_PR), FakeGit()
    body = _body(_create_pr(tmp_path, gh, git, worktree=True))
    assert body["success"] is True
    assert body["data"]["prNumber"] == 50
    assert body["data"]["existing"] is True
    assert not gh.did("pr", "create")
    assert not git.pushed()


def test_create_pr_returns_an_already_merged_pr(tmp_path: Path) -> None:
    body = _body(_create_pr(tmp_path, FakeGh(MERGED_PR), FakeGit(), worktree=True))
    assert body["success"] is True
    assert body["data"]["prNumber"] == 50


def test_create_pr_without_worktree_returns_the_existing_pr(tmp_path: Path) -> None:
    body = _body(_create_pr(tmp_path, FakeGh(OPEN_PR), FakeGit(), worktree=False))
    assert body["success"] is True
    assert body["data"]["prNumber"] == 50


def test_create_pr_without_worktree_and_no_pr_still_refuses(tmp_path: Path) -> None:
    body = _body(_create_pr(tmp_path, FakeGh(None), FakeGit(), worktree=False))
    assert body["success"] is False
    assert "No worktree found" in body["error"]


# --- merge -------------------------------------------------------------------


def test_merge_without_worktree_merges_the_open_pr(tmp_path: Path) -> None:
    gh = FakeGh(OPEN_PR)
    body = _body(_merge(tmp_path, gh, FakeGit(), worktree=False))
    assert body["success"] is True
    assert gh.did("pr", "merge")


def test_merge_without_worktree_and_no_pr_still_refuses(tmp_path: Path) -> None:
    body = _body(_merge(tmp_path, FakeGh(None), FakeGit(), worktree=False))
    assert body["success"] is False
    assert "No worktree found" in body["error"]


def test_merge_refused_by_github_is_not_a_200(tmp_path: Path) -> None:
    # With a worktree, so the refusal comes from GitHub, not from the guard.
    result = _merge(tmp_path, FakeGh(OPEN_PR, merge_ok=False), FakeGit(), worktree=True)
    assert isinstance(result, JSONResponse)
    assert result.status_code != 200
    body = _body(result)
    assert body["success"] is False
    assert "could not merge pull request #50" in body["error"]
