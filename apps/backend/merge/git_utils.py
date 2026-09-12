"""
Git Utilities
==============

Helper functions for git operations used in merge orchestration.

This module provides utilities for:
- Finding git worktrees
- Getting file content from branches
- Working with git repositories
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

# NOTE: GitReadError / is_missing_path_error live in timeline_git, not here.
# That module is allowlisted stdlib-only (#1089) and is exec'd straight off
# disk with spec_from_file_location by test_control_plane_reads_the_pushed_work,
# which gives it NO package context -- so it cannot import from this package
# at all. This module has no such constraint, so the dependency points that way.
from .timeline_git import GitReadError, is_missing_path_error

# Re-exported deliberately: callers import these from here, the natural home
# for git helpers, while the definitions must live in the stdlib-only module.
# __all__ rather than `as` aliases -- mypy --strict wants an explicit export,
# ruff --strict flags the alias form as PLC0414, and __all__ satisfies both.
__all__ = ["GitReadError", "is_missing_path_error"]

logger = logging.getLogger(__name__)


def find_worktree(project_dir: Path, task_id: str) -> Path | None:
    """
    Find the worktree path for a task.

    Args:
        project_dir: The project root directory
        task_id: The task identifier

    Returns:
        Path to the worktree, or None if not found
    """
    # Check new path first
    new_worktrees_dir = project_dir / ".aifactory" / "worktrees" / "tasks"
    if new_worktrees_dir.exists():
        for entry in new_worktrees_dir.iterdir():
            if entry.is_dir() and task_id in entry.name:
                return entry

    # Legacy fallback for backwards compatibility
    legacy_worktrees_dir = project_dir / ".worktrees"
    if legacy_worktrees_dir.exists():
        for entry in legacy_worktrees_dir.iterdir():
            if entry.is_dir() and task_id in entry.name:
                return entry

    return None


def get_file_from_branch(project_dir: Path, file_path: str, branch: str) -> str | None:
    """
    Get file content from a specific git branch.

    Args:
        project_dir: The project root directory
        file_path: Path to the file relative to project root
        branch: Branch name

    Returns:
        File content as string, or None if the file doesn't exist on the branch

    Raises:
        GitReadError: `git show` failed for a reason other than the file
            being absent at that ref (bad ref, corrupt repo, lock
            contention, I/O error, ...). Callers must not treat this the
            same as "file doesn't exist" -- doing so silently invents an
            empty merge baseline and can lose existing content.
    """
    result = subprocess.run(
        ["git", "show", f"{branch}:{file_path}"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout
    if is_missing_path_error(result.stderr):
        return None
    logger.error(
        "git show %s:%s failed (exit %d): %s",
        branch,
        file_path,
        result.returncode,
        result.stderr.strip(),
    )
    raise GitReadError(
        f"git show {branch}:{file_path} failed (exit {result.returncode}): "
        f"{result.stderr.strip()}"
    )
