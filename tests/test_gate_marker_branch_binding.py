"""Gate evidence must be readable where there is no working copy of the task (#1550).

The control plane (merger PR body, QA guard) often has no task worktree, so
`gate_dir_for` falls back to `project_dir`, whose HEAD is main -- and a marker
bound to the task's commit never matched it. The task branch tip is the tree the
evidence is about there. On the packed path the marker also never left the Job.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

import core.workspace_fetch as wf  # noqa: E402
from agents.gate_runner import (  # noqa: E402
    trailing_gate_evidence,
    write_trailing_gate_marker,
)

SPEC = "042-x"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, Path, str]:
    """main + aifactory/<SPEC> at commit X, then main advanced past it."""
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(
        repo,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
    )
    task_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "branch", f"aifactory/{SPEC}")
    _git(
        repo,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "main moves on",
    )
    spec_dir = repo / ".aifactory" / "specs" / SPEC
    spec_dir.mkdir(parents=True)
    return repo, spec_dir, task_sha


def _mark(spec_dir: Path, sha: str, evidence: str = "pytest: passed") -> None:
    (spec_dir / ".trailing_gates_done").write_text(f"{sha}\n{evidence}\n")


def test_marker_for_branch_tip_counts_without_a_worktree(tmp_path):
    repo, spec_dir, task_sha = _repo(tmp_path)
    _mark(spec_dir, task_sha)
    assert trailing_gate_evidence(spec_dir, repo) == "pytest: passed"


def test_marker_is_stale_once_the_branch_moves_on(tmp_path):
    repo, spec_dir, task_sha = _repo(tmp_path)
    _mark(spec_dir, task_sha)
    _git(repo, "checkout", "-q", f"aifactory/{SPEC}")
    _git(
        repo,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "fix",
    )
    _git(repo, "checkout", "-q", "main")
    assert trailing_gate_evidence(spec_dir, repo) is None


def test_no_task_branch_falls_back_to_head_and_stays_absent(tmp_path):
    repo, spec_dir, task_sha = _repo(tmp_path)
    _git(repo, "branch", "-D", f"aifactory/{SPEC}")
    _mark(spec_dir, task_sha)  # main's HEAD moved on, no branch to vouch for it
    assert trailing_gate_evidence(spec_dir, repo) is None


def test_packed_path_origin_tip_counts_without_a_local_branch(tmp_path):
    """#1550: the packed Job pushes to origin; the control plane has no local
    task branch, only the ``origin/aifactory/<spec>`` ref the merger fetched."""
    repo, spec_dir, task_sha = _repo(tmp_path)
    _git(repo, "branch", "-D", f"aifactory/{SPEC}")
    _git(repo, "update-ref", f"refs/remotes/origin/aifactory/{SPEC}", task_sha)
    _mark(spec_dir, task_sha)
    assert trailing_gate_evidence(spec_dir, repo) == "pytest: passed"


def test_origin_tip_counts_over_a_stale_local_branch(tmp_path):
    """A stale local ref must not hide evidence for the commit origin holds."""
    repo, spec_dir, task_sha = _repo(tmp_path)
    # The Job built one more commit and pushed it; the local branch still sits
    # at the pre-build tip, and main's HEAD is elsewhere.
    _git(repo, "checkout", "-q", f"aifactory/{SPEC}")
    _git(
        repo,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "built in the Job",
    )
    pushed = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "update-ref", f"refs/remotes/origin/aifactory/{SPEC}", pushed)
    _git(repo, "branch", "-f", f"aifactory/{SPEC}", task_sha)
    _mark(spec_dir, pushed)
    assert trailing_gate_evidence(spec_dir, repo) == "pytest: passed"


def test_marker_matching_neither_tip_stays_absent(tmp_path):
    repo, spec_dir, task_sha = _repo(tmp_path)
    _git(repo, "update-ref", f"refs/remotes/origin/aifactory/{SPEC}", task_sha)
    _mark(spec_dir, "0" * 40)
    assert trailing_gate_evidence(spec_dir, repo) is None


def test_writer_binding_unchanged_in_project_dir(tmp_path):
    repo, spec_dir, _ = _repo(tmp_path)
    write_trailing_gate_marker(spec_dir, repo, "pytest: passed")
    assert trailing_gate_evidence(spec_dir, repo) == "pytest: passed"


# ── packed path: the marker must leave the Job ───────────────────────────────


class _FakeStore:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def put_bytes(self, key, data, _ct=None, **_):
        self.blobs[key] = data
        return key

    def get_bytes(self, key):
        return self.blobs[key]


def test_push_then_fetch_round_trips_and_overwrites(tmp_path, monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr("core.artifact_store.ArtifactStore", lambda *_a, **_k: store)
    monkeypatch.setenv(wf.WORKSPACE_URI_ENV, "s3://b/ws.tar.gz")
    job, ctrl = tmp_path / "job", tmp_path / "ctrl"
    job.mkdir()
    ctrl.mkdir()
    (job / ".trailing_gates_done").write_text("abc\npytest: passed\n")
    (ctrl / ".trailing_gates_done").write_text("old\npytest: failed\n")
    assert wf.maybe_push_gate_marker(job, SPEC) is True
    assert wf.maybe_fetch_gate_marker(ctrl, SPEC) is True
    assert (ctrl / ".trailing_gates_done").read_text() == "abc\npytest: passed\n"


def test_push_is_noop_off_packed_path_or_without_marker(tmp_path, monkeypatch):
    monkeypatch.delenv(wf.WORKSPACE_URI_ENV, raising=False)
    (tmp_path / ".trailing_gates_done").write_text("abc\nx\n")
    assert wf.maybe_push_gate_marker(tmp_path, SPEC) is False
    monkeypatch.setenv(wf.WORKSPACE_URI_ENV, "s3://b/ws.tar.gz")
    (tmp_path / ".trailing_gates_done").unlink()
    assert wf.maybe_push_gate_marker(tmp_path, SPEC) is False  # absence stays absence


def test_fetch_with_nothing_pushed_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "core.artifact_store.ArtifactStore", lambda *_a, **_k: _FakeStore()
    )
    assert wf.maybe_fetch_gate_marker(tmp_path, SPEC) is False
    assert not (tmp_path / ".trailing_gates_done").exists()


def _source_and_worktree_spec(project: Path) -> tuple[Path, Path]:
    source = project / ".aifactory" / "specs" / SPEC
    worktree = (
        project
        / ".aifactory"
        / "worktrees"
        / "tasks"
        / SPEC
        / ".aifactory"
        / "specs"
        / SPEC
    )
    source.mkdir(parents=True)
    worktree.mkdir(parents=True)
    return source, worktree


def test_isolated_build_pushes_the_worktree_marker(tmp_path, monkeypatch):
    """#1550 (Copilot on #1563): isolated mode writes the marker into the
    worktree's spec copy; the build-end push is handed the SOURCE spec dir.
    It must still upload what the build wrote."""
    store = _FakeStore()
    monkeypatch.setattr("core.artifact_store.ArtifactStore", lambda *_a, **_k: store)
    monkeypatch.setenv(wf.WORKSPACE_URI_ENV, "s3://b/ws.tar.gz")
    source, worktree = _source_and_worktree_spec(tmp_path)
    (worktree / ".trailing_gates_done").write_text("abc\npytest: passed\n")

    chosen = wf.gate_marker_spec_dir(tmp_path, source)
    assert chosen == worktree
    assert wf.maybe_push_gate_marker(chosen, SPEC) is True
    ctrl = tmp_path / "ctrl"
    ctrl.mkdir()
    assert wf.maybe_fetch_gate_marker(ctrl, SPEC) is True
    assert (ctrl / ".trailing_gates_done").read_text() == "abc\npytest: passed\n"


def test_direct_mode_keeps_the_given_spec_dir(tmp_path):
    source, _worktree = _source_and_worktree_spec(tmp_path)  # no marker in worktree
    (source / ".trailing_gates_done").write_text("abc\npytest: passed\n")
    assert wf.gate_marker_spec_dir(tmp_path, source) == source
