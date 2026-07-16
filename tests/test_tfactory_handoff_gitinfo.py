"""PARR handoff fix: payload carries git_url + source_branch (pushes the branch)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from pfactory.tfactory_client import (
    _build_worktree,
    _git_info_and_push,
    build_ingest_payload,
)  # noqa: E402


def test_worktree_path_derivation():
    spec_dir = Path("/data/workspaces/proj/.aifactory/specs/027-x")
    wt = _build_worktree(spec_dir, "027-x")
    assert wt == Path("/data/workspaces/proj/.aifactory/worktrees/tasks/027-x")


def test_git_info_graceful_when_no_worktree(tmp_path):
    # No worktree dir -> (None, None), never raises.
    assert _git_info_and_push(
        tmp_path / "nope" / ".aifactory" / "specs" / "1", "1"
    ) == (None, None)


def test_payload_still_sets_branch_without_git(tmp_path):
    # No worktree AND no project git: git_url is omitted (nothing to resolve) but
    # source_branch still comes from the fixed build convention (#893) so TFactory
    # fetches the built branch instead of verifying BASE.
    sd = tmp_path / "workspaces" / "proj" / ".aifactory" / "specs" / "1"
    sd.mkdir(parents=True)
    (sd / "spec.md").write_text("## Acceptance Criteria\n- works\n")
    payload = build_ingest_payload(sd, "1")
    assert payload["project_id"] == "proj"
    assert payload["source_branch"] == "aifactory/1"
    assert "git_url" not in payload  # no repo to resolve → graceful


def test_payload_resolves_repo_from_project_when_worktree_absent(tmp_path):
    # RFC-0017 packed build: no valid build worktree on the control plane, but the
    # project's shared base repo (<project>/.git) still knows origin. #893: the
    # payload must carry BOTH git_url (from the project repo) and source_branch
    # (from the build convention) so TFactory fetches the actual built code.
    import subprocess

    proj = tmp_path / "workspaces" / "proj"
    proj.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/o/r.git"],
        cwd=proj,
        check=True,
    )
    sd = proj / ".aifactory" / "specs" / "014-x"
    sd.mkdir(parents=True)
    (sd / "spec.md").write_text("## Acceptance Criteria\n- works\n")
    # No worktree dir exists → _git_info_and_push returns (None, None).
    payload = build_ingest_payload(sd, "014-x")
    assert payload["git_url"] == "https://github.com/o/r.git"
    assert payload["source_branch"] == "aifactory/014-x"


def test_payload_includes_git_fields_for_a_real_worktree(tmp_path, monkeypatch):
    # A real git worktree with an origin -> payload carries git_url + branch.
    import subprocess

    proj = tmp_path / "workspaces" / "proj"
    wt = proj / ".aifactory" / "worktrees" / "tasks" / "1"
    wt.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "aifactory/1"], cwd=wt, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/o/r.git"],
        cwd=wt,
        check=True,
    )
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=wt, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=wt, check=True)
    (wt / "f").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-qm", "c"], cwd=wt, check=True)
    # neutralize the network push
    real = subprocess.run

    def fake_run(args, **kw):
        if args[:2] == ["git", "push"]:

            class R:
                stdout = ""
                returncode = 0

            return R()
        return real(args, **kw)

    monkeypatch.setattr(subprocess, "run", fake_run)
    sd = proj / ".aifactory" / "specs" / "1"
    sd.mkdir(parents=True)
    (sd / "spec.md").write_text("## Acceptance Criteria\n- works\n")
    payload = build_ingest_payload(sd, "1")
    assert payload["git_url"] == "https://github.com/o/r.git"
    assert payload["source_branch"] == "aifactory/1"
