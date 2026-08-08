#!/usr/bin/env python3
"""
Regression: a build branch is cut from the CURRENT remote tip, and carries no
factory bookkeeping (#1106, Factory#245)
=============================================================================

Found while recording the Factory#245 HITL governance demo. Approve opened a PR
and GitHub refused the merge: ``mergeable: false, mergeable_state: dirty``. Two
defects compounded.

1. The project checkout a build runs in is cloned once and reused, so its LOCAL
   base branch drifts behind ``origin/<base>``. The task worktree was cut from
   that stale ref, so the PR was based on an old commit.
2. ``.aifactory-status`` (the ccstatusline file) was written at the repo ROOT of
   the worktree and rewritten on every subtask. In the demo project it was
   already a tracked file, so the coder staged it and the PR carried a
   factory-internal file that conflicted with the base.

Either alone is enough to break Approve, so both are asserted here. The git
plumbing exercised is the real one: a real origin repo, a real clone, a real
``git worktree add``. What is NOT exercised is a live build in the cluster.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1] / "apps" / "backend"
sys.path.insert(0, str(_BACKEND))

from agents.utils import commit_uncommitted_changes  # noqa: E402
from core.worktree import WorktreeError, WorktreeManager  # noqa: E402
from ui.status import BuildState, StatusManager  # noqa: E402


def _git(args: list[str], cwd: Path, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=check
    )
    return result.stdout.strip()


@pytest.fixture
def origin_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """An ``origin`` repo on ``main`` plus a clone standing in for the checkout."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(["init", "-b", "main"], origin)
    _git(["config", "user.email", "t@example.com"], origin)
    _git(["config", "user.name", "Test"], origin)
    (origin / "app.py").write_text("v1\n")
    # A tracked .aifactory-status, exactly as the demo repo carries it — this is
    # what makes the gitignore-only remedy useless.
    (origin / ".aifactory-status").write_text('{"active": false}\n')
    _git(["add", "-A"], origin)
    _git(["commit", "-m", "base"], origin)

    clone = tmp_path / "checkout"
    _git(["clone", str(origin), str(clone)], tmp_path)
    _git(["config", "user.email", "t@example.com"], clone)
    _git(["config", "user.name", "Test"], clone)
    return origin, clone


def _advance_origin(origin: Path, message: str = "moved on") -> str:
    """Land a commit on origin/main that the clone has not fetched."""
    (origin / "other.py").write_text(f"{message}\n")
    _git(["add", "-A"], origin)
    _git(["commit", "-m", message], origin)
    return _git(["rev-parse", "HEAD"], origin)


def test_worktree_is_cut_from_the_current_origin_tip(origin_and_clone):
    """The build branch must be based on origin's HEAD, not the stale local one."""
    origin, clone = origin_and_clone
    stale_local = _git(["rev-parse", "main"], clone)
    remote_tip = _advance_origin(origin)
    assert stale_local != remote_tip  # the drift the bug rode in on

    info = WorktreeManager(clone).create_worktree("001-add-a-thing")

    base = _git(["rev-parse", "HEAD"], info.path)
    assert base == remote_tip, "worktree was cut from the stale local base"


def test_pr_diff_carries_no_factory_bookkeeping(origin_and_clone):
    """The diff Approve turns into a PR must contain code only."""
    origin, clone = origin_and_clone
    remote_tip = _advance_origin(origin)
    info = WorktreeManager(clone).create_worktree("001-add-a-thing")

    # A build: the status manager runs, then the coder writes code, then the
    # safety net sweeps up whatever was left uncommitted.
    StatusManager(info.path).set_active("001-add-a-thing", BuildState.BUILDING)
    (info.path / "app.py").write_text("v2\n")
    commit_uncommitted_changes(info.path, "001-add-a-thing")

    changed = _git(["diff", "--name-only", f"{remote_tip}...HEAD"], info.path).split()
    assert "app.py" in changed, "the actual work must be in the PR"
    assert ".aifactory-status" not in changed
    assert not [f for f in changed if f.startswith(".aifactory")]


def test_status_file_is_written_inside_the_gitignored_runtime_dir(tmp_path: Path):
    """Nothing at the repo root, so nothing for a coder's `git add -A` to stage."""
    StatusManager(tmp_path).set_active("001-add-a-thing", BuildState.BUILDING)

    assert (tmp_path / ".aifactory" / "status.json").exists()
    assert not (tmp_path / ".aifactory-status").exists()


def test_unfetchable_origin_fails_loudly_instead_of_using_a_stale_base(
    origin_and_clone,
):
    """A refresh that cannot happen must fail the build, not silently degrade.

    Falling back to the local ref is precisely the bug: it produces a
    green-looking build whose PR cannot merge, discovered only when a human
    clicks Approve.
    """
    origin, clone = origin_and_clone
    _advance_origin(origin)
    _git(["remote", "set-url", "origin", str(clone / "does-not-exist")], clone)

    with pytest.raises(WorktreeError, match="Could not fetch base branch"):
        WorktreeManager(clone).create_worktree("001-add-a-thing")


def test_unfetchable_origin_fails_even_when_the_tracking_ref_looks_current(
    origin_and_clone,
):
    """The 'origin/<base> matches, so it must be fine' shortcut is not allowed.

    A remote-tracking ref is the memory of an earlier fetch, not evidence of
    currency: in a checkout that never fetched it equals the stale local branch,
    which is exactly the reported failure. Guarding on it would report success
    while measuring nothing.
    """
    origin, clone = origin_and_clone
    _advance_origin(origin)  # origin moves; the clone's origin/main does not
    assert _git(["rev-parse", "main"], clone) == _git(
        ["rev-parse", "refs/remotes/origin/main"], clone
    )
    _git(["remote", "set-url", "origin", str(clone / "does-not-exist")], clone)

    with pytest.raises(WorktreeError):
        WorktreeManager(clone).create_worktree("001-add-a-thing")


def test_base_branch_absent_from_the_remote_still_builds(origin_and_clone):
    """A local-only base has no remote tip to be behind, so it is not stale.

    This is the documented `_detect_base_branch` fallback (a repo with no
    main/master, cut from the current branch). Failing it would break every
    build on a remote ref that was never meant to exist. Narrow by design: only
    git's own "couldn't find remote ref" passes, never an unreachable or
    unauthorised remote.
    """
    _origin, clone = origin_and_clone
    _git(["checkout", "-q", "-b", "local-only"], clone)

    info = WorktreeManager(clone, base_branch="local-only").create_worktree("001-x")

    assert _git(["rev-parse", "HEAD"], info.path) == _git(
        ["rev-parse", "local-only"], clone
    )


def test_local_only_repo_still_builds(tmp_path: Path):
    """No origin at all (offline dev, unit fixtures) — nothing to be stale against."""
    repo = tmp_path / "solo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    (repo / "app.py").write_text("v1\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "base"], repo)

    info = WorktreeManager(repo).create_worktree("001-add-a-thing")

    assert _git(["rev-parse", "HEAD"], info.path) == _git(["rev-parse", "main"], repo)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
