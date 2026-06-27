"""Task event emission + output processing — EmitMixin (#703).

The WebSocket task:update/task:status emission (with dedup), progress emission,
agent-output parsing/processing, and the pure _dedup_signature helper — lifted
out of the AgentService god-class into a mixin (see #703 / #704 for the pattern).
AgentService inherits this mixin; methods run as bound methods via the MRO.
routes/agent_service re-exports _dedup_signature so existing importers are
unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..websockets.events import emit_task_status, emit_task_update
from .agent_task_models import TaskLog, TaskProgress
from .task_log_writer import TaskLogWriter
from .task_phase import (
    TaskPhase,
    phase_to_review_reason,
    phase_to_status,
    scale_progress,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_log = logging.getLogger(__name__)


def _dedup_signature(payload: dict) -> tuple:
    """Compute a structural signature of a task:update payload for deduplication.

    Excluded (volatile per-tick, not material to state):
      - message            — streams free-text per tick during QA, etc.
      - sequenceNumber     — monotonically increases on every emit by design
      - startedAt          — fixed for a task's lifetime
      - timestamp          — wall-clock per emit

    Included (material state):
      - phase / executionProgress.{phase, phaseProgress, overallProgress, currentSubtask}
      - subtasksCompleted / subtasksTotal
      - subtasks (as a tuple of (id, status) pairs — checkbox transitions are
        meaningful even when phase/progress haven't moved)
    """
    exec_ = payload.get("executionProgress") or {}
    subtasks = payload.get("subtasks") or []
    return (
        payload.get("phase"),
        exec_.get("phase"),
        exec_.get("phaseProgress"),
        exec_.get("overallProgress"),
        exec_.get("currentSubtask"),
        payload.get("subtasksCompleted"),
        payload.get("subtasksTotal"),
        tuple((s.get("id"), s.get("status")) for s in subtasks),
    )


class EmitMixin:
    """Event emission + output processing methods for AgentService."""

    if TYPE_CHECKING:
        # Attributes/methods provided by the concrete host (AgentService) and
        # sibling mixins; declared so mypy resolves the self.* refs (#703 pattern).
        _last_emitted_task_update: dict[str, Any]
        _log_callbacks: dict[str, Any]
        _progress_callbacks: dict[str, Any]
        _spec_dirs: dict[str, Any]
        _spec_stderr_logs: dict[str, Any]
        _task_current_phases: dict[str, Any]
        _task_rate_limits: dict[str, Any]
        _task_sequence_numbers: dict[str, Any]
        _task_start_times: dict[str, Any]
        _is_rate_limit_line: Callable[..., Any]

    async def _emit_log(self, log: TaskLog) -> None:
        """Emit a log to all registered callbacks."""
        callbacks = self._log_callbacks.get(log.task_id, [])
        for callback in callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(log)
                else:
                    callback(log)
            except Exception:  # noqa: BLE001 - one bad callback must not break log fan-out
                logging.getLogger(__name__).debug(
                    "[AgentService] log callback raised (ignored)", exc_info=True
                )

    def _get_next_sequence_number(self, task_id: str) -> int:
        """Get the next sequence number for a task (for out-of-order detection)."""
        current = self._task_sequence_numbers.get(task_id, 0)
        next_seq = current + 1
        self._task_sequence_numbers[task_id] = next_seq
        return next_seq

    def _get_current_phase(self, task_id: str) -> TaskPhase:
        """Get the current execution phase for a task.

        Returns the tracked phase or defaults to PLANNING if unknown.
        This is used to determine which phase to mark as completed/failed
        when a task finishes, avoiding incorrect status on phases that were
        never actually reached.
        """
        return self._task_current_phases.get(task_id, TaskPhase.PLANNING)

    async def _safe_emit_task_update(
        self, task_id: str, payload: dict, *, force: bool = False
    ) -> None:
        """Funnel for all in-service task:update emissions with structural dedup.

        Compares the payload's structural signature (phase, progress, subtasks,
        etc. — see ``_dedup_signature``) against the last emission for this
        task. If identical, the emit is suppressed and we log at DEBUG.

        ``force=True`` bypasses the dedup check and always broadcasts. Use it
        from the periodic worktree-sync tick when we know files were just
        copied (the file CONTENT may have changed even though the structural
        signature didn't — e.g. ``task_logs.json`` grew, ``build-progress.txt``
        was rewritten, qwen3 is mid-tool-loop inside a single subtask). Without
        this escape hatch the kanban board freezes for the entire duration
        of a long subtask because dedup correctly observes that phase/progress/
        subtask-status haven't moved yet.

        asyncio single-thread invariant: the comparison and the dict write are
        not separated by any ``await`` — no other coroutine can interleave on
        this event loop. If anyone ever moves these emissions to a thread
        pool, ``_last_emitted_task_update`` becomes a race and would need an
        ``asyncio.Lock``.
        """
        import logging

        _logger = logging.getLogger(__name__)
        sig = _dedup_signature(payload)
        if not force and self._last_emitted_task_update.get(task_id) == sig:
            _logger.debug("[AgentService] dedup-suppressed task:update for %s", task_id)
            return
        self._last_emitted_task_update[task_id] = sig
        await emit_task_update(task_id, payload)

    async def _safe_emit_task_status(
        self, task_id: str, status: str, review_reason: str | None = None
    ) -> None:
        """Funnel for all in-service task:status emissions.

        No dedup — status transitions are rare and meaningful, and a duplicate
        is harmless (the frontend just reapplies the same column move). Kept
        as a helper for symmetry with _safe_emit_task_update and for future
        evolution (e.g. inserting metrics, alerting).

        Side effect (Epic #35 #40 half-B): fires a workspace-store snapshot
        upload at the four phase boundaries that matter — coding /
        review_pending / completed / failed. Failure-safe per the store's
        own contract; never crashes this hot path.

        Side effect (Epic #35 #42 PR-1): wraps phase-boundary work in an
        OTel ``task:phase:<status>`` span so the agent-task lifecycle
        shows up in traces. No-op when OTel SDK isn't initialised.
        """
        from ..observability.tracing import task_phase_span

        await emit_task_status(task_id, status, review_reason)
        if status in ("coding", "review_pending", "completed", "failed"):
            with task_phase_span(task_id, status):
                await self._snapshot_project_workspace(task_id, status)

    async def _snapshot_project_workspace(
        self,
        task_id: str,
        phase: str,
    ) -> None:
        """Snapshot the project workspace to S3 (or whichever fsspec
        backend is configured). No-op when WORKSPACE_S3_URI_BASE is
        unset. The store handles all failure modes internally; this
        wrapper just resolves the project context."""
        try:
            from .workspace_store import WorkspaceStore

            store = WorkspaceStore.from_settings()
            if not store.is_remote():
                return  # local-only mode; no upload to do

            # task_id format established by execution.py:
            # `{project_id}:{spec_id}` for normal tasks, plain
            # `{spec_id}` for legacy CLI-spawned ones we can't snapshot.
            if ":" not in task_id:
                return
            project_id = task_id.split(":", 1)[0]

            # Resolve the project record + its local path.
            from ..routes.projects import load_projects

            projects = load_projects()
            proj = projects.get(project_id)
            if proj is None:
                return  # project was deleted while task was running
            local_path = Path(proj.get("path", ""))
            if not local_path.is_dir():
                return  # workspace got cleaned up; nothing to snapshot

            # org_id is optional today (single-tenant default). Epic #36
            # will populate it on every project; we fall back to
            # "default" so single-tenant deployments still snapshot
            # cleanly without a migration.
            org_id = proj.get("org_id") or "default"

            await store.upload_project(
                org_id=org_id,
                project_id=project_id,
                local_path=local_path,
                triggered_by_task_id=task_id,
                triggered_by_phase=phase,
            )
        except Exception:
            # Belt-and-braces: the store is already failure-safe but a
            # bug in this wrapper shouldn't crash the status emission.
            import logging

            logging.getLogger(__name__).warning(
                "[workspace_store] snapshot hook failed for task=%s phase=%s",
                task_id,
                phase,
                exc_info=True,
            )

    async def _emit_progress(
        self, progress: TaskProgress, previous_phase: TaskPhase | None = None
    ) -> None:
        """Emit progress to all registered callbacks and broadcast via WebSocket.

        If previous_phase is provided and differs from current phase, also emits
        a status change event to update the kanban board column.
        """
        # Broadcast via WebSocket for real-time frontend updates
        try:
            # Use task:update event which frontend handles correctly for progress
            # Frontend's onTaskUpdate handler expects: {taskId, executionProgress?, phase?, subtasks?, ...}
            phase_progress = progress.percentage or 0
            phase_value = progress.phase.value if progress.phase else "coding"
            # Scale within-phase progress to overall range, unless explicitly overridden
            if progress.overall_progress is not None:
                overall_progress = progress.overall_progress
            else:
                overall_progress = scale_progress(phase_value, phase_progress)

            # Get sequence number for out-of-order detection
            sequence_number = self._get_next_sequence_number(progress.task_id)

            # Get task start time (tracked when task started)
            started_at = self._task_start_times.get(progress.task_id)

            # Read subtasks from implementation_plan.json for real-time UI updates
            # Frontend needs the full subtasks array to display checkboxes and status
            subtasks_data = []
            try:
                # Get spec directory from task metadata
                spec_dir = self._spec_dirs.get(progress.task_id)
                if spec_dir:
                    plan_file = spec_dir / "implementation_plan.json"
                    if plan_file.exists():
                        plan = json.loads(plan_file.read_text())
                        # Extract all subtasks from all phases
                        phases = plan.get("phases", [])
                        for phase in phases:
                            phase_subtasks = phase.get("subtasks", [])
                            for subtask in phase_subtasks:
                                subtasks_data.append(
                                    {
                                        "id": subtask.get("id", ""),
                                        "status": subtask.get("status", "pending"),
                                        "title": subtask.get("description", ""),
                                    }
                                )
            except Exception as e:
                import logging

                logging.getLogger(__name__).debug(
                    f"[AgentService] Could not read subtasks for {progress.task_id}: {e}"
                )

            await self._safe_emit_task_update(
                progress.task_id,
                {
                    "executionProgress": {
                        "phase": phase_value,
                        "phaseProgress": phase_progress,
                        "overallProgress": overall_progress,
                        "currentSubtask": progress.subtask,
                        "message": progress.message,
                        "sequenceNumber": sequence_number,
                        "startedAt": started_at,
                    },
                    "phase": phase_value,
                    "subtasksCompleted": progress.subtask_index,
                    "subtasksTotal": progress.subtask_total,
                    "subtasks": subtasks_data,  # Include subtasks array for frontend
                },
            )

            # If phase changed, also emit status change for kanban column movement
            if previous_phase is not None and progress.phase != previous_phase:
                new_status = phase_to_status(progress.phase)
                review_reason = phase_to_review_reason(progress.phase)
                await self._safe_emit_task_status(
                    progress.task_id, new_status, review_reason
                )

        except Exception as e:
            import logging

            logging.getLogger(__name__).warning(
                f"[AgentService] WebSocket broadcast failed: {e}"
            )

        # Also emit to local callbacks
        callbacks = self._progress_callbacks.get(progress.task_id, [])
        for callback in callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(progress)
                else:
                    callback(progress)
            except Exception:  # noqa: BLE001 - one bad callback must not break progress fan-out
                logging.getLogger(__name__).debug(
                    "[AgentService] progress callback raised (ignored)", exc_info=True
                )

    def _parse_phase_event(self, line: str) -> dict | None:
        """Parse phase event from agent output.

        Supports two formats:
        1. [PHASE_EVENT] phase=coding message="Starting"
        2. __EXEC_PHASE__:{"phase":"coding","message":"Starting","progress":50}
        """
        # Check for __EXEC_PHASE__: prefix (JSON format from backend)
        exec_phase_prefix = "__EXEC_PHASE__:"
        if line.startswith(exec_phase_prefix):
            try:
                json_str = line[len(exec_phase_prefix) :]
                event = json.loads(json_str)
                # Map 'progress' to 'percentage' for consistency
                if "progress" in event:
                    event["percentage"] = event.pop("progress")
                return event
            except json.JSONDecodeError:
                return None

        # Check for [PHASE_EVENT] prefix (key=value format)
        match = re.match(r"\[PHASE_EVENT\]\s*(.+)", line)
        if not match:
            return None

        event_str = match.group(1)
        event = {}

        # Parse key=value pairs
        for part in re.findall(r"(\w+)=([^\s]+|\"[^\"]+\")", event_str):
            key, value = part
            value = value.strip('"')
            event[key] = value

        return event if event else None

    async def _process_output(
        self,
        task_id: str,
        stream: asyncio.StreamReader,
        is_stderr: bool = False,
        log_writer: TaskLogWriter | None = None,
        spec_id: str | None = None,
    ) -> TaskPhase:
        """Process output stream from subprocess.

        Returns the final phase detected.
        """
        import logging

        logger = logging.getLogger(__name__)
        # Use the tracked phase if available (e.g., PLANNING when started via start_task_execution),
        # otherwise default to SPEC_CREATION for spec creation processes
        current_phase = self._task_current_phases.get(task_id, TaskPhase.SPEC_CREATION)

        # Epic #44 — tee this stream's bytes into the task's Live Console
        # FIFO (read-only mirror). Gated once up-front so there's no
        # per-line cost when rmux is off. spec_id is the suffix of the
        # composite task_id (``project_id:spec_id``).
        _rmux_spec = task_id.split(":", 1)[1] if ":" in task_id else task_id
        _rmux_feed = None
        try:
            from ..rmux.integration import feed_if_enabled as _rmux_feed_fn
            from ..rmux.integration import is_enabled as _rmux_on

            if _rmux_on():
                _rmux_feed = _rmux_feed_fn
        except Exception:  # noqa: BLE001 - rmux integration is optional; degrade to no mirror
            logger.debug(
                "[AgentService] rmux integration unavailable; Live Console mirror disabled",
                exc_info=True,
            )
            _rmux_feed = None

        async for line_bytes in stream:
            # Mirror raw bytes to the Live Console (xterm needs CRLF).
            if _rmux_feed is not None:
                try:
                    _rmux_feed(_rmux_spec, line_bytes.replace(b"\n", b"\r\n"))
                except Exception:  # noqa: BLE001 - Live Console mirror is best-effort
                    logger.debug(
                        "[AgentService] rmux Live Console feed raised (ignored)",
                        exc_info=True,
                    )

            line = line_bytes.decode("utf-8", errors="replace").rstrip()

            # Log stderr to server logs for debugging
            if is_stderr and line:
                logger.warning(f"[AgentService] Task {task_id} stderr: {line}")
                # Also mirror stderr to a per-spec file so post-mortem
                # debugging works even when the subprocess dies before
                # writing its own task_logs.json (#146).
                stderr_file = self._spec_stderr_logs.get(task_id)
                if stderr_file is not None:
                    try:
                        with stderr_file.open("a", encoding="utf-8") as fh:
                            fh.write(line + "\n")
                    except OSError:
                        pass

            # Create log entry
            log = TaskLog(
                task_id=task_id,
                content=line,
                source="stderr" if is_stderr else "stdout",
                level="error" if is_stderr else "info",
            )
            await self._emit_log(log)

            # Detect rate limit messages to trigger failover after exit
            if self._is_rate_limit_line(line):
                self._task_rate_limits[task_id] = True
                logger.warning(
                    f"[AgentService] Rate limit detected for task {task_id} (will attempt failover if enabled)"
                )

            # Write to task_logs.json for detailed phase logs
            if log_writer and spec_id and not is_stderr:
                log_writer.process_line(spec_id, current_phase, line)

            # Check for phase events (__EXEC_PHASE__: or [PHASE_EVENT])
            event = self._parse_phase_event(line)
            if event:
                phase_str = event.get("phase", "")
                phase_map = {
                    "spec_creation": TaskPhase.SPEC_CREATION,
                    "planning": TaskPhase.PLANNING,
                    "coding": TaskPhase.CODING,
                    "qa_review": TaskPhase.QA_REVIEW,
                    "qa_fixing": TaskPhase.QA_FIXING,
                    "complete": TaskPhase.COMPLETED,  # backend uses "complete"
                    "completed": TaskPhase.COMPLETED,
                    "failed": TaskPhase.FAILED,
                }
                old_phase = current_phase
                if phase_str in phase_map:
                    current_phase = phase_map[phase_str]

                    # Track current phase for proper status on task completion
                    self._task_current_phases[task_id] = current_phase

                    # Update log writer phase status
                    if log_writer and spec_id:
                        if old_phase != current_phase:
                            log_writer.set_phase_status(spec_id, old_phase, "completed")
                        # For COMPLETED/FAILED phases, don't set them as "active" - just mark previous complete
                        if current_phase not in (TaskPhase.COMPLETED, TaskPhase.FAILED):
                            log_writer.set_phase_status(
                                spec_id, current_phase, "active"
                            )
                        # Ensure validation phase is properly marked completed when task completes
                        if current_phase == TaskPhase.COMPLETED and old_phase in (
                            TaskPhase.QA_REVIEW,
                            TaskPhase.QA_FIXING,
                        ):
                            log_writer.set_phase_status(spec_id, old_phase, "completed")

                # Always emit progress for phase events (even if phase didn't change)
                progress = TaskProgress(
                    task_id=task_id,
                    phase=current_phase,
                    message=event.get("message", ""),
                    subtask=event.get("subtask"),
                    subtask_index=int(event["subtask_index"])
                    if "subtask_index" in event
                    else None,
                    subtask_total=int(event["subtask_total"])
                    if "subtask_total" in event
                    else None,
                    percentage=event.get("percentage"),  # Include percentage from event
                    data=event,
                )
                # Pass previous phase if it changed, so status event can be emitted
                await self._emit_progress(
                    progress,
                    previous_phase=old_phase if old_phase != current_phase else None,
                )

            # Check for JSON progress data
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    if "phase" in data or "status" in data:
                        phase_str = data.get("phase", data.get("status", ""))
                        if phase_str in [
                            "coding",
                            "planning",
                            "qa_review",
                            "qa_fixing",
                        ]:
                            old_phase = current_phase
                            current_phase = TaskPhase(phase_str)

                            # Track current phase for proper status on task completion
                            self._task_current_phases[task_id] = current_phase

                            # Update log writer phase status
                            if log_writer and spec_id:
                                if old_phase != current_phase:
                                    log_writer.set_phase_status(
                                        spec_id, old_phase, "completed"
                                    )
                                log_writer.set_phase_status(
                                    spec_id, current_phase, "active"
                                )

                        progress = TaskProgress(
                            task_id=task_id,
                            phase=current_phase,
                            message=data.get("message", ""),
                            subtask=data.get("subtask"),
                            subtask_index=data.get("subtask_index"),
                            subtask_total=data.get("subtask_total"),
                            percentage=data.get("percentage"),
                            data=data,
                        )
                        # Pass previous phase if it changed, so status event can be emitted
                        await self._emit_progress(
                            progress,
                            previous_phase=old_phase
                            if old_phase != current_phase
                            else None,
                        )
                except json.JSONDecodeError:
                    pass

        return current_phase
