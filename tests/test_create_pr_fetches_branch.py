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
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

_WS = Path(__file__).resolve().parent.parent / "apps" / "web-server"
if str(_WS) not in sys.path:
    sys.path.insert(0, str(_WS))
_BACKEND = _WS.parent / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

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

    The route shells out to gh for `pr create` (auth is per call, #1688); the
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


# -- explicit LFS upload before the ref push (#1690) --------------------------


def _lfs_present(monkeypatch: pytest.MonkeyPatch) -> None:
    real_which = shutil.which

    def _which(cmd: str, *a: Any, **kw: Any) -> str | None:
        return "/x/git-lfs" if cmd == "git-lfs" else real_which(cmd, *a, **kw)

    monkeypatch.setattr("core.child_env.shutil.which", _which)


def _record_git(
    monkeypatch: pytest.MonkeyPatch,
    *,
    check_rc: int | None = 1,
    upload: Callable[[], subprocess.CompletedProcess[str]],
) -> list[list[str]]:
    """Fake get-url / config check / lfs push; pass everything else through."""
    real = subprocess.run
    calls: list[list[str]] = []

    def rec(argv: Any, *a: Any, **kw: Any) -> Any:
        if isinstance(argv, list):
            calls.append(argv)
            if argv[:3] == ["git", "remote", "get-url"]:
                return subprocess.CompletedProcess(
                    argv, 0, "https://github.com/acme/proj\n", ""
                )
            if "--get-regexp" in argv and check_rc is not None:
                return subprocess.CompletedProcess(argv, check_rc, "", "")
            if "lfs" in argv and argv[argv.index("lfs") + 1] == "push":
                return upload()
        return real(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", rec)
    return calls


def _is_push(c: list[str]) -> bool:
    return c[:2] == ["git", "push"]


def _is_upload(c: list[str]) -> bool:
    return "lfs" in c and c[c.index("lfs") + 1] == "push"


@pytest.mark.asyncio
async def test_lfs_upload_failure_fails_create_pr(
    packed_path_repos: dict[str, Path],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_route(monkeypatch, packed_path_repos["projects_file"])
    _lfs_present(monkeypatch)
    calls = _record_git(
        monkeypatch,
        upload=lambda: subprocess.CompletedProcess([], 2, "", "boom"),
    )

    _status, body = await _call_create_pr()

    assert body["success"] is False
    assert "Failed to push LFS objects" in body["error"]
    assert not any(_is_push(c) for c in calls)


@pytest.mark.asyncio
async def test_lfs_customtransfer_refuses_create_pr(
    packed_path_repos: dict[str, Path],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_route(monkeypatch, packed_path_repos["projects_file"])
    _lfs_present(monkeypatch)
    calls = _record_git(
        monkeypatch,
        check_rc=0,
        upload=lambda: subprocess.CompletedProcess([], 0, "", ""),
    )

    _status, body = await _call_create_pr()

    assert "LFS step refused" in body["error"]
    assert not any(_is_upload(c) for c in calls)
    assert not any(_is_push(c) for c in calls)


@pytest.mark.asyncio
async def test_lfs_runs_before_push(
    packed_path_repos: dict[str, Path],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_route(monkeypatch, packed_path_repos["projects_file"])
    _lfs_present(monkeypatch)
    calls = _record_git(
        monkeypatch,
        upload=lambda: subprocess.CompletedProcess([], 0, "", ""),
    )

    status, body = await _call_create_pr()

    assert status == 200, body.get("error")
    up = next(i for i, c in enumerate(calls) if _is_upload(c))
    push = next(i for i, c in enumerate(calls) if _is_push(c))
    assert up < push


@pytest.mark.asyncio
async def test_lfs_timeout_maps_to_push_timed_out(
    packed_path_repos: dict[str, Path],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_route(monkeypatch, packed_path_repos["projects_file"])
    _lfs_present(monkeypatch)

    def _timeout() -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("git", 60)

    calls = _record_git(monkeypatch, upload=_timeout)

    _status, body = await _call_create_pr()

    assert body["error"] == "Push timed out"
    assert not any(_is_push(c) for c in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["insteadOf", "pushInsteadOf"])
async def test_lfs_url_rewrite_refuses_create_pr(
    key: str,
    packed_path_repos: dict[str, Path],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # git-lfs applies url.*.insteadOf to the pinned lfs.url; the REAL config check
    # (not faked here) must refuse before any upload.
    _git(
        packed_path_repos["worktree"],
        "config",
        f"url.ssh://evil.invalid/.{key}",
        "https://github.com/acme/proj.git/",
    )
    _patch_route(monkeypatch, packed_path_repos["projects_file"])
    _lfs_present(monkeypatch)
    calls = _record_git(
        monkeypatch,
        check_rc=None,
        upload=lambda: subprocess.CompletedProcess([], 0, "", ""),
    )

    _status, body = await _call_create_pr()

    assert "LFS step refused" in body["error"]
    assert not any(_is_upload(c) for c in calls)
    assert not any(_is_push(c) for c in calls)
