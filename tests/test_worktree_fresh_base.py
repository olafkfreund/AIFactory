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

import json
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


def test_adopted_worktree_preserves_approved_branch_and_rejects_drift(origin_and_clone):
    origin, clone = origin_and_clone
    branch = "factory/2172-approved"
    _git(["checkout", "-q", "-b", branch], origin)
    for folder in ("intent", "spec", "plan"):
        (origin / folder).mkdir()
        (origin / folder / "approved.md").write_text(f"{folder}\n")
    _git(["add", "-A"], origin)
    _git(["commit", "-qm", "approved"], origin)
    approved = _git(["rev-parse", "HEAD"], origin)

    spec = clone / ".aifactory" / "specs" / "2172-approved"
    spec.mkdir(parents=True)
    (spec / "task_metadata.json").write_text(
        json.dumps({"source_branch": branch, "approved_head_sha": approved})
    )
    manager = WorktreeManager(clone)
    info = manager.create_worktree("2172-approved")
    assert info.branch == branch
    assert _git(["rev-parse", "HEAD"], info.path) == approved
    assert all(
        (info.path / folder / "approved.md").exists()
        for folder in ("intent", "spec", "plan")
    )

    (origin / "later.md").write_text("unapproved\n")
    _git(["add", "-A"], origin)
    _git(["commit", "-qm", "later"], origin)
    with pytest.raises(WorktreeError, match="no longer matches approved head"):
        manager.create_worktree("2172-approved")
    assert _git(["rev-parse", "HEAD"], info.path) == approved

    (spec / "memory").mkdir()
    (spec / "memory" / "build_commits.json").write_text(
        json.dumps({"commits": [{"sha": approved}]})
    )
    resumed = manager.create_worktree("2172-approved")
    assert resumed.branch == branch
    assert _git(["rev-parse", "HEAD"], resumed.path) == _git(
        ["rev-parse", branch], origin
    )


def test_null_source_branch_uses_normal_task_branch(origin_and_clone):
    _, clone = origin_and_clone
    spec = clone / ".aifactory" / "specs" / "001-normal"
    spec.mkdir(parents=True)
    (spec / "task_metadata.json").write_text('{"source_branch": null}')
    manager = WorktreeManager(clone)
    assert manager.get_branch_name("001-normal") == "aifactory/001-normal"


def test_adopted_checkout_uses_verified_sha(origin_and_clone, monkeypatch):
    origin, clone = origin_and_clone
    branch = "factory/approved"
    _git(["checkout", "-q", "-b", branch], origin)
    approved = _git(["rev-parse", "HEAD"], origin)
    spec = clone / ".aifactory" / "specs" / "002-approved"
    spec.mkdir(parents=True)
    (spec / "task_metadata.json").write_text(
        json.dumps({"source_branch": branch, "approved_head_sha": approved})
    )
    manager = WorktreeManager(clone)
    original = manager._run_git
    commands = []

    def capture(args, *a, **kw):
        commands.append(args)
        return original(args, *a, **kw)

    monkeypatch.setattr(manager, "_run_git", capture)
    manager.create_worktree("002-approved")
    assert any(
        args[:2] == ["worktree", "add"] and args[-1] == approved for args in commands
    )
    assert any(
        args[:3] == ["fetch", "--", "origin"]
        and f"refs/heads/{branch}:refs/remotes/origin/{branch}" in args[-1]
        for args in commands
    )


def test_verified_adopted_branch_cannot_be_removed_from_metadata(
    origin_and_clone, monkeypatch
):
    from core import contract_trust

    _, clone = origin_and_clone
    spec = clone / ".aifactory/specs/003-approved"
    spec.mkdir(parents=True)
    (spec / "task_metadata.json").write_text("{}")
    monkeypatch.setenv(contract_trust.ENV, "verified-digest")
    monkeypatch.setattr(
        contract_trust,
        "trusted_contract",
        lambda *_a: (
            "verified",
            {"provenance": {"source_branch": "factory/approved"}},
            "",
        ),
    )
    with pytest.raises(WorktreeError, match="metadata missing"):
        WorktreeManager(clone).get_branch_name("003-approved")


@pytest.mark.parametrize("branch", ["HEAD", "-upload-pack=evil"])
def test_metadata_base_branch_rejects_unsafe_names(origin_and_clone, branch):
    _, clone = origin_and_clone
    with pytest.raises(WorktreeError, match="Invalid base branch"):
        WorktreeManager(clone, base_branch=branch)


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


def test_git_env_carries_no_github_token(monkeypatch):
    """#1723: the worktree's git children get no GitHub token in env."""
    from core.worktree import _git_env

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_1723notarealtoken")  # gitleaks:allow
    monkeypatch.setenv("GH_TOKEN", "ghp_1723notarealtoken")  # gitleaks:allow
    env = _git_env()
    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env


def test_base_fetch_gets_the_token_per_call_and_never_runs_setup_git(
    origin_and_clone, monkeypatch
):
    token = "ghp_1723notarealtoken"  # gitleaks:allow
    monkeypatch.setenv("GITHUB_TOKEN", token)
    origin, clone = origin_and_clone
    _advance_origin(origin)
    real_run = subprocess.run
    argvs: list[list[str]] = []
    fetches: list[tuple[dict[str, str], str]] = []

    def recorder(argv, **kw):
        argvs.append(list(argv))
        if argv[-4:-2] == ["fetch", "--"] and argv[-2] == "origin":
            env = kw["env"]
            # the per-call dir is removed on exit: read hosts.yml now
            fetches.append((env, Path(env["GH_CONFIG_DIR"], "hosts.yml").read_text()))
        return real_run(argv, **kw)

    # patch via the class's own module: another test may swap sys.modules["core"]
    wt_mod = sys.modules[WorktreeManager.__module__]
    monkeypatch.setattr(wt_mod.subprocess, "run", recorder)
    WorktreeManager(clone).create_worktree("001-add-a-thing")

    assert fetches, "no fetch origin <base> call was recorded"
    env, hosts = fetches[0]
    assert token in hosts
    assert token not in "".join(env.values())
    pairs = {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert pairs["credential.https://github.com.helper"] == "!gh auth git-credential"
    assert not any("setup-git" in a for argv in argvs for a in argv)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
