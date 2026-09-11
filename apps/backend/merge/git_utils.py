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

logger = logging.getLogger(__name__)

# `git show <ref>:<path>` exits 128 both when the path is legitimately absent
# at that ref AND when the read genuinely failed (bad ref, corrupt object,
# lock contention, I/O error, ...). Both cases raise the same
# CalledProcessError, so the exit code alone can't tell them apart -- only
# the stderr text does. Confirmed against a real repo:
#   - missing path, valid ref:   "fatal: path '<p>' does not exist in '<ref>'"
#   - path on disk, uncommitted: "fatal: path '<p>' exists on disk, but not in '<ref>'"
#   - bad/unknown ref:           "fatal: invalid object name '<ref>'."
_MISSING_PATH_MARKERS = ("does not exist in", "exists on disk, but not in")


def is_missing_path_error(stderr: str) -> bool:
    """True only for git's "the path is absent at this ref" messages."""
    return any(marker in stderr for marker in _MISSING_PATH_MARKERS)


class GitReadError(Exception):
    """Raised when `git show` fails for a reason other than a missing path.

    Never treat this the same as "file doesn't exist" -- callers must not
    fall back to an empty/new-file baseline on this error.
    """


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
