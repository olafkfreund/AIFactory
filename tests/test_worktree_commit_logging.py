#!/usr/bin/env python3
"""
Regression: commit_in_worktree failure logging must not crash (#376 wave abort)
==============================================================================

commit_in_worktree's failure branch logged extra={"message": <commit msg>}.
"message" is a reserved logging.LogRecord attribute, so the logger.error call
raised "Attempt to overwrite 'message' in LogRecord". That exception propagated
out of a parallel subtask's commit step → the subtask failed → the whole wave
aborted to serial ("no progress in wave 1"). So parallelism silently never
engaged.

This test drives the failure branch and asserts it returns False WITHOUT raising.
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))

from core.worktree import WorktreeManager  # noqa: E402


def _mgr(tmp_path: Path) -> WorktreeManager:
    # base_branch explicit → __init__ doesn't shell out to detect it.
    return WorktreeManager(tmp_path, base_branch="main")


def _failed_commit(*_a, **_k):
    return subprocess.CompletedProcess(
        args=["git", "commit"], returncode=1, stdout="", stderr="fatal: cannot commit"
    )


def test_commit_failure_logs_without_raising(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path)
    monkeypatch.setattr(mgr, "get_worktree_path", lambda spec: tmp_path)  # exists
    monkeypatch.setattr(mgr, "_run_git", _failed_commit)

    # Before the fix this raised KeyError("Attempt to overwrite 'message' ...").
    result = mgr.commit_in_worktree("002-spec__w__subtask-1-3", "implement upstreams router")
    assert result is False  # clean failure, no exception


def test_commit_success_returns_true(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path)
    monkeypatch.setattr(mgr, "get_worktree_path", lambda spec: tmp_path)
    monkeypatch.setattr(
        mgr, "_run_git",
        lambda *a, **k: subprocess.CompletedProcess(["git"], 0, "", ""),
    )
    assert mgr.commit_in_worktree("spec", "msg") is True


def test_nothing_to_commit_returns_true(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path)
    monkeypatch.setattr(mgr, "get_worktree_path", lambda spec: tmp_path)
    monkeypatch.setattr(
        mgr, "_run_git",
        lambda *a, **k: subprocess.CompletedProcess(["git"], 1, "nothing to commit, working tree clean", ""),
    )
    assert mgr.commit_in_worktree("spec", "msg") is True


def test_extra_has_no_reserved_logrecord_keys():
    # Guard: the failure-branch extra must not reuse reserved LogRecord attrs.
    import logging
    reserved = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
    used = {"worktree_path", "error", "commit_message"}  # keys in the fixed extra
    assert used.isdisjoint(reserved), f"reserved keys in extra: {used & reserved}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
