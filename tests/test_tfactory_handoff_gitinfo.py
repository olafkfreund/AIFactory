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


def _init_repo_on_branch(path: Path, branch: str) -> None:
    """A git repo at ``path`` on ``branch`` with an origin remote (push is a no-op)."""
    import subprocess

    def g(*a):
        subprocess.run(["git", *a], cwd=str(path), capture_output=True, text=True)

    path.mkdir(parents=True, exist_ok=True)
    g("init", "-q")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    g("config", "commit.gpgsign", "false")
    g("checkout", "-q", "-b", branch)
    (path / "f").write_text("x")
    g("add", "-A")
    g("commit", "-qm", "x")
    g("remote", "add", "origin", "https://github.com/owner/repo")


def test_kubejob_worktree_on_base_still_stamps_build_branch(tmp_path):
    # #938: on the kubejob path the build runs INSIDE the k8s Job and the
    # control-plane build worktree is left on the base branch (main). The handoff
    # must NOT send source_branch=main (TFactory would verify base, where the built
    # code does not exist) — it must fall back to the canonical build branch.
    sd = tmp_path / "workspaces" / "proj" / ".aifactory" / "specs" / "042-x"
    sd.mkdir(parents=True)
    (sd / "spec.md").write_text("## Acceptance Criteria\n- works\n")
    wt = (
        tmp_path
        / "workspaces"
        / "proj"
        / ".aifactory"
        / "worktrees"
        / "tasks"
        / "042-x"
    )
    _init_repo_on_branch(wt, "main")

    payload = build_ingest_payload(sd, "042-x")
    assert payload["source_branch"] == "aifactory/042-x"  # NOT "main"


def test_diverged_remote_is_force_repaired_not_left_stale(tmp_path):
    # #1007: origin's build branch has DIVERGED from the built HEAD (only the base
    # was ever pushed, so the remote tip is not an ancestor of the build commit).
    # A plain `git push` is rejected non-fast-forward; swallowing that rejection
    # (as the old code did) left origin at the base and handed TFactory a tree
    # WITHOUT the build. The handoff must force the built HEAD onto origin so the
    # verifier clones the ACTUAL build.
    import subprocess

    def g(cwd, *a):
        return subprocess.run(
            ["git", *a], cwd=str(cwd), capture_output=True, text=True
        )

    origin = tmp_path / "origin.git"
    origin.mkdir()
    g(origin, "init", "-q", "--bare")

    proj = tmp_path / "workspaces" / "proj"
    wt = proj / ".aifactory" / "worktrees" / "tasks" / "084-x"
    wt.mkdir(parents=True)
    g(wt, "init", "-q", "-b", "aifactory/084-x")
    g(wt, "config", "user.email", "t@t")
    g(wt, "config", "user.name", "t")
    g(wt, "config", "commit.gpgsign", "false")
    g(wt, "remote", "add", "origin", str(origin))
    # Base commit, pushed to origin so origin/aifactory/084-x = base.
    (wt / "f").write_text("base")
    g(wt, "add", "-A")
    g(wt, "commit", "-qm", "base")
    g(wt, "push", "-q", "origin", "aifactory/084-x")
    base_sha = g(wt, "rev-parse", "HEAD").stdout.strip()
    # DIVERGE: amend so local HEAD is a different, non-descendant commit — the
    # build — which a plain push cannot fast-forward onto the remote base.
    (wt / "f").write_text("def factorial(n): ...")
    g(wt, "add", "-A")
    g(wt, "commit", "-q", "--amend", "-m", "build: factorial")
    build_sha = g(wt, "rev-parse", "HEAD").stdout.strip()
    assert build_sha != base_sha

    sd = proj / ".aifactory" / "specs" / "084-x"
    sd.mkdir(parents=True)
    url, branch = _git_info_and_push(sd, "084-x")

    assert branch == "aifactory/084-x"
    # origin now carries the BUILT commit, not the stale base (#1007).
    remote_tip = g(wt, "ls-remote", str(origin), "aifactory/084-x").stdout.split()[0]
    assert remote_tip == build_sha, "handoff left origin at the stale base tree"
    assert remote_tip != base_sha


def test_packed_path_pushes_build_ref_from_project_repo_not_clone_base(tmp_path):
    # #1007 (RFC-0017 packed path): the build ran in the k8s Job; the control-plane
    # build clone is left on the BASE branch (main), while the built branch ref is
    # unpacked into the PROJECT repo. The handoff must push the build branch
    # resolved from the PROJECT repo -- pushing the clone's base HEAD leaves the
    # build off origin and sends TFactory to a tree without it.
    import subprocess

    def g(cwd, *a):
        return subprocess.run(
            ["git", *a], cwd=str(cwd), capture_output=True, text=True
        )

    origin = tmp_path / "origin.git"
    origin.mkdir()
    g(origin, "init", "-q", "--bare")

    proj = tmp_path / "workspaces" / "proj"
    proj.mkdir(parents=True)
    g(proj, "init", "-q", "-b", "main")
    g(proj, "config", "user.email", "t@t")
    g(proj, "config", "user.name", "t")
    g(proj, "config", "commit.gpgsign", "false")
    g(proj, "remote", "add", "origin", str(origin))
    (proj / "f").write_text("base")
    g(proj, "add", "-A")
    g(proj, "commit", "-qm", "base")
    g(proj, "push", "-q", "origin", "main")
    # The built branch exists only as a REF in the project repo (unpacked artifact).
    g(proj, "checkout", "-q", "-b", "aifactory/090-x")
    (proj / "impl.py").write_text("def is_prime(n): ...")
    g(proj, "add", "-A")
    g(proj, "commit", "-qm", "build: is_prime")
    build_sha = g(proj, "rev-parse", "HEAD").stdout.strip()
    g(proj, "checkout", "-q", "main")  # control plane sits on base, not the build

    # The build clone (worktree path) is a SEPARATE clone left on the base branch.
    wt = proj / ".aifactory" / "worktrees" / "tasks" / "090-x"
    wt.mkdir(parents=True)
    g(wt, "init", "-q", "-b", "main")
    g(wt, "config", "user.email", "t@t")
    g(wt, "config", "user.name", "t")
    g(wt, "config", "commit.gpgsign", "false")
    g(wt, "remote", "add", "origin", str(origin))
    (wt / "f").write_text("base")
    g(wt, "add", "-A")
    g(wt, "commit", "-qm", "base")

    sd = proj / ".aifactory" / "specs" / "090-x"
    sd.mkdir(parents=True)
    url, branch = _git_info_and_push(sd, "090-x")

    assert branch == "aifactory/090-x"
    remote = g(proj, "ls-remote", str(origin), "aifactory/090-x").stdout.split()
    assert remote and remote[0] == build_sha, "build branch not pushed from project repo"


def test_worktree_on_real_build_branch_is_trusted(tmp_path):
    # The legacy path where the worktree genuinely IS on aifactory/<spec_id> keeps
    # working: a non-base branch is used verbatim.
    sd = tmp_path / "workspaces" / "proj" / ".aifactory" / "specs" / "043-y"
    sd.mkdir(parents=True)
    (sd / "spec.md").write_text("## Acceptance Criteria\n- works\n")
    wt = (
        tmp_path
        / "workspaces"
        / "proj"
        / ".aifactory"
        / "worktrees"
        / "tasks"
        / "043-y"
    )
    _init_repo_on_branch(wt, "aifactory/043-y")

    payload = build_ingest_payload(sd, "043-y")
    assert payload["source_branch"] == "aifactory/043-y"
