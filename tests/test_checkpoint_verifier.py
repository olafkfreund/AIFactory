#!/usr/bin/env python3
"""
Tests for checkpoints + per-unit verifier (Phase 2 of #397)
===========================================================

Checkpointer must snapshot the task-branch HEAD and roll back to it (dropping
untracked files a failed unit created). The verifier must shape gate results into
a pass/fail verdict the self-heal loop can act on, with no real subprocesses.
"""

import subprocess
from pathlib import Path

import pytest
from agents.checkpoint import Checkpoint, Checkpointer
from agents.gate_runner import Gate
from agents.verifier import verify_unit


def _commit(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content)
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", f"add {name}"], cwd=repo, capture_output=True
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()


class TestCheckpointer:
    def test_snapshot_records_head(self, temp_git_repo: Path):
        cp = Checkpointer(temp_git_repo)
        snap = cp.snapshot("pre-wave-1")
        assert snap is not None
        assert snap.label == "pre-wave-1"
        assert snap.ref == cp.current_head()

    def test_rollback_restores_committed_state(self, temp_git_repo: Path):
        cp = Checkpointer(temp_git_repo)
        snap = cp.snapshot("good")
        # A "bad wave" commits a file after the checkpoint.
        _commit(temp_git_repo, "bad.py", "raise SystemExit\n")
        assert (temp_git_repo / "bad.py").exists()

        assert cp.rollback(snap) is True
        assert not (temp_git_repo / "bad.py").exists()  # commit undone
        assert cp.current_head() == snap.ref

    def test_rollback_drops_untracked_files(self, temp_git_repo: Path):
        cp = Checkpointer(temp_git_repo)
        snap = cp.snapshot("good")
        # A failed unit leaves a half-written untracked file.
        (temp_git_repo / "half_written.py").write_text("def broken(:\n")

        assert cp.rollback(snap) is True
        assert not (temp_git_repo / "half_written.py").exists()

    def test_multiple_checkpoints_tracked_for_report(self, temp_git_repo: Path):
        cp = Checkpointer(temp_git_repo)
        cp.snapshot("pre-wave-1")
        _commit(temp_git_repo, "a.py", "x = 1\n")
        cp.snapshot("pre-wave-2")
        report = cp.as_report()
        assert [c["label"] for c in report] == ["pre-wave-1", "pre-wave-2"]
        assert cp.latest().label == "pre-wave-2"

    def test_snapshot_without_git_returns_none(self, tmp_path: Path):
        # Not a git repo → snapshot is a no-op, not a crash.
        cp = Checkpointer(tmp_path)
        assert cp.snapshot("x") is None


class TestVerifier:
    async def test_passes_when_no_gates_detected(self, tmp_path: Path):
        # Empty dir → no gates → not a failure.
        result = await verify_unit(tmp_path)
        assert result.passed is True
        assert result.ran is False

    async def test_failing_gate_marks_failed(self, tmp_path: Path):
        (tmp_path / "tests").mkdir()  # triggers a pytest gate
        gates = [Gate("pytest", ["pytest", "-q"])]

        def runner(cmd, cwd):
            return 1, "1 failed"  # non-zero exit

        result = await verify_unit(tmp_path, gates=gates, runner=runner)
        assert result.passed is False
        assert "pytest" in result.failures
        assert result.ran is True

    async def test_passing_gate_marks_passed(self, tmp_path: Path):
        gates = [Gate("pytest", ["pytest", "-q"])]
        result = await verify_unit(tmp_path, gates=gates, runner=lambda c, d: (0, "ok"))
        assert result.passed is True
        assert result.failures == []

    async def test_missing_tool_is_skipped_not_failed(self, tmp_path: Path):
        gates = [Gate("mypy", ["mypy", "."])]
        # exit_code None => tool missing => skipped => not a failure.
        result = await verify_unit(
            tmp_path, gates=gates, runner=lambda c, d: (None, "not found")
        )
        assert result.passed is True
        assert result.failures == []

    async def test_mixed_gates_fail_if_any_real_failure(self, tmp_path: Path):
        gates = [Gate("lint", ["lint"]), Gate("pytest", ["pytest"])]
        outcomes = {"lint": (0, "ok"), "pytest": (1, "fail")}
        result = await verify_unit(
            tmp_path, gates=gates, runner=lambda c, d: outcomes[c[0]]
        )
        assert result.passed is False
        assert result.failures == ["pytest"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
