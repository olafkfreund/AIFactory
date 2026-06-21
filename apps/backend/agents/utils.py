"""
Utility Functions for Agent System
===================================

Helper functions for git operations, plan management, and file syncing.
"""

import json
import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def get_latest_commit(project_dir: Path) -> str | None:
    """Get the hash of the latest git commit."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None


def get_commit_count(project_dir: Path) -> int:
    """Get the total number of commits."""
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        return int(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        return 0


def commit_uncommitted_changes(
    project_dir: Path, subtask_id: str | None = None
) -> str | None:
    """Safety-net commit of any uncommitted agent-written changes (#611 g).

    The coding agent commits its own work, but if it finishes with files written
    and NOT committed, a later post-run bookkeeping step that aborts (or a
    worktree teardown) can lose that work — the 2026-06-18 demo nearly lost
    ``app/main.py`` this way. This stages everything in the worktree and commits
    it so the work survives regardless of what bookkeeping does next.

    Fully defensive: returns the new commit hash, or ``None`` when there was
    nothing to commit or the commit could not be made (never raises).
    """
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        if not status.stdout.strip():
            return None  # clean tree — agent already committed everything

        subprocess.run(
            ["git", "add", "-A"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        msg = "chore(agent): safety-net commit of uncommitted session changes"
        if subtask_id:
            msg += f" [{subtask_id}]"
        subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        return get_latest_commit(project_dir)
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.warning("safety-net commit failed in %s: %s", project_dir, exc)
        return None


def load_implementation_plan(spec_dir: Path) -> dict | None:
    """Load the implementation plan JSON."""
    plan_file = spec_dir / "implementation_plan.json"
    if not plan_file.exists():
        return None
    try:
        with open(plan_file) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def find_subtask_in_plan(plan: dict, subtask_id: str) -> dict | None:
    """Find a subtask by ID in the plan."""
    for phase in plan.get("phases", []):
        for subtask in phase.get("subtasks", []):
            if subtask.get("id") == subtask_id:
                return subtask
    return None


def find_phase_for_subtask(plan: dict, subtask_id: str) -> dict | None:
    """Find the phase containing a subtask."""
    for phase in plan.get("phases", []):
        for subtask in phase.get("subtasks", []):
            if subtask.get("id") == subtask_id:
                return phase
    return None


def sync_plan_to_source(spec_dir: Path, source_spec_dir: Path | None) -> bool:
    """
    Sync implementation_plan.json from worktree back to source spec directory.

    When running in isolated mode (worktrees), the agent updates the implementation
    plan inside the worktree. This function syncs those changes back to the main
    project's spec directory so the frontend/UI can see the progress.

    Args:
        spec_dir: Current spec directory (may be inside worktree)
        source_spec_dir: Original spec directory in main project (outside worktree)

    Returns:
        True if sync was performed, False if not needed or failed
    """
    # Skip if no source specified or same path (not in worktree mode)
    if not source_spec_dir:
        return False

    # Resolve paths and check if they're different
    spec_dir_resolved = spec_dir.resolve()
    source_spec_dir_resolved = source_spec_dir.resolve()

    if spec_dir_resolved == source_spec_dir_resolved:
        return False  # Same directory, no sync needed

    # Sync the implementation plan
    plan_file = spec_dir / "implementation_plan.json"
    if not plan_file.exists():
        return False

    source_plan_file = source_spec_dir / "implementation_plan.json"

    try:
        shutil.copy2(plan_file, source_plan_file)
        logger.debug(f"Synced implementation plan to source: {source_plan_file}")
        return True
    except Exception as e:
        logger.warning(f"Failed to sync implementation plan to source: {e}")
        return False


def record_subtask_completion(
    subtask_id: str, plan_path: Path, source_spec_dir: Path | None
) -> bool:
    """Mark a subtask completed in the canonical implementation plan + sync it.

    The parallel coder records completion via the parent's ``plan_path``
    (``spec_dir/implementation_plan.json``). When that worktree spec dir holds no
    plan, a bare ``ImplementationPlan.load(plan_path)`` throws and the caller
    swallows it — so completion is silently lost, the canonical plan stays at 0
    completed, and the finalize step reports a SUCCESSFUL build as ``failed``.
    Fall back to the canonical ``source_spec_dir`` plan in that case, and never
    fail silently: return False (the caller logs) when no plan can be found.

    Returns True iff a subtask was found and its completion persisted.
    """
    from implementation_plan.plan import ImplementationPlan

    target = plan_path
    if not target.exists() and source_spec_dir is not None:
        fallback = source_spec_dir / "implementation_plan.json"
        if fallback.exists():
            target = fallback
    if not target.exists():
        logger.error(
            "record_subtask_completion: no implementation_plan.json at %s "
            "(nor source %s) — subtask %s completion NOT recorded; the build may "
            "be falsely reported failed",
            plan_path,
            source_spec_dir,
            subtask_id,
        )
        return False

    plan = ImplementationPlan.load(target)
    found = False
    for phase in plan.phases:
        for subtask in phase.subtasks:
            if subtask.id == subtask_id:
                subtask.complete()
                found = True
    if not found:
        return False

    plan.save(target)
    # Propagate the worktree plan to the canonical source (no-op when target is
    # already the source).
    sync_plan_to_source(target.parent, source_spec_dir)
    return True
