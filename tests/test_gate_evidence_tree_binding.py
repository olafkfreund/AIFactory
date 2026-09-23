"""Trailing-gate evidence must be bound to the tree it was recorded for.

`.trailing_gates_done` is persistent: worktree setup copies the whole spec
directory, and the web sync republishes files into a resumed build's spec
dir. Either can carry a marker written for a DIFFERENT commit into a build
that never ran a gate over it. A QA-fixer iteration that then commits a fix
must also get its own gate run, not the previous commit's stale pass
(AIFactory#1545).

These tests drive the real git-backed helpers in `agents.gate_runner`
end-to-end (real `git init`/`commit`, no mocking) since the whole point is
that the binding tracks an ACTUAL tree identity.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from agents.gate_runner import (  # noqa: E402
    gate_dir_for,
    trailing_gate_evidence,
    trailing_gate_marker_is_current,
    write_trailing_gate_marker,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "first")
    return repo


def test_evidence_written_for_this_commit_is_accepted(tmp_path):
    project = _repo(tmp_path)
    spec = tmp_path / "spec"
    spec.mkdir()

    write_trailing_gate_marker(spec, gate_dir_for(spec, project), "pytest: passed")

    assert trailing_gate_evidence(spec, project) == "pytest: passed"


def test_a_marker_copied_in_from_a_different_commit_is_not_evidence(tmp_path):
    """The #1545 repro: a marker written for one commit gets carried (by a
    worktree copy, or a web-sync republish) into a build sitting on a LATER
    commit. It must read as no evidence, not a stale pass."""
    project = _repo(tmp_path)
    spec = tmp_path / "spec"
    spec.mkdir()

    write_trailing_gate_marker(spec, gate_dir_for(spec, project), "pytest: passed")
    assert trailing_gate_evidence(spec, project) == "pytest: passed"

    # A QA-fixer iteration (or a resumed build) commits new work -- HEAD moves,
    # but the marker was never regenerated.
    (project / "a.py").write_text("x = 2\n")
    _git(project, "add", "a.py")
    _git(project, "commit", "-qm", "fix")

    assert trailing_gate_evidence(spec, project) is None
    assert trailing_gate_marker_is_current(spec, gate_dir_for(spec, project)) is False


def test_a_marker_carried_by_worktree_setup_into_a_resumed_build_is_rejected(
    tmp_path,
):
    """Same failure, different vector: worktree setup copies the whole spec
    dir including a marker from an earlier resume of the SAME spec, but the
    project has since moved to a new commit."""
    project = _repo(tmp_path)
    spec = tmp_path / "spec"
    spec.mkdir()
    write_trailing_gate_marker(spec, gate_dir_for(spec, project), "mypy: passed")

    # Simulate the worktree-copy vector directly: the marker's bytes are
    # identical, but the project tree has moved on since they were written.
    marker_bytes = (spec / ".trailing_gates_done").read_bytes()
    resumed_spec = tmp_path / "resumed-spec"
    resumed_spec.mkdir()
    (resumed_spec / ".trailing_gates_done").write_bytes(marker_bytes)
    (project / "a.py").write_text("x = 3\n")
    _git(project, "add", "a.py")
    _git(project, "commit", "-qm", "second build")

    assert trailing_gate_evidence(resumed_spec, project) is None


def test_a_fresh_commit_after_the_gate_run_retriggers_it(tmp_path, monkeypatch):
    """The other half of #1545: the run-once guard used to key on marker
    EXISTENCE alone, so a QA-fixer commit after the one gate run never got its
    own gate pass. `trailing_gate_marker_is_current` is what
    `_run_trailing_gates_if_build_complete`'s run-once guard now checks."""
    import implementation_plan.plan as ipp
    from agents.coder import _run_trailing_gates_if_build_complete

    class _FakePlan:
        phases: list = []

    project = _repo(tmp_path)
    spec = tmp_path / "spec"
    spec.mkdir()
    (spec / "implementation_plan.json").write_text("{}")
    monkeypatch.setattr(
        ipp.ImplementationPlan, "load", staticmethod(lambda _p: _FakePlan())
    )

    calls = {"n": 0}

    async def fake_run_gates(cwd, gates, **kw):
        calls["n"] += 1
        return []

    import agents.gate_runner as gate_runner

    monkeypatch.setattr(gate_runner, "run_gates", fake_run_gates)
    # No gate-detectable project files -> "no gates detected" path, which
    # still writes (and re-writes) the marker with the current sha.

    import asyncio

    asyncio.run(_run_trailing_gates_if_build_complete(spec, project))
    marker_after_first = (spec / ".trailing_gates_done").read_text()

    # Same tree, called again (parallel wave + serial post-loop both reach
    # this) -- must NOT rewrite the marker.
    asyncio.run(_run_trailing_gates_if_build_complete(spec, project))
    assert (spec / ".trailing_gates_done").read_text() == marker_after_first

    # A QA-fixer commits a fix -- HEAD moves, so the next call must treat the
    # marker as stale and run (here: write) again rather than silently no-op.
    (project / "a.py").write_text("x = 9\n")
    _git(project, "add", "a.py")
    _git(project, "commit", "-qm", "qa fix")

    asyncio.run(_run_trailing_gates_if_build_complete(spec, project))
    marker_after_fix = (spec / ".trailing_gates_done").read_text()
    assert marker_after_fix != marker_after_first, (
        "the marker must rebind to the new commit, not keep the stale sha"
    )
