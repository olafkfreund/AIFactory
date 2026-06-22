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

from ..websockets.events import emit_subtask_update
from . import task_control
from .task_phase import TaskPhase, scale_progress

if TYPE_CHECKING:
    from collections.abc import Callable

_log = logging.getLogger(__name__)


class WorktreeSyncMixin:
    """Worktree-to-main spec-dir file sync for AgentService."""

    if TYPE_CHECKING:
        # Attributes/methods provided by the concrete host (AgentService) and
        # sibling mixins; declared so mypy resolves the self.* refs (#703 pattern).
        _task_build_progress_offset: dict[str, Any]
        _task_current_phases: dict[str, Any]
        _task_subtask_states: dict[str, Any]
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

        # Directories to sync (will copy entire directory tree)
        dirs_to_sync = [
            "memory",  # Session insights and memory data
        ]

        synced_count = 0
        for filename in files_to_sync:
            src = worktree_spec / filename
            dst = main_spec / filename
            if src.exists():
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

        # Sync directories
        for dirname in dirs_to_sync:
            src_dir = worktree_spec / dirname
            dst_dir = main_spec / dirname
            if src_dir.exists() and src_dir.is_dir():
                try:
                    # Remove existing and copy fresh
                    if dst_dir.exists():
                        shutil.rmtree(dst_dir)
                    shutil.copytree(src_dir, dst_dir)
                    synced_count += 1
                except Exception as e:
                    logger.warning(
                        f"[AgentService] Failed to sync directory {dirname}: {e}"
                    )

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
