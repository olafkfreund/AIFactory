#!/usr/bin/env python3
"""Trailing gate runs against the task worktree, not the project root (#597).

The in-cluster (and docker) verification gate silently no-op'd in real builds
because ``detect_gates`` ran at the primary checkout, which has no go.mod /
package.json — the build's code lives in the per-task worktree. These tests pin
the fix: gates are detected+run in the worktree, the run is once-only, and a
"no gates" outcome is recorded visibly rather than skipped silently.
"""

from pathlib import Path

import agents.gate_runner as gate_runner
import implementation_plan.plan as ipp
import pytest
from agents.coder import _run_trailing_gates_if_build_complete


class _FakePlan:
    """A complete plan (no pending subtasks) so the gate proceeds."""

    phases: list = []


def _setup(tmp_path: Path, *, go_in_worktree: bool, go_in_root: bool):
    project_dir = tmp_path / "proj"
    spec_id = "001-demo"
    spec_dir = project_dir / ".aifactory" / "specs" / spec_id
    worktree = project_dir / ".aifactory" / "worktrees" / "tasks" / spec_id
    spec_dir.mkdir(parents=True)
    worktree.mkdir(parents=True)
    (spec_dir / "implementation_plan.json").write_text("{}")
    if go_in_worktree:
        (worktree / "go.mod").write_text("module example.com/x\n")
    if go_in_root:
        (project_dir / "go.mod").write_text("module example.com/x\n")
    return project_dir, spec_dir, worktree


@pytest.mark.asyncio
async def test_gate_runs_in_worktree_not_root(tmp_path, monkeypatch):
    project_dir, spec_dir, worktree = _setup(
        tmp_path, go_in_worktree=True, go_in_root=False
    )
    monkeypatch.setattr(ipp.ImplementationPlan, "load", staticmethod(lambda _p: _FakePlan()))
    seen = {}

    async def fake_run_gates(cwd, gates, **kw):
        seen["cwd"] = Path(cwd)
        seen["gates"] = [g.name for g in gates]
        return []  # no failures

    monkeypatch.setattr(gate_runner, "run_gates", fake_run_gates)

    await _run_trailing_gates_if_build_complete(spec_dir, project_dir)

    # The fix: gates detected + run in the WORKTREE (where go.mod is), not the root.
    assert seen.get("cwd") == worktree
    assert "go-test" in seen["gates"]
    assert (spec_dir / ".trailing_gates_done").exists()


@pytest.mark.asyncio
async def test_runs_only_once(tmp_path, monkeypatch):
    project_dir, spec_dir, _ = _setup(tmp_path, go_in_worktree=True, go_in_root=False)
    monkeypatch.setattr(ipp.ImplementationPlan, "load", staticmethod(lambda _p: _FakePlan()))
    calls = {"n": 0}

    async def fake_run_gates(cwd, gates, **kw):
        calls["n"] += 1
        return []

    monkeypatch.setattr(gate_runner, "run_gates", fake_run_gates)

    await _run_trailing_gates_if_build_complete(spec_dir, project_dir)
    await _run_trailing_gates_if_build_complete(spec_dir, project_dir)  # serial + parallel both reach here
    assert calls["n"] == 1  # run-once marker dedupes


@pytest.mark.asyncio
async def test_no_gates_is_recorded_not_silent(tmp_path, monkeypatch):
    # Neither worktree nor root has a recognizable project -> no gates. The fix
    # records this visibly (honest) instead of returning with no trace.
    project_dir, spec_dir, _ = _setup(tmp_path, go_in_worktree=False, go_in_root=False)
    monkeypatch.setattr(ipp.ImplementationPlan, "load", staticmethod(lambda _p: _FakePlan()))

    async def boom(*a, **k):  # must never run when no gates
        raise AssertionError("run_gates called with no gates detected")

    monkeypatch.setattr(gate_runner, "run_gates", boom)

    await _run_trailing_gates_if_build_complete(spec_dir, project_dir)

    marker = spec_dir / ".trailing_gates_done"
    assert marker.exists()
    assert "no gates detected" in marker.read_text()
