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
        # Stage everything EXCEPT AIFactory's own bookkeeping. .aifactory-status
        # (the ccstatusline file) and .aifactory-security.json churn on every
        # subtask; once one of them slips into a commit they become tracked and
        # every later safety-net re-commits the churn, cluttering the branch with
        # "safety-net" commits (and leaving one as the branch tip). The net is
        # meant to rescue real uncommitted CODE the agent forgot, so exclude the
        # bookkeeping via pathspec. (.aifactory/ is already gitignored; excluded
        # here too for the case where it isn't.)
        subprocess.run(
            [
                "git",
                "add",
                "-A",
                "--",
                ".",
                ":(exclude).aifactory/",
                ":(exclude)aifactory/specs/",
                ":(exclude).aifactory-status",
                ":(exclude).aifactory-security.json",
            ],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        # If nothing (real) got staged, the only changes were bookkeeping churn —
        # don't manufacture a commit. `git diff --cached --quiet` exits 0 when the
        # index matches HEAD (nothing staged), 1 when there is staged work.
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False,  # returncode is the signal (0 = nothing staged)
        )
        if staged.returncode == 0:
            return None  # only bookkeeping changed — nothing to rescue

        msg = "chore(agent): safety-net commit of uncommitted changes"
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


def sync_memory_to_source(spec_dir: Path, source_spec_dir: Path | None) -> bool:
    """Sync the spec's ``memory/`` tree from the worktree back to the source.

    **The bug this closes (#1030): agent memory never survived a task.** Session
    insights are written to ``<worktree>/.aifactory/specs/<spec>/memory/``, and
    :func:`sync_plan_to_source` copies ``implementation_plan.json`` and nothing
    else — so ``memory/`` died with the worktree. Every one of the 8 insight
    files on the live cluster sat inside a worktree; none had ever reached a
    source spec directory.

    That made the whole subsystem inert across tasks. Sessions accumulate memory
    *within* one task (they share a worktree), then it is thrown away, so the
    second pass over a codebase costs exactly what the first did — the opposite
    of what memory exists for, and of RFC-0010's premise.

    **Why syncing here is sufficient.** ``copy_spec_to_worktree`` seeds each new
    worktree with ``shutil.copytree(source_spec_dir, target_spec_dir)`` — the
    WHOLE spec directory. So once memory reaches the source, the next task's
    worktree is seeded with it and the loop closes. No read-path change is
    needed, and adding one would be the wrong fix.

    Mirrors rather than replaces: files are copied worktree → source, and
    nothing in the source is deleted. A stale source file whose worktree
    counterpart vanished is left alone, because losing memory is the failure
    mode being fixed and this function should never be able to cause it.

    Returns True if anything was copied.
    """
    if not source_spec_dir:
        return False

    if spec_dir.resolve() == source_spec_dir.resolve():
        return False  # not in worktree mode; already writing to the durable path

    memory_dir = spec_dir / "memory"
    if not memory_dir.is_dir():
        return False

    target = source_spec_dir / "memory"
    try:
        # dirs_exist_ok so a re-sync merges into an existing store rather than
        # failing; copy2 preserves mtimes, which the expiry policy in RFC-0021
        # will want to trust.
        shutil.copytree(memory_dir, target, dirs_exist_ok=True)
        logger.debug(f"Synced memory to source: {target}")
        return True
    except Exception as e:
        # Never fatal: a build that produced working code must not fail because
        # its memory could not be filed.
        logger.warning(f"Failed to sync memory to source: {e}")
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
