"""Worktree file-sync — WorktreeSyncMixin (#703).

_sync_worktree_files (copies a build's worktree spec dir back to the main spec
dir for frontend visibility, with throttled progress emission), lifted out of
the AgentService god-class into a mixin. AgentService inherits this mixin;
methods run as bound methods via the MRO.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from server.specpath import safe_spec_component as _safe_spec_component

from ..websockets.events import emit_subtask_update
from . import task_control
from .task_phase import TaskPhase, scale_progress
from .task_status import read_plan

if TYPE_CHECKING:
    from collections.abc import Callable

_log = logging.getLogger(__name__)

# Live running-cost throttle (seconds). The worktree sync ticks ~every 3s; emit a
# usage snapshot from the worktree's live token_usage.json at most once per this
# window so the cockpit shows accruing cost WHILE a build runs without flooding
# the completion webhook. Env-overridable for ops/tests.
_USAGE_EMIT_WINDOW_S = 15.0





class WorktreeSyncMixin:
    """Worktree-to-main spec-dir file sync for AgentService."""

    if TYPE_CHECKING:
        # Attributes/methods provided by the concrete host (AgentService) and
        # sibling mixins; declared so mypy resolves the self.* refs (#703 pattern).
        _task_build_progress_offset: dict[str, Any]
        _task_current_phases: dict[str, Any]
        _task_subtask_states: dict[str, Any]
        _task_usage_emit_ts: dict[str, float]
        _safe_emit_task_update: Callable[..., Any]

    async def _sync_worktree_files(
        self, project_path: Path, spec_id: str, task_id: str | None = None
    ) -> None:
        """Sync files from worktree spec dir to main spec dir for frontend visibility.

        Args:
            project_path: Path to the project
            spec_id: Spec directory name (e.g., "001-fix-bug")
            task_id: Full task ID (project_id:spec_id) for consistent tracking. Falls back to spec_id if not provided.
        """
        # Use task_id for tracking if provided, otherwise fall back to spec_id for backwards compatibility
        tracking_key = task_id or spec_id
        import logging

        logger = logging.getLogger(__name__)

        # Paths
        # Barrier BEFORE the value reaches any path expression.
        spec_id = _safe_spec_component(spec_id)

        worktree_spec = (
            project_path
            / ".aifactory"
            / "worktrees"
            / "tasks"
            / spec_id
            / ".aifactory"
            / "specs"
            / spec_id
        )
        main_spec = project_path / ".aifactory" / "specs" / spec_id

        # Ensure main spec dir exists
        main_spec.mkdir(parents=True, exist_ok=True)

        # Files to sync (in order of priority)
        files_to_sync = [
            "implementation_plan.json",  # Most critical for UI
            "task_logs.json",  # Detailed phase logs for UI
            "build-progress.txt",
            "context.json",
            "qa_report.md",
            "QA_FIX_REQUEST.md",
            "spec.md",
            "requirements.json",
        ]

        # NOTE: task_control.json and qa_review_cycle.json are deliberately
        # ABSENT from files_to_sync. Both are authoritative state owned outside
        # the agent's worktree (control-plane #259, QA review-cycle #260) and a
        # worktree copy must never reset or replay them.

        # Directories to sync (merged into the destination, see below).
        #
        # TWO mechanisms write `memory/` back, deliberately, and they do
        # different jobs (#1033):
        #
        #   * THIS one is a VISIBILITY MIRROR. It ticks every few seconds while
        #     an in-pod build runs, so `routes/context.py` can show session
        #     insights before the build finishes. It only covers builds this
        #     process monitors.
        #   * `agents/utils.sync_memory_to_source` is the DURABLE WRITE. It runs
        #     inside the build itself, so it also covers Job-dispatched builds
        #     that no monitor is watching, and it is what makes memory survive
        #     worktree teardown (#1030).
        #
        # Both MERGE, so running both is harmless and order does not matter. If
        # a third is ever added, make it merge too — see the note on the loop.
        dirs_to_sync = [
            "memory",  # Session insights and memory data
        ]

        synced_count = 0
        for filename in files_to_sync:
            src = worktree_spec / filename
            dst = main_spec / filename
            if src.exists():
                # #1069: fail at WRITE time, where the cause is one line away.
                # A build that emits unparseable JSON must not have that file
                # copied into the spec dir the control plane reads; the last
                # good copy is strictly better than garbage, and the read side
                # only ever saw the fault hours later as a status discrepancy.
                if filename == "implementation_plan.json":
                    _, plan_error = read_plan(src)
                    if plan_error is not None:
                        _log.error(
                            "[AgentService] refusing to sync %s: %s", src, plan_error
                        )
                        continue
                try:
                    # For implementation_plan.json we still merge SUBTASK status
                    # forward-only (a legitimate agent-artifact concern), but we
                    # NO LONGER preserve control-plane status/reviewReason here.
                    #
                    # Issue #259: control-plane state (board column / task status
                    # / reviewReason) now lives in the dedicated, agent-immutable
                    # task_control.json store. We STRIP those fields from the
                    # worktree copy so an agent sync can never reset the
                    # human/system control decision — replacing the brittle
                    # "preserve-then-fall-back-to-raw-copy" workaround that, on
                    # any merge error, used to clobber the control state.
                    if filename == "implementation_plan.json" and dst.exists():
                        try:
                            main_plan = json.loads(dst.read_text())
                            worktree_plan = json.loads(src.read_text())

                            # Build map of main spec subtask statuses
                            STATUS_ORDER = {
                                "pending": 0,
                                "in_progress": 1,
                                "completed": 2,
                                "failed": 2,
                            }
                            main_subtask_statuses = {}
                            for phase in main_plan.get("phases", []):
                                for subtask in phase.get("subtasks", []):
                                    sid = subtask.get("id")
                                    if sid:
                                        main_subtask_statuses[sid] = subtask.get(
                                            "status", "pending"
                                        )

                            # Start from worktree plan (has latest structure)
                            merged_plan = worktree_plan

                            # Control-plane fields never belong in the plan file
                            # anymore — drop them so the reader can't pick a stale
                            # agent value over the dedicated control store.
                            task_control.strip_control_fields(merged_plan)

                            # Prevent subtask status regressions
                            for phase in merged_plan.get("phases", []):
                                for subtask in phase.get("subtasks", []):
                                    sid = subtask.get("id")
                                    if sid and sid in main_subtask_statuses:
                                        main_rank = STATUS_ORDER.get(
                                            main_subtask_statuses[sid], 0
                                        )
                                        wt_rank = STATUS_ORDER.get(
                                            subtask.get("status", "pending"), 0
                                        )
                                        if main_rank > wt_rank:
                                            subtask["status"] = main_subtask_statuses[
                                                sid
                                            ]

                            dst.write_text(json.dumps(merged_plan, indent=2))
                        except (json.JSONDecodeError, OSError) as merge_err:
                            # Even the error path must not reintroduce control
                            # fields: strip them before copying the raw worktree
                            # plan in.
                            logger.warning(
                                f"[AgentService] Failed to merge implementation_plan.json, falling back to stripped copy: {merge_err}"
                            )
                            try:
                                raw = json.loads(src.read_text())
                                task_control.strip_control_fields(raw)
                                dst.write_text(json.dumps(raw, indent=2))
                            except (json.JSONDecodeError, OSError):
                                # Last resort: a raw copy. Control state is still
                                # safe because the reader trusts task_control.json
                                # over the plan file.
                                shutil.copy2(src, dst)
                    else:
                        shutil.copy2(src, dst)
                    synced_count += 1
                except Exception as e:
                    logger.warning(f"[AgentService] Failed to sync {filename}: {e}")

        # Sync any additional files created by the agent (e.g., plan .md files)
        # that aren't in the hardcoded list
        try:
            known_files = set(files_to_sync)
            for src_file in worktree_spec.iterdir():
                if src_file.is_file() and src_file.name not in known_files:
                    try:
                        shutil.copy2(src_file, main_spec / src_file.name)
                        synced_count += 1
                    except Exception as e:
                        logger.warning(
                            f"[AgentService] Failed to sync extra file {src_file.name}: {e}"
                        )
        except OSError as e:
            logger.warning(
                f"[AgentService] Failed to scan worktree spec dir for extra files: {e}"
            )

        # Sync directories — MERGE, never replace (#1033).
        #
        # This used to `rmtree(dst_dir)` and copy fresh. For `memory/` that is a
        # data-loss bug: the destination is a MEMORY STORE that accumulates
        # across tasks, and wiping it discards anything written since this
        # worktree was seeded. Under RFC-0016 concurrency two tasks can build the
        # same spec, and the slower one's replace would silently delete the
        # faster one's session insights — the exact failure #1030 was about.
        #
        # Replace was safe only under the assumption that the worktree copy is
        # always a superset of the destination, which holds at seed time and
        # stops holding the moment anything else writes.
        #
        # Merging costs nothing here: every file this syncs is either identical
        # or newer in the worktree, so copy2's overwrite still wins for the
        # files this build owns.
        for dirname in dirs_to_sync:
            src_dir = worktree_spec / dirname
            dst_dir = main_spec / dirname
            if src_dir.exists() and src_dir.is_dir():
                try:
                    shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
                    synced_count += 1
                except Exception as e:
                    logger.warning(
                        f"[AgentService] Failed to sync directory {dirname}: {e}"
                    )

        # RFC-0021 Phase 0, corrected: pool this spec's memory at PROJECT level
        # HERE, in the control plane.
        #
        # The in-build sync (agents/utils.sync_memory_to_project) cannot do it
        # for a Job-dispatched build: inside the Job every path — including
        # source_spec_dir — is under /work, the pod's emptyDir, so anything it
        # writes dies with the pod. A live build proved it: the project pool
        # stayed at 0 files across two runs while this mirror carried 6 and 4
        # files to the PVC. THIS function is the only component that knows the
        # real durable project path.
        #
        # Merged, never replaced: the pool is the union of every spec's
        # insights, and one spec may not clear it.
        try:
            project_memory = project_path / ".aifactory" / "memory"
            spec_memory = main_spec / "memory"
            if spec_memory.is_dir():
                project_memory.mkdir(parents=True, exist_ok=True)
                shutil.copytree(spec_memory, project_memory, dirs_exist_ok=True)
        except (OSError, shutil.Error) as e:
            # Never fatal: a build that produced working code must not fail
            # because its memory could not be pooled.
            logger.warning(f"[AgentService] Failed to pool memory at project level: {e}")

        if synced_count > 0:
            logger.debug(
                f"[AgentService] Synced {synced_count} files from worktree to main spec dir"
            )

        # Tier B auto-reload — stream new build-progress.txt lines as task:log
        # events.  The agent appends a human-readable narrative ("Starting
        # phase 1: PROJECT DISCOVERY", "Discovered 22 files", "Working on
        # 1.1 — ...") that, until now, only the full-page-reload `getTask`
        # endpoint surfaced.  Tailing the delta on each sync tick lets the
        # kanban detail view scroll the narrative in real time.
        if task_id:
            try:
                bp_main = main_spec / "build-progress.txt"
                if bp_main.exists():
                    current_size = bp_main.stat().st_size
                    prev_offset = self._task_build_progress_offset.get(task_id, 0)
                    # If the file was truncated/restarted, reset to 0 rather
                    # than re-reading nonsense from a stale offset.
                    if current_size < prev_offset:
                        prev_offset = 0
                    if current_size > prev_offset:
                        with bp_main.open(
                            "r", encoding="utf-8", errors="replace"
                        ) as fh:
                            fh.seek(prev_offset)
                            new_text = fh.read()
                        self._task_build_progress_offset[task_id] = current_size
                        # Emit one task:log per non-empty line so the frontend
                        # batches them at its 16-ms tick (useIpc.ts:191).
                        from ..websockets.events import emit_task_log

                        for line in new_text.splitlines():
                            stripped = line.rstrip()
                            if stripped:
                                await emit_task_log(task_id, stripped)
            except Exception as e:
                logger.debug(f"[AgentService] build-progress tail emit failed: {e}")

        # Always check for subtask status changes and emit WebSocket updates
        # This runs independently of file sync to ensure real-time updates
        try:
            # Read implementation plan for progress info
            plan_file = main_spec / "implementation_plan.json"
            if plan_file.exists():
                plan = json.loads(plan_file.read_text())

                # Calculate progress from subtasks in phases
                all_subtasks = []
                current_phase = None
                for phase in plan.get("phases", []):
                    if phase.get("status") == "in_progress":
                        current_phase = phase.get("name")
                    all_subtasks.extend(phase.get("subtasks", []))

                completed = sum(
                    1 for s in all_subtasks if s.get("status") == "completed"
                )
                total = len(all_subtasks)
                progress = int((completed / total) * 100) if total > 0 else 0

                # Find current subtask
                current_subtask = None
                for s in all_subtasks:
                    if s.get("status") == "in_progress":
                        current_subtask = s.get("description", s.get("id"))
                        break

                # Build subtasks array for real-time frontend updates
                subtasks_data = [
                    {"id": s.get("id"), "status": s.get("status")} for s in all_subtasks
                ]

                # Detect individual subtask status changes and emit granular events
                # This enables real-time subtask checkbox updates in the frontend
                previous_states = self._task_subtask_states.get(tracking_key, {})
                current_states = {s.get("id"): s.get("status") for s in all_subtasks}

                # Check for changes and emit individual events
                has_changes = False
                for subtask_id, current_status in current_states.items():
                    previous_status = previous_states.get(subtask_id)
                    if previous_status != current_status:
                        has_changes = True
                        # Subtask status changed - emit granular event
                        # Use task_id (projectId:specId format) so frontend can match
                        await emit_subtask_update(
                            task_id=task_id or spec_id,
                            subtask_id=subtask_id,
                            status=current_status,
                            previous_status=previous_status,
                        )

                # Update tracking for next comparison
                self._task_subtask_states[tracking_key] = current_states

                # Emit task update if subtasks changed OR worktree files were
                # synced. The ``force`` flag tells _safe_emit_task_update to
                # bypass the structural dedup when ``synced_count > 0`` —
                # otherwise long subtasks where phase/progress/subtask-status
                # haven't moved yet would suppress every 3-sec heartbeat and
                # the kanban board freezes. Frontend's updateExecutionProgress
                # is idempotent for identical payloads, so the cost is minimal.
                if has_changes or synced_count > 0:
                    # Use the actual current execution phase from phase event tracking
                    actual_phase = (
                        self._task_current_phases.get(task_id, TaskPhase.PLANNING).value
                        if task_id
                        else "coding"
                    )
                    await self._safe_emit_task_update(
                        task_id or spec_id,
                        {
                            "executionProgress": {
                                "phase": actual_phase,
                                "phaseProgress": progress,
                                "overallProgress": scale_progress(
                                    actual_phase, progress
                                ),
                                "currentSubtask": current_subtask,
                                "message": f"{completed}/{total} subtasks completed",
                            },
                            "phase": current_phase,
                            "subtasksCompleted": completed,
                            "subtasksTotal": total,
                            "subtasks": subtasks_data,
                        },
                        # Sync ticks always go through: file CONTENT may have
                        # changed even if the dedup signature didn't.
                        force=synced_count > 0,
                    )
        except Exception as e:
            logger.warning(f"[AgentService] Failed to emit task update: {e}")

        # Running-cost (live): emit a THROTTLED usage snapshot from the worktree's
        # live token_usage.json so the cockpit reflects accruing cost WHILE the
        # build runs — not only when it pauses (review, PR1) or terminally
        # completes. The ~3s sync tick is throttled per task to _USAGE_EMIT_WINDOW_S
        # so it can't flood the completion webhook. Best-effort; the throttle clock
        # only advances on a real emit, so early ticks (no usage yet) keep checking
        # cheaply until usage exists.
        if task_id:
            try:
                import time as _time

                now = _time.monotonic()
                if (
                    now - self._task_usage_emit_ts.get(task_id, 0.0)
                    >= _USAGE_EMIT_WINDOW_S
                ):
                    from .completion import emit_usage_snapshot

                    pid = task_id.split(":", 1)[0] if ":" in task_id else ""
                    emitted = emit_usage_snapshot(
                        worktree_spec,
                        task_id=task_id,
                        project_id=pid,
                        spec_id=spec_id,
                        status="running",
                    )
                    if emitted is not None:
                        self._task_usage_emit_ts[task_id] = now
            except Exception:  # noqa: BLE001 - live cost reporting is best-effort
                logger.debug(
                    "[AgentService] live usage snapshot emit failed (best-effort)",
                    exc_info=True,
                )
