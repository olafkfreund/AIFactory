"""
Utility Functions for Agent System
===================================

Helper functions for git operations, plan management, and file syncing.
"""

import asyncio
import json
import logging
import shutil
import subprocess
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from memory.paths import project_memory_dir, project_memory_dir_from_aifactory

logger = logging.getLogger(__name__)

# AIFactory's own runtime bookkeeping inside a task worktree. Never belongs in a
# task commit (and so never in the PR the Approve control opens) — see #1106.
# ``.aifactory-status`` is legacy: StatusManager now writes .aifactory/status.json,
# but a repo that already tracks the old root file still has it in the worktree.
_BOOKKEEPING_PATHS = (
    ".aifactory/",
    "aifactory/specs/",
    ".aifactory-status",
    ".aifactory-security.json",
)


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
        # Stage everything, then UNSTAGE AIFactory's own bookkeeping.
        # .aifactory-status (the ccstatusline file) and .aifactory-security.json
        # churn on every subtask; once one of them slips into a commit they
        # become tracked and every later safety-net re-commits the churn — and
        # in a repo where one is already tracked, that lands a factory-internal
        # file in the PR and conflicts with the base (#1106). The net is meant to
        # rescue real uncommitted CODE the agent forgot.
        #
        # Two steps rather than one `git add -A -- . :(exclude)...`: the
        # exclude-pathspec form failed outright in the pod (#1106, non-zero exit
        # from git add), which took the WHOLE add down and rescued nothing.
        # `git add -A` cannot fail on a pathspec, and `git reset` on paths that
        # are absent from the index is a no-op, so the unstage step is safe
        # whether or not the bookkeeping is present or tracked.
        subprocess.run(
            ["git", "add", "-A"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "reset", "--quiet", "HEAD", "--", *_BOOKKEEPING_PATHS],  # noqa: S607
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False,  # absent paths / an unborn HEAD must not break the net
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
        # ERROR, not warning: this is the net that stops agent work being lost,
        # so "it silently did not run" is itself a hazard (#1106). Still
        # non-raising — the caller's build may be fine and failing it here would
        # trade a possible loss for a certain one.
        stderr = getattr(exc, "stderr", "") or ""
        logger.error(
            "safety-net commit FAILED in %s (uncommitted agent work is NOT "
            "protected): %s %s",
            project_dir,
            exc,
            stderr.strip()[:500],
        )
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


# Plan publishes run here when a loop is running (#1228). ONE worker, and that
# is the point rather than a saving: pushes are serialised in submission order,
# so a slow upload cannot land an OLDER plan on top of a newer one and leave the
# cockpit's DAG reading backwards until the next transition. Threads are created
# on first submit, so this costs nothing in a process that never publishes.
_PUBLISH_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="plan-publish")


def publish_plan(spec_dir: Path) -> None:
    """Push this spec's plan to object storage mid-build, for the DAG (#1228).

    ``maybe_push_plan`` already existed and was already called with exactly
    these arguments — once, from ``cli/main.py`` after the build returns. That
    is why CFactory's live execution diagram showed every node ``waiting`` for a
    whole build and then flipped all of them to done in one step: per-subtask
    ``status``/``started_at`` live in ``implementation_plan.json``, which on the
    packed path is written inside the Job's ephemeral ``/work``. Calling the
    same push on each transition is the whole fix; no new transport, no new
    protocol, and the control plane's throttled pull
    (``KubeJobLogStreamer._maybe_sync_plan``) is the other half.

    A no-op off the packed path, where the spec dir is the control plane's own.
    Best-effort and silent: this is progress reporting, and a build must never
    fail because a status could not be published.

    **Never blocks an event loop.** The push is object-store I/O — a PUT, and a
    boto3 client construction per call — and the funnels this hangs off run
    inside one: ``apply_subtask_status_update`` is a coroutine, and
    ``sync_plan_to_source`` is called from the wave orchestrator's async
    ``run_subtask``/``_reset_to_pending``. Blocking there stalls every
    concurrent subtask agent in the wave, so with a loop running the push is
    handed to a worker and NOT awaited: nothing downstream needs the upload's
    result, and the final synchronous push in ``cli/main.py`` is what guarantees
    the terminal plan lands regardless.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _publish_plan_now(spec_dir)  # no loop to protect — push inline
    else:
        loop.run_in_executor(_PUBLISH_POOL, _publish_plan_now, spec_dir)


def _publish_plan_now(spec_dir: Path) -> None:
    """The blocking half of :func:`publish_plan`. Never raises."""
    try:
        from core.workspace_fetch import maybe_push_plan  # noqa: PLC0415

        maybe_push_plan(spec_dir, spec_dir.name)
    except Exception:  # noqa: BLE001 - reporting must never fail a build
        logger.debug("plan publish skipped (best-effort)", exc_info=True)


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
    # #1228: publish ``spec_dir``'s copy, and do it before the worktree-mode
    # early-returns below rather than after the copy.
    #
    # ``spec_dir`` is by construction the directory the caller just wrote, so it
    # is the freshest copy in every mode — publishing the SOURCE would publish
    # the pre-copy one in worktree mode. And it must run above the early-returns
    # because those are precisely the non-worktree builds, which advance the
    # plan just the same; on the packed path the whole Job filesystem — worktree
    # AND source — is an ephemeral emptyDir, so reaching the source spec dir is
    # not reaching the control plane.
    publish_plan(spec_dir)

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
    # Narrow rather than blind: these are what copytree can actually raise
    # (shutil.Error aggregates per-file failures, OSError covers the rest). A
    # bare `except Exception` would also swallow a programming error here and
    # report it as "memory could not be filed", which is the kind of quiet
    # mislabelling that hid this bug in the first place.
    except (OSError, shutil.Error) as e:
        # Never fatal: a build that produced working code must not fail because
        # its memory could not be filed.
        logger.warning(f"Failed to sync memory to source: {e}")
        return False


def sync_memory_to_project(spec_dir: Path, source_spec_dir: Path | None) -> bool:
    """Mirror a spec's ``memory/`` into the PROJECT's durable store (RFC-0021 P0).

    Spec-scoped memory survives worktree teardown (#1030) and then has almost
    nothing to read it: a project holds many specs, each is built about once, so
    the next build looks in a different directory. This is the half that makes a
    lesson from spec 034 reachable by spec 041.

    Merges, never replaces — the project store is the accumulation of every
    spec's insights, and no single spec may clear it. Never raises, for the same
    reason :func:`sync_memory_to_source` does not: a build that produced working
    code must not fail because its memory could not be filed.
    """
    if not source_spec_dir:
        return False

    memory_dir = spec_dir / "memory"
    if not memory_dir.is_dir():
        return False

    # Anchored on source_spec_dir, NOT project_dir. Inside a build Job,
    # `project_dir` is the EPHEMERAL clone under the pod's emptyDir, so pooling
    # there writes to a filesystem that dies with the pod — the original #1030
    # bug one level out, and a live Job build is the only thing that revealed it
    # (the first release wrote 0 files while its neighbour wrote 6).
    #
    # source_spec_dir is `<project>/.aifactory/specs/<id>` on the co-mounted
    # durable volume — proven durable by that same run — so its grandparent is
    # the project's `.aifactory/` and the store belongs beside `specs/`.
    project_root_aifactory = source_spec_dir.parent.parent

    try:
        shutil.copytree(
            memory_dir,
            project_memory_dir_from_aifactory(project_root_aifactory),
            dirs_exist_ok=True,
        )
        return True
    except (OSError, shutil.Error) as e:
        logger.warning(f"Failed to sync memory to the project store: {e}")
        return False


def seed_memory_from_project(project_dir: Path | None, spec_dir: Path) -> bool:
    """Seed a fresh worktree's spec ``memory/`` from the project store (RFC-0021 P0).

    The read half. The agent's filesystem is confined to its worktree, so the
    project store has to be copied IN before a build starts or it may as well
    not exist.

    Seeded rather than replaced: anything the spec already carries wins, because
    a spec's own history is more specific than the project's pooled memory.
    """
    if not project_dir:
        return False

    try:
        src = project_memory_dir(project_dir)
        if not any(src.iterdir()):
            return False
        target = spec_dir / "memory"
        # dirs_exist_ok merges; the spec's own files are written after this and
        # therefore win on any collision.
        shutil.copytree(src, target, dirs_exist_ok=True)
        return True
    except (OSError, shutil.Error) as e:
        logger.warning(f"Failed to seed memory from the project store: {e}")
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


def record_subtask_started(
    subtask_ids: Iterable[str], plan_path: Path, source_spec_dir: Path | None
) -> int:
    """Stamp ``started_at`` on a wave's subtasks in the canonical plan (#1195).

    The wave engine's counterpart to :func:`record_subtask_completion`. Without
    it the wave path never wrote ``started_at`` at all, so CFactory's live
    execution diagram could not classify a running subtask as active
    (``taskFlow.flowStatus``) and its per-node timer chip was always blank
    (``nodeElapsedSeconds`` returns null with no start).

    Batched per WAVE, not per subtask: a wave is by definition the set that
    starts together, so one plan write per wave is both accurate and cheap.

    Called from the parent's ``on_wave`` hook, which runs BEFORE the wave's
    ``asyncio.gather`` and after the previous wave's completions have been
    written — i.e. on the same single-threaded, parent-owned step as every
    other canonical plan mutation (concurrency invariant #3), so it needs no
    lock of its own.

    Best-effort bookkeeping: returns the number of subtasks stamped, and never
    raises. A build must not fail because a timestamp could not be recorded.

    Returns:
        How many subtasks were found and stamped (0 when the plan is missing).
    """
    # Lazy, exactly as record_subtask_completion above: agents.utils is imported
    # by the plan package's own callers, so a top-level import here cycles.
    from implementation_plan.plan import ImplementationPlan  # noqa: PLC0415

    target = plan_path
    if not target.exists() and source_spec_dir is not None:
        fallback = source_spec_dir / "implementation_plan.json"
        if fallback.exists():
            target = fallback
    if not target.exists():
        logger.warning(
            "record_subtask_started: no implementation_plan.json at %s (nor "
            "source %s); start times for %s not recorded",
            plan_path,
            source_spec_dir,
            list(subtask_ids),
        )
        return 0

    wanted = set(subtask_ids)
    if not wanted:
        return 0

    try:
        plan = ImplementationPlan.load(target)
        stamped = 0
        for phase in plan.phases:
            for subtask in phase.subtasks:
                if subtask.id in wanted:
                    subtask.start()
                    stamped += 1
        if stamped:
            plan.save(target)
            sync_plan_to_source(target.parent, source_spec_dir)
        return stamped
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not fail a build
        logger.warning("record_subtask_started: could not stamp start times: %s", exc)
        return 0
