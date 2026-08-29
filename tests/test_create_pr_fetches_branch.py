"""create-PR must fetch the task branch before pushing it (#1459).

This is #959 / PR #962 on the other door. Under the kubejob build backend the
build runs in a separate pod and pushes its branch straight to origin; the
control-plane worktree this route pushes from never had that branch, so
``git push -u origin <branch>`` dies with ``src refspec <branch> does not match
any`` and no PR opens -- while the task still reports completion.

The test builds REAL git repositories, so the refspec failure is git's own and
not a mock's opinion:

    origin (bare)  <- the "pod" clone pushes aifactory/<spec> here
    project        <- the control plane's checkout, on the base branch
    project/.aifactory/worktrees/tasks/<spec>  <- has NO local task branch

Removing the fetch guard from ``routes/pr.py`` must make this fail with that
exact git error. A test that only asserts a 200 on the co-mount path (where the
branch is already local) passes with or without the guard and proves nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_WS = Path(__file__).resolve().parent.parent / "apps" / "web-server"
if str(_WS) not in sys.path:
    sys.path.insert(0, str(_WS))

SPEC_ID = "039-roman-numeral-conversion-utili"
PROJECT_ID = "502baac8-4816-42bb-bf2d-c54a38087302"
BASE_BRANCH = "dev"
TASK_BRANCH = f"aifactory/{SPEC_ID}"

_GIT_ID = [
    "-c",
    "user.email=t@example.com",
    "-c",
    "user.name=Test",
    "-c",
    "commit.gpgsign=false",
]


def _git(cwd: Path, *args: str) -> str:
    """Run git in *cwd*, raising with git's own message on failure."""
    proc = subprocess.run(
        ["git", *_GIT_ID, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture()
def packed_path_repos(tmp_path: Path) -> dict[str, Path]:
    """Build the kubejob/packed-path topology described in the module docstring."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch", BASE_BRANCH)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--initial-branch", BASE_BRANCH)
    (seed / "README.md").write_text("seed\n")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-u", "origin", BASE_BRANCH)

    # The build pod: pushes the task branch to origin and then evaporates.
    pod = tmp_path / "pod"
    _git(tmp_path, "clone", str(origin), str(pod))
    _git(pod, "checkout", "-b", TASK_BRANCH)
    (pod / "feature.py").write_text("def roman(n: int) -> str:\n    return 'I' * n\n")
    _git(pod, "add", "feature.py")
    _git(pod, "commit", "-m", "feat: roman numerals")
    _git(pod, "push", "-u", "origin", TASK_BRANCH)

    project = tmp_path / "project"
    _git(tmp_path, "clone", str(origin), str(project))

    # The control-plane worktree: a clone that has never seen the task branch.
    worktree = project / ".aifactory" / "worktrees" / "tasks" / SPEC_ID
    worktree.parent.mkdir(parents=True)
    _git(tmp_path, "clone", str(origin), str(worktree))
    assert TASK_BRANCH not in _git(worktree, "branch", "--list", TASK_BRANCH)

    spec_dir = project / ".aifactory" / "specs" / SPEC_ID
    spec_dir.mkdir(parents=True)
    (spec_dir / "requirements.json").write_text(
        json.dumps({"title": "Roman numerals", "description": "convert ints"})
    )

    projects_file = tmp_path / "projects.json"
    projects_file.write_text(
        json.dumps({PROJECT_ID: {"path": str(project), "name": "proj"}})
    )
    return {
        "origin": origin,
        "project": project,
        "worktree": worktree,
        "projects_file": projects_file,
    }


@pytest.fixture()
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `gh` on PATH that succeeds and prints a PR URL for `gh pr create`.

    The route shells out to gh for `auth setup-git` and for `pr create`; the
    real CLI would need credentials and a real remote. Everything the test
    actually asserts on -- the fetch and the push -- stays real git.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "pr" ] && [ "$2" = "create" ]; then\n'
        '  echo "https://github.com/acme/proj/pull/7"\n'
        "fi\n"
        "exit 0\n"
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return gh


def _patch_route(monkeypatch: pytest.MonkeyPatch, projects_file: Path) -> None:
    """Point the route at the temp projects file and off the provider API."""
    from server.routes import github as github_routes
    from server.routes import pr as pr_routes

    monkeypatch.setattr(pr_routes, "get_projects_file", lambda: projects_file)
    monkeypatch.setattr(github_routes, "_use_provider_api", lambda _pid: False)


async def _call_create_pr() -> tuple[int, dict[str, Any]]:
    """Invoke the route handler in-process; return (status, body)."""
    from fastapi.responses import JSONResponse
    from server.routes import pr as pr_routes

    result = await pr_routes.create_pr_from_task(
        f"{PROJECT_ID}:{SPEC_ID}",
        pr_routes.CreatePRFromTaskOptions(),
        _access={},
    )
    if isinstance(result, JSONResponse):
        body: dict[str, Any] = json.loads(bytes(result.body))
        return result.status_code, body
    assert isinstance(result, dict)
    return 200, result


@pytest.mark.asyncio
async def test_create_pr_pushes_a_branch_the_worktree_never_had(
    packed_path_repos: dict[str, Path],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MCP create-PR route must open the PR on the packed path.

    Without the `git fetch origin <branch>:<branch>` guard this returns 409
    with git's "src refspec ... does not match any".
    """
    _patch_route(monkeypatch, packed_path_repos["projects_file"])

    status, body = await _call_create_pr()

    assert status == 200, body.get("error")
    assert body["success"] is True, body.get("error")
    assert body["data"]["branch"] == TASK_BRANCH
    assert body["data"]["prNumber"] == 7
    # The guard's whole job: the branch now resolves locally in the worktree.
    assert _git(
        packed_path_repos["worktree"], "rev-parse", "--verify", TASK_BRANCH
    ) == _git(packed_path_repos["origin"], "rev-parse", TASK_BRANCH)


@pytest.mark.asyncio
async def test_fetch_guard_is_a_no_op_when_the_branch_is_already_checked_out(
    packed_path_repos: dict[str, Path],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-safe: on the co-mount path git refuses the fetch and we continue.

    `git fetch origin b:b` cannot write a branch that is checked out here, so
    the guard must swallow that refusal rather than turn it into an error.
    """
    worktree = packed_path_repos["worktree"]
    _git(worktree, "fetch", "origin", TASK_BRANCH)
    _git(worktree, "checkout", "-B", TASK_BRANCH, "FETCH_HEAD")

    _patch_route(monkeypatch, packed_path_repos["projects_file"])

    status, body = await _call_create_pr()

    assert status == 200, body.get("error")
    assert body["success"] is True, body.get("error")
    assert body["data"]["branch"] == TASK_BRANCH
