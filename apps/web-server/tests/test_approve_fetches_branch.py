"""CFactory#457 (amendment): Approve must fetch the task branch before resolving it.

Under the kubejob build backend the build pod pushes ``aifactory/<spec>`` to
origin, and the control-plane checkout was cloned BEFORE that push, so it has
never seen the ref. ``resolve_task_branch`` only sees refs that are already
fetched, so create-pr and merge both refused with "Could not determine task
branch" before reaching the existing-PR / PR-merge paths. Seen on prod
2026-09-23 on task ``020-e2e-457-approve-probe``.

Real git (bare origin, a pod clone that pushes, a project cloned before the
push); only ``gh`` is faked.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.responses import JSONResponse
from server.routes import pr, worktree_merge

PROJECT_ID = "proj"
SPEC_ID = "020-probe"
TASK_ID = f"{PROJECT_ID}:{SPEC_ID}"
TASK_BRANCH = f"aifactory/{SPEC_ID}"
BASE = "main"
OPEN_PR = {"number": 7, "state": "OPEN", "url": "https://github.com/acme/proj/pull/7"}


def _git(cwd: Path, *args: str) -> str:
    # Fixed test-only argv against temp repos; no untrusted input.
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture()
def repos(tmp_path: Path) -> dict[str, Path]:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch", BASE)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--initial-branch", BASE)
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "user.name", "t")
    (seed / "README.md").write_text("seed\n")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-u", "origin", BASE)

    # The control plane cloned BEFORE the build pushed: no task ref at all.
    project = tmp_path / "project"
    _git(tmp_path, "clone", str(origin), str(project))

    pod = tmp_path / "pod"
    _git(tmp_path, "clone", str(origin), str(pod))
    _git(pod, "config", "user.email", "t@t")
    _git(pod, "config", "user.name", "t")
    _git(pod, "checkout", "-b", TASK_BRANCH)
    (pod / "e2e.md").write_text("probe\n")
    _git(pod, "add", "e2e.md")
    _git(pod, "commit", "-m", "probe")
    _git(pod, "push", "-u", "origin", TASK_BRANCH)

    refs = _git(project, "for-each-ref", "--format=%(refname:short)")
    assert SPEC_ID not in refs  # the precondition prod had

    (project / ".aifactory" / "specs" / SPEC_ID).mkdir(parents=True)
    projects_file = tmp_path / "projects.json"
    projects_file.write_text(json.dumps({PROJECT_ID: {"path": str(project)}}))
    return {"project": project, "projects_file": projects_file}


class FakeGh:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **_: Any) -> dict[str, Any]:
        self.calls.append(list(args))
        if args[:2] == ["pr", "list"]:
            return {"success": True, "output": json.dumps([OPEN_PR])}
        return {"success": True, "output": ""}


def _body(result: Any) -> dict[str, Any]:
    if isinstance(result, JSONResponse):
        parsed: dict[str, Any] = json.loads(bytes(result.body))
        return parsed
    assert isinstance(result, dict)
    return result


def test_merge_resolves_a_branch_only_on_origin(repos: dict[str, Path]) -> None:
    gh = FakeGh()
    with (
        patch.object(
            worktree_merge, "get_projects_file", return_value=repos["projects_file"]
        ),
        patch("server.services.approval.run_gh_command", gh),
    ):
        body = _body(asyncio.run(worktree_merge.merge_worktree(TASK_ID, _access={})))
    assert body["success"] is True, body
    assert ["pr", "merge", "7", "--squash"] in gh.calls


def test_create_pr_resolves_a_branch_only_on_origin(repos: dict[str, Path]) -> None:
    gh = FakeGh()
    with (
        patch.object(pr, "get_projects_file", return_value=repos["projects_file"]),
        patch.object(pr, "task_repo_dir", return_value=None),
        patch("server.services.approval.run_gh_command", gh),
        patch("server.routes.github.run_gh_command", gh),
        patch("server.routes.github._use_provider_api", return_value=False),
    ):
        body = _body(asyncio.run(pr.create_pr_from_task(TASK_ID, _access={})))
    assert body["success"] is True, body
    assert body["data"]["prNumber"] == 7
    assert body["data"]["branch"] == TASK_BRANCH
