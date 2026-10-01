#!/usr/bin/env python3
"""
Tests for the "unreadable is not empty" merge-baseline fix.

`git show <ref>:<path>` exits 128 both when a path is legitimately absent
at that ref AND when the read genuinely failed (bad ref, corrupt repo,
lock contention, I/O error, ...). Both git_utils.get_file_from_branch and
TimelineGitHelper.get_file_content_at_commit used to collapse both cases
to `None`, and every caller then turned `None` into `""` -- a false "new
file" baseline that can silently drop existing content during a 3-way
merge.

Covers:
- git_utils.get_file_from_branch: absent path -> None, genuine failure -> GitReadError
- TimelineGitHelper.get_file_content_at_commit: same distinction
- orchestrator._merge_file: a genuine read failure fails the file loudly,
  it never falls back to an empty/new-file baseline
- timeline_tracker.on_task_start: a genuine read failure skips registering
  that file rather than recording a wrong empty branch-point baseline
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add aifactory directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "Apps" / "backend"))
# Add tests directory to path for test_fixtures
sys.path.insert(0, str(Path(__file__).parent))

from merge import MergeOrchestrator
from merge.git_utils import GitReadError, get_file_from_branch
from merge.timeline_git import TimelineGitHelper
from merge.timeline_tracker import FileTimelineTracker
from merge.types import MergeDecision


def _fake_run(returncode: int, stderr: str, stdout: str = ""):
    return subprocess.CompletedProcess(
        args=["git", "show"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestGetFileFromBranch:
    """git_utils.get_file_from_branch must distinguish absent vs unreadable."""

    def test_absent_path_returns_none(self, temp_project):
        """A path that never existed at a valid ref is legitimately absent."""
        result = get_file_from_branch(temp_project, "does/not/exist.py", "main")
        assert result is None

    def test_existing_path_returns_content(self, temp_project):
        result = get_file_from_branch(temp_project, "src/utils.py", "main")
        assert result is not None
        assert len(result) > 0

    def test_genuine_failure_raises_git_read_error(self, temp_project):
        """A bad ref is a real failure, not an absent file -- must not be None."""
        with patch(
            "merge.git_utils.subprocess.run",
            return_value=_fake_run(128, "fatal: invalid object name 'bogus-ref'.\n"),
        ):
            with pytest.raises(GitReadError):
                get_file_from_branch(temp_project, "src/utils.py", "bogus-ref")


class TestGetFileContentAtCommit:
    """TimelineGitHelper.get_file_content_at_commit: same distinction."""

    def test_absent_path_returns_none(self, temp_project):
        helper = TimelineGitHelper(temp_project)
        result = helper.get_file_content_at_commit("does/not/exist.py", "HEAD")
        assert result is None

    def test_genuine_failure_raises_git_read_error(self, temp_project):
        helper = TimelineGitHelper(temp_project)
        with patch(
            "merge.timeline_git.subprocess.run",
            return_value=_fake_run(128, "fatal: invalid object name 'bogus'.\n"),
        ):
            with pytest.raises(GitReadError):
                helper.get_file_content_at_commit("src/utils.py", "bogus")


class TestOrchestratorRefusesInventedBaseline:
    """orchestrator._merge_file must not merge against a fabricated baseline."""

    def test_unreadable_baseline_fails_the_file_not_silently_empty(self, temp_project):
        orchestrator = MergeOrchestrator(temp_project)

        class _Snapshot:
            task_id = "task-001"
            semantic_changes = []

        with patch(
            "merge.orchestrator.get_file_from_branch",
            side_effect=GitReadError("git show main:src/utils.py failed (exit 128)"),
        ):
            result = orchestrator._merge_file(
                file_path="src/utils.py",
                task_snapshots=[_Snapshot()],
                target_branch="main",
            )

        assert result.decision == MergeDecision.FAILED
        assert result.error is not None
        assert "src/utils.py" in result.error
        # Must not have proceeded to merge against an invented "" baseline.
        assert result.merged_content is None


class TestTimelineTrackerRefusesInventedBaseline:
    """on_task_start must not record a wrong empty branch-point baseline."""

    def test_unreadable_branch_point_skips_registration(self, temp_project):
        tracker = FileTimelineTracker(temp_project)

        with patch.object(
            tracker.git,
            "get_file_content_at_commit",
            side_effect=GitReadError("git show HEAD:src/utils.py failed (exit 128)"),
        ):
            tracker.on_task_start(
                task_id="task-001",
                files_to_modify=["src/utils.py"],
                branch_point_commit="HEAD",
            )

        # No timeline should have been persisted for this file: registering
        # it with a fabricated "" baseline would be worse than not
        # registering it at all.
        timeline = tracker._timelines.get("src/utils.py")
        assert timeline is None or timeline.get_task_view("task-001") is None

    def test_readable_branch_point_registers_normally(self, temp_project):
        """Control case: a real read still registers the task view."""
        tracker = FileTimelineTracker(temp_project)

        tracker.on_task_start(
            task_id="task-001",
            files_to_modify=["src/utils.py"],
            branch_point_commit="HEAD",
        )

        timeline = tracker._timelines.get("src/utils.py")
        assert timeline is not None
        task_view = timeline.get_task_view("task-001")
        assert task_view is not None
        assert len(task_view.branch_point.content) > 0
