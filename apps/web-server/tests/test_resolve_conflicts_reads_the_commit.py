"""A failed commit must not be reported as a successful merge (#1210).

`resolve_worktree_conflicts` ends its no-conflicts path by running
``git commit --no-edit`` and used to DISCARD the result: a hook rejection, a
stale ``index.lock`` or an unwritable object store all reached the caller as
``success: true`` while the merge sat uncommitted. Same shape as the gate
defects that ran through this repo all week — a result read as clean because
nobody looked at the exit code.

The fix cannot be "non-zero means failure", and that is the whole reason this
file exists. ``git commit`` exits 1 when there is nothing to commit, and that is
the EXPECTED state on this path: we only get here because no conflicted files
were found, so git has very often already committed the merge itself. Reporting
that as an error would turn the ordinary success case red — the mirror image of
the bug, and no better.

So both directions are asserted: a real failure must surface, and the benign
"nothing to commit" must stay green.
"""

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.responses import JSONResponse
from server.routes import worktree_merge
from server.services.http_verdict import REFUSED_STATUS


def _git(args: list[str], cwd: Path) -> None:
    # Suppressed the same way test_task_branch.py does: a fixed argv of
    # literals in a test. (Do not start this comment with the directive
    # word itself — ruff reads a bare one as a blanket suppression, PGH004.)
    subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "t@t.t"], path)
    _git(["config", "user.name", "t"], path)
    (path / "seed.txt").write_text("seed\n")
    _git(["add", "."], path)
    _git(["commit", "-m", "seed"], path)


def _stage(tmp_path: Path) -> tuple[str, Path, Path]:
    """A project + task worktree in the shape the endpoint expects."""
    project_id, spec_id = "proj-1", "041-feat"
    project_path = tmp_path / "repo"
    _init_repo(project_path)
    (project_path / ".aifactory" / "specs" / spec_id).mkdir(parents=True)
    worktree_path = project_path / ".aifactory" / "worktrees" / "tasks" / spec_id
    # A REAL `git worktree`, not a second independent repo. The endpoint resolves
    # the task branch and then requires a readable ref for it in the PROJECT
    # repo, so two unrelated repos are refused before the commit path is ever
    # reached — which is how the first version of this test passed its
    # `success is False` assertion for entirely the wrong reason.
    _git(
        ["worktree", "add", "-q", "-b", f"aifactory/{spec_id}", str(worktree_path)],
        project_path,
    )

    projects_file = tmp_path / "projects.json"
    projects_file.write_text(json.dumps({project_id: {"path": str(project_path)}}))
    return f"{project_id}:{spec_id}", project_path, projects_file


def _run(task_id: str, projects_file: Path) -> dict[str, Any]:
    with patch.object(worktree_merge, "get_projects_file", return_value=projects_file):
        result = asyncio.run(
            worktree_merge.resolve_worktree_conflicts(task_id, _access={})
        )
    # Factory#460: a refusal leaves as a 409 JSONResponse rather than a
    # 200-wrapped dict. Unwrap so the assertions below read the same body.
    if isinstance(result, JSONResponse):
        assert result.status_code in (REFUSED_STATUS, 200)
        body: dict[str, Any] = json.loads(bytes(result.body))
        return body
    assert isinstance(result, dict)
    return result


def test_a_failing_commit_is_not_reported_as_success(tmp_path: Path) -> None:
    """A pre-commit hook that refuses must not produce `success: true`."""
    task_id, project_path, projects_file = _stage(tmp_path)

    # A hook that always refuses is the cleanest stand-in for the real causes
    # (hook rejection, index.lock, unwritable objects): it makes `git commit`
    # exit non-zero WITHOUT the "nothing to commit" wording, which is exactly
    # the case the old code swallowed.
    hooks = project_path / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'refused by policy' >&2\nexit 1\n")
    hook.chmod(0o755)
    # Something staged, so the commit is genuinely attempted rather than a no-op.
    (project_path / "new.txt").write_text("x\n")
    _git(["add", "new.txt"], project_path)

    result = _run(task_id, projects_file)

    assert result.get("success") is False, (
        "a refused commit was reported as a successful merge — the exact defect "
        f"#1210 records; got {result}"
    )
    assert "commit failed" in (result.get("error") or "").lower(), result


def test_nothing_to_commit_stays_a_success(tmp_path: Path) -> None:
    """The benign case must NOT be turned red by the fix above.

    Nothing staged, no hook: `git commit --no-edit` exits 1 saying there is
    nothing to commit. That is the ordinary state of this path and has to stay
    green, or the fix is just the same bug pointing the other way.
    """
    task_id, _project_path, projects_file = _stage(tmp_path)

    result = _run(task_id, projects_file)

    assert result.get("success") is True, (
        f"an empty commit was reported as a failure — over-correction; got {result}"
    )
