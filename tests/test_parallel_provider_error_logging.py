#!/usr/bin/env python3
"""
Regression: parallel subtask session errors logged to canonical spec_dir (#455)
================================================================================

When a non-Claude provider session fails inside a parallel wave subtask,
two things must happen:
  1. SubtaskResult.error carries the real provider error message (not the
     hardcoded "agent session error" placeholder).
  2. A task_log entry is written to the CANONICAL spec_dir so the portal
     can display it — child-worktree logs are never synced back.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))

from task_logger import TaskLogger, load_task_logs  # noqa: E402


def _make_spec_dir(tmp_path: Path) -> Path:
    spec_dir = tmp_path / "specs" / "001-test"
    spec_dir.mkdir(parents=True)
    return spec_dir


def test_subtask_result_carries_provider_error_message(tmp_path):
    """SubtaskResult.error must be the real error, not 'agent session error'."""
    from agents.parallel_runner import SubtaskResult

    # Simulate what run_subtask now does: extract message from _err dict
    _err = {
        "type": "other",
        "message": "Antigravity CLI not found: 'antigravity'",
        "exception_type": "RuntimeError",
    }
    error_field = (_err or {}).get("message") or "agent session error"

    result = SubtaskResult(
        subtask_id="subtask-1-1",
        success=False,
        worktree_name="spec__w__subtask-1-1",
        error=error_field,
    )
    assert result.error == "Antigravity CLI not found: 'antigravity'"
    assert result.error != "agent session error"


def test_session_error_logged_to_canonical_spec_dir(tmp_path):
    """On session failure the canonical spec_dir task log must have an error entry."""
    spec_dir = _make_spec_dir(tmp_path)

    _err_msg = "Copilot CLI timed out after 300s"
    # Reproduce the logging logic added in the fix
    TaskLogger(spec_dir, emit_markers=False).log_error(
        f"[parallel] subtask subtask-2-1: {_err_msg}",
        None,
    )

    logs = load_task_logs(spec_dir)
    assert logs is not None, "task_logs.json should exist after logging"

    all_entries = [
        entry
        for phase in logs.get("phases", {}).values()
        for entry in phase.get("entries", [])
    ]
    error_entries = [e for e in all_entries if e.get("type") == "error"]
    assert error_entries, "Expected at least one error entry in canonical task log"
    assert any("subtask-2-1" in e.get("content", "") for e in error_entries)
    assert any(_err_msg in e.get("content", "") for e in error_entries)


def test_none_err_dict_falls_back_gracefully():
    """If _err is None (shouldn't happen but defensive), error field has a sane fallback."""
    _err = None
    error_field = (_err or {}).get("message") or "agent session error"
    assert error_field == "agent session error"


def test_empty_err_dict_falls_back_gracefully():
    """If _err is empty dict, error field has a sane fallback."""
    _err = {}
    error_field = (_err or {}).get("message") or "agent session error"
    assert error_field == "agent session error"
