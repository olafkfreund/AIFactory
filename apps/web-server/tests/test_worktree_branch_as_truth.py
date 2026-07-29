"""The review surface must read the pushed task branch, not the worktree HEAD (#1082).

Under ``AIFACTORY_BUILD_BACKEND=kubejob`` the task worktree is a standalone
clone left on the BASE branch; the work escapes the build Job by ``git push``.
Endpoints that read the worktree's HEAD as if it were the task branch end up
diffing ``base...base``: the reviewer's diff view is empty, status reports zero
commits, and merge-preview says "nothing to merge, can merge".

The fixture builds that exact shape with real git repositories:

- an origin repo holding ``main``
- the control plane's project checkout: a CLONE of origin, sitting on ``main``
- the task worktree: a clone of the project, also on ``main``
- the build Job: a separate clone that commits the work on
  ``aifactory/<spec>`` and pushes it to origin AFTER the project clone was
  made, so the project's refs do NOT contain the branch until something
  fetches it (that is the ``task_branch._exists`` ordering defect folded into
  the same fix)

``git worktree add`` cannot reproduce this: it SHARES refs with the project
repo, which hides both bugs.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_WEB_SERVER = Path(__file__).resolve().parents[1]
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

from server.routes import worktree_merge  # noqa: E402
from server.services import conflict_service, pr_endgame, task_branch  # noqa: E402

SPEC_ID = "097-add-feature"
TASK_BRANCH = f"aifactory/{SPEC_ID}"
PROJECT_ID = "proj-1"


def _git(args: list[str], cwd: Path) -> None:
    # S603/S607: fixed literals in a test fixture, no shell, no external input.
    subprocess.run(  # noqa: S603
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True  # noqa: S607
    )


def _configure(repo: Path) -> None:
    _git(["config", "user.email", "t@t.t"], repo)
    _git(["config", "user.name", "t"], repo)


@pytest.fixture
def kubejob_shape(tmp_path: Path) -> dict[str, Path]:
    """Origin + project-clone-on-base + worktree-on-base + branch pushed late."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(["init", "--bare", "-q", "-b", "main"], origin)

    # Seed origin's main.
    seed = tmp_path / "seed"
    _git(["clone", "-q", str(origin), str(seed)], tmp_path)
    _configure(seed)
    (seed / "app.py").write_text("print('v1')\n")
    _git(["add", "."], seed)
    _git(["commit", "-qm", "seed"], seed)
    _git(["push", "-q", "origin", "main"], seed)

    # The control plane's project checkout: clone of origin, on main.
    project = tmp_path / "project"
    _git(["clone", "-q", str(origin), str(project)], tmp_path)
    _configure(project)
    (project / ".aifactory" / "specs" / SPEC_ID).mkdir(parents=True)

    # The task worktree: standalone clone of the project, left on main.
    worktree = project / ".aifactory" / "worktrees" / "tasks" / SPEC_ID
    worktree.parent.mkdir(parents=True)
    _git(["clone", "-q", str(project), str(worktree)], project)
    _configure(worktree)

    # The build Job: separate clone, does the work, pushes the branch to
    # origin AFTER the project clone exists — so the project repo has no
    # origin/<branch> ref until something fetches.
    job = tmp_path / "job"
    _git(["clone", "-q", str(origin), str(job)], tmp_path)
    _configure(job)
    _git(["checkout", "-q", "-b", TASK_BRANCH], job)
    (job / "app.py").write_text("print('v2')\n")
    (job / "feature.py").write_text("def feature():\n    return 42\n")
    _git(["add", "."], job)
    _git(["commit", "-qm", "the work"], job)
    _git(["push", "-q", "origin", TASK_BRANCH], job)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "projects.json").write_text(
        json.dumps({PROJECT_ID: {"path": str(project)}})
    )
    return {"project": project, "worktree": worktree, "data_dir": data_dir}


class _StubConflictService:
    async def detect_conflicts(self, **_kw):
        return {"success": True, "conflicts": [], "stats": {}}

    async def ai_merge_three_way(self, **_kw):
        return {"success": True, "content": "merged\n"}


def test_diff_shows_the_pushed_work(kubejob_shape: dict[str, Path]) -> None:
    """THE reviewer surface: /worktree/diff must show the pushed branch's diff."""
    with patch.object(
        worktree_merge, "get_data_dir", return_value=kubejob_shape["data_dir"]
    ):
        result = asyncio.run(
            worktree_merge.get_worktree_diff(f"{PROJECT_ID}:{SPEC_ID}", _access={})
        )

    assert result["success"], result
    paths = {f["path"] for f in result["data"]["files"]}
    assert "feature.py" in paths, (
        f"empty/wrong diff: reviewer sees {sorted(paths)!r} — the worktree HEAD "
        f"(base) was diffed instead of the pushed task branch"
    )
    assert "app.py" in paths
    app_diff = next(f for f in result["data"]["files"] if f["path"] == "app.py")
    assert "+print('v2')" in app_diff["diff"]


def test_status_reports_the_pushed_work(kubejob_shape: dict[str, Path]) -> None:
    with patch.object(
        worktree_merge, "get_data_dir", return_value=kubejob_shape["data_dir"]
    ):
        result = asyncio.run(
            worktree_merge.get_worktree_status(f"{PROJECT_ID}:{SPEC_ID}", _access={})
        )

    assert result["success"], result
    data = result["data"]
    assert data["exists"] is True
    assert data["branch"] == TASK_BRANCH, (
        f"branch reported as {data['branch']!r} — the worktree HEAD, not the "
        f"task branch"
    )
    assert data["commitCount"] >= 1
    assert data["filesChanged"] >= 2


def test_merge_preview_shows_the_pushed_work(kubejob_shape: dict[str, Path]) -> None:
    with (
        patch.object(
            worktree_merge, "get_data_dir", return_value=kubejob_shape["data_dir"]
        ),
        patch.object(
            conflict_service,
            "get_conflict_service",
            return_value=_StubConflictService(),
        ),
    ):
        # merge-preview takes the bare spec id: it searches projects for the
        # spec dir rather than splitting a project:spec pair.
        result = asyncio.run(
            worktree_merge.get_worktree_merge_preview(SPEC_ID, _access={})
        )

    assert result["success"], result
    data = result["data"]
    changed = {f["path"] for f in data["changedFiles"]}
    assert "feature.py" in changed, (
        f"merge-preview saw {sorted(changed)!r} changed files — an empty "
        f"preview approves nothing"
    )
    assert data["gitConflicts"]["commitsAhead"] >= 1
    assert data["gitConflicts"]["specBranch"] == TASK_BRANCH


def test_resolve_conflicts_merges_the_pushed_work(
    kubejob_shape: dict[str, Path],
) -> None:
    """Pre-fix this merged base into base and reported a clean merge of nothing."""
    project = kubejob_shape["project"]
    projects_file = kubejob_shape["data_dir"] / "projects.json"

    with patch.object(
        worktree_merge, "get_projects_file", return_value=projects_file
    ):
        result = asyncio.run(
            worktree_merge.resolve_worktree_conflicts(
                f"{PROJECT_ID}:{SPEC_ID}", _access={}
            )
        )

    assert result["success"], result
    assert (project / "feature.py").exists(), (
        "reported success but the task's work never reached the project — "
        "the merge ran base-into-base"
    )


def test_resolve_uncommitted_sees_the_task_files(
    kubejob_shape: dict[str, Path],
) -> None:
    """Pre-fix task_files came from base...base (empty), so no conflict was seen."""
    project = kubejob_shape["project"]
    # An uncommitted local edit to a file the task also changed.
    (project / "app.py").write_text("print('local uncommitted')\n")

    with (
        patch.object(
            worktree_merge, "get_data_dir", return_value=kubejob_shape["data_dir"]
        ),
        patch.object(
            conflict_service,
            "get_conflict_service",
            return_value=_StubConflictService(),
        ),
    ):
        # resolve-uncommitted takes the bare spec id (same search as preview).
        result = asyncio.run(
            worktree_merge.resolve_uncommitted_conflicts(SPEC_ID, _access={})
        )

    assert result["success"], result
    assert "app.py" in result["data"].get("resolved", []), (
        f"got {result['data']!r} — the overlap with the task branch was "
        f"computed against base...base and came out empty"
    )


def test_resolve_work_ref_fetches_before_existence_check(
    kubejob_shape: dict[str, Path],
) -> None:
    """task_branch._exists only sees already-fetched refs; the helper must fetch.

    The marker records the branch, the branch exists on origin, but the project
    repo has never fetched it. Resolution must still succeed.
    """
    project = kubejob_shape["project"]
    task_branch.record_branch(project, SPEC_ID, TASK_BRANCH)

    branch, ref, reason = task_branch.resolve_work_ref(
        worktree_path=kubejob_shape["worktree"],
        project_path=project,
        spec_id=SPEC_ID,
        base_branch="main",
    )
    assert branch == TASK_BRANCH, reason
    assert ref is not None
    # The ref must be resolvable in the project repo.
    out = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "--verify", "--quiet", ref],  # noqa: S607
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, f"{ref!r} does not resolve in the project repo"


def test_gather_pr_context_uses_the_resolver_not_the_convention(
    kubejob_shape: dict[str, Path],
) -> None:
    """pr_endgame hardcoded aifactory/<spec>; a differently-named branch was lost."""
    project = kubejob_shape["project"]
    # The real branch uses a NON-convention name; fetch it into the project so
    # discovery can see it (gather_pr_context itself must stay network-free in
    # the fast path).
    _git(["fetch", "-q", "origin", f"{TASK_BRANCH}:wip/{SPEC_ID}"], project)
    # Drop the convention-named branch from origin so the hardcoded guess is
    # provably wrong rather than accidentally right.
    _git(["push", "-q", "origin", "--delete", TASK_BRANCH], project)

    spec_dir = project / ".aifactory" / "specs" / SPEC_ID
    (spec_dir / "requirements.json").write_text(
        json.dumps({"github_repo": "owner/repo"})
    )

    ctx = pr_endgame.gather_pr_context(
        spec_dir=spec_dir,
        spec_id=SPEC_ID,
        project_path=project,
    )
    assert ctx is not None
    assert ctx["branch"] == f"wip/{SPEC_ID}", (
        f"got {ctx['branch']!r} — the aifactory/<spec> convention was "
        f"hardcoded instead of resolving the real branch"
    )
