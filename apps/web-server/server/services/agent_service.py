"""
Agent execution service.

Wraps the existing run.py and spec_runner.py CLI tools as async services,
enabling task execution with real-time streaming of logs and progress.
"""

import asyncio
import functools
import json
import logging
import os
import signal
import sys
import threading
from collections import deque
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from factory_common.logsafe import sanitize_log

from ..config import get_settings
from ..utils.subprocess_env import make_subprocess_env
from . import task_control
from .agent_credential import CredentialMixin
from .agent_emit import (
    EmitMixin,
    _dedup_signature,  # noqa: F401  (re-export)
)
from .agent_kubejob import KubejobMixin
from .agent_queue import QueueMixin
from .agent_skill_context import SkillContextMixin
from .agent_spec_creation import SpecCreationMixin
from .agent_worktree_sync import WorktreeSyncMixin

# Module-level logger for the admission-control paths (RFC-0016 #668). The rest
# of this (legacy) module still uses local ``import logging`` inside methods.
_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Re-exports from the god-file decomposition. Every name below moved out of this
# module and is imported back so existing callers are unchanged. isort sorts
# this block, so a comment naming one module cannot sit above another's import;
# each is annotated inline instead.
# ---------------------------------------------------------------------------
# Agent task runtime models (services/agent_task_models.py).
from .agent_task_models import (  # noqa: E402,F401
    QueuedTask,
    TaskLog,
    TaskProgress,
)

# TaskLogWriter (services/task_log_writer.py).
from .task_log_writer import TaskLogWriter  # noqa: E402,F401

# Task-phase model + phase/progress helpers (services/task_phase.py).
from .task_phase import (  # noqa: E402,F401
    PHASE_RANGES,
    TaskPhase,
    _append_parallel_flags,
    _append_quick_mode_flag,
    is_failed_build,
    phase_to_review_reason,
    phase_to_status,
    scale_progress,
    should_pass_force,
    sync_require_review_metadata,
)

# Tenant Isolation Mode — namespace routing, Epic #35 #36 PR-2
# (services/tenant_target.py).
from .tenant_target import TenantTarget, resolve_tenant_target  # noqa: E402,F401


class AgentService(
    KubejobMixin,
    CredentialMixin,
    EmitMixin,
    QueueMixin,
    WorktreeSyncMixin,
    SkillContextMixin,
    SpecCreationMixin,
):
    """Service for executing AI agents on tasks."""

    def __init__(self):
        self.settings = get_settings()
        self.running_tasks: dict[str, asyncio.subprocess.Process] = {}
        self._log_callbacks: dict[str, list[Callable]] = {}
        self._progress_callbacks: dict[str, list[Callable]] = {}
        self._task_log_writers: dict[str, tuple[TaskLogWriter, TaskLogWriter]] = {}
        # Per-spec stderr capture file paths (#146).
        self._spec_stderr_logs: dict[str, Path] = {}
        # Track sequence numbers per task for frontend out-of-order detection
        self._task_sequence_numbers: dict[str, int] = {}
        # Issue #14 — last emitted task:update signature per task. Used by
        # _safe_emit_task_update to suppress identical re-emissions (e.g. the
        # 3-second periodic _sync_worktree_files tick during long phases).
        self._last_emitted_task_update: dict[str, tuple] = {}
        # Track task start times for UI display
        self._task_start_times: dict[str, str] = {}
        # Track user IDs per task for email notifications
        self._task_user_ids: dict[str, str] = {}
        # Track current execution phase per task (for proper phase status on completion)
        self._task_current_phases: dict[str, TaskPhase] = {}
        # Track which Claude profile each task is using (for reactive failover)
        self._task_profiles: dict[str, dict] = {}
        # RFC-0016 #670 Claude token pool. Admission control (#672) runs MULTIPLE
        # builds concurrently in one pod; without a pool they all draw the SAME
        # token and collide on one rate limit. The pool hands DISTINCT creds when
        # several are configured and shares the single one otherwise (no change
        # for the 1-token case). Lazily built on first checkout so test/runtime
        # changes to the profiles file / env are picked up. The build is guarded
        # by _token_pool_build_lock so concurrent first-checkouts don't each
        # construct a private pool (which would all hand out the same LRU
        # credential — the very collision this fixes).
        self._token_pool: Any = None
        self._token_pool_build_lock = threading.Lock()
        # Track rate limit detection per task to allow reactive failover
        self._task_rate_limits: dict[str, bool] = {}
        # Track previous subtask statuses per task for granular change detection
        # Format: {task_id: {subtask_id: status_string}}
        self._task_subtask_states: dict[str, dict[str, str]] = {}
        # Track spec directory per task for reading implementation plans
        self._spec_dirs: dict[str, Path] = {}
        # Track tasks that were manually stopped (to prevent _monitor_process from re-handling)
        self._task_stopped: set[str] = set()
        # Track byte offset into build-progress.txt per task so the periodic
        # worktree-sync tick can emit only NEW lines as task:log events. Lets
        # the kanban detail view scroll the agent's narrative in real time
        # rather than waiting for full-page reload (Tier B auto-reload).
        self._task_build_progress_offset: dict[str, int] = {}
        # Live running-cost throttle (cost): monotonic ts of the last usage
        # snapshot emitted per task from the worktree-sync tick, so the cockpit
        # shows accruing cost mid-build without flooding the webhook.
        self._task_usage_emit_ts: dict[str, float] = {}
        # Admission control (RFC-0016 #668). A global concurrency cap keyed off
        # ``settings.MAX_CONCURRENT_TASKS``. Builds admitted while at the cap are
        # parked FIFO in ``_task_queue`` and auto-started when a running build
        # finishes (drained from the subprocess-exit monitor). The lock
        # serialises the count-check + spawn + dequeue so admit and exit-callback
        # can never race on the same freed slot. Single event loop, no busy-wait.
        self._admission_lock = asyncio.Lock()
        self._task_queue: deque[QueuedTask] = deque()
        # RFC-0016 #668 durable job state. When DATABASE_URL is set the cap +
        # FIFO queue + running set are backed by Postgres (survives restart,
        # multi-replica safe via SELECT ... FOR UPDATE). When unset we keep the
        # in-memory deque above and log that it is NOT multi-replica safe. The
        # in-process ``running_tasks`` dict is ALWAYS the authority for killing
        # this replica's own subprocesses; the store is the cross-replica gate.
        from .job_state_store import store_enabled

        self._store_enabled: bool = store_enabled()
        self._job_store: Any = None  # lazily built (needs the DB engine)
        # RFC-0016 #671 control/execution split. When AIFACTORY_BUILD_BACKEND=
        # kubejob the build runs as a k8s Job (run.py) instead of an in-pod
        # subprocess; this backend dispatches the Job + reconciles/reaps it.
        # Lazily built (needs the store). Default OFF — see _kubejob_backend_enabled.
        self._kubejob_build_backend: Any = None
        # RFC-0017 #680 Job-native log streaming. Per-task background tasks that
        # follow each build Job's pod logs into the cockpit + rmux sinks (the
        # in-pod path's two log sinks). Cancelled when the build reaches a
        # terminal state (reconcile) or is stopped. Keyed by task_id.
        self._kubejob_log_streamers: dict[str, asyncio.Task[Any]] = {}
        if self._store_enabled:
            _log.info(
                "[AgentService] RFC-0016 durable job-state store ENABLED "
                "(DATABASE_URL set) — cap/queue backed by Postgres."
            )
        else:
            _log.warning(
                "[AgentService] RFC-0016 durable job-state store DISABLED "
                "(DATABASE_URL unset) — using IN-MEMORY admission cap/queue. "
                "This is single-pod dev only and is NOT multi-replica safe; "
                "the cap/queue will NOT survive a restart."
            )

    def _store(self) -> Any:
        """Lazily build + cache the durable job-state store (RFC-0016 #668)."""
        if self._job_store is None:
            from .job_state_store import JobStateStore

            self._job_store = JobStateStore()
        return self._job_store

    def _make_spawn_args(self, **kwargs: Any) -> Any:
        """Build a JSON-portable SpawnArgs from start/queue kwargs (#668)."""
        from .job_state_store import SpawnArgs

        project_path = kwargs.pop("project_path")
        return SpawnArgs(project_path=str(project_path), **kwargs)

    def _read_correlation_key(self, project_path: Path, spec_id: str) -> str | None:
        """Read the upstream GitHub issue number for the correlation key (#612).

        Threads ``requirements.json -> provenance.issue_number`` into the
        durable job-state row so a build is joinable across PFactory/TFactory/
        CFactory. Best-effort: returns None when the spec has no provenance.
        """
        try:
            req_file = (
                project_path / ".aifactory" / "specs" / spec_id / "requirements.json"
            )
            if not req_file.exists():
                return None
            req = json.loads(req_file.read_text())
            prov = req.get("provenance")
            if isinstance(prov, dict) and prov.get("issue_number") is not None:
                return str(prov["issue_number"])
        except (OSError, ValueError):
            pass
        return None

    @property
    def backend_path(self) -> Path:
        """Get path to the backend directory."""
        return Path(self.settings.BACKEND_PATH)

    def register_log_callback(self, task_id: str, callback: Callable) -> Callable:
        """Register a callback for task logs. Returns unregister function."""
        if task_id not in self._log_callbacks:
            self._log_callbacks[task_id] = []
        self._log_callbacks[task_id].append(callback)
        return lambda: self._log_callbacks.get(task_id, []).remove(callback)

    def register_progress_callback(self, task_id: str, callback: Callable) -> Callable:
        """Register a callback for task progress. Returns unregister function."""
        if task_id not in self._progress_callbacks:
            self._progress_callbacks[task_id] = []
        self._progress_callbacks[task_id].append(callback)
        return lambda: self._progress_callbacks.get(task_id, []).remove(callback)

    async def _monitor_process(
        self,
        task_id: str,
        proc: asyncio.subprocess.Process,
        project_path: Path | None = None,
        spec_id: str | None = None,
        cmd: list[str] | None = None,
        env: dict | None = None,
    ) -> None:
        """Monitor subprocess and clean up when it finishes.

        Thin delegator: the ~670-line body now lives in
        ``services/process_monitor.monitor_process`` (#649). Kept here so the
        public call sites (``self._monitor_process(...)``) and the recursive
        failover/continuation restarts are unchanged. Imported lazily to avoid
        an import cycle (process_monitor imports names from this module).
        """
        from .process_monitor import monitor_process

        # RFC-0016 #668: capture the spec_dir BEFORE the monitor runs, because
        # the monitor pops ``_spec_dirs[task_id]`` during its terminal cleanup —
        # we still need it afterwards to read the control-plane status and map
        # it to a durable terminal lifecycle_state (never-overclaim).
        spec_dir_hint = self._spec_dirs.get(task_id)
        if spec_dir_hint is None and project_path is not None and spec_id:
            spec_dir_hint = project_path / ".aifactory" / "specs" / spec_id

        await monitor_process(self, task_id, proc, project_path, spec_id, cmd, env)

        # A build just exited (or this monitor instance handed off to a
        # failover/continuation restart). Free the durable slot first (so the
        # cap reflects reality before we drain), then try to fill any free slot
        # from the FIFO queue. ``_drain_queue`` is slot-count driven and
        # lock-guarded, so it is a safe no-op when a restart kept the task in
        # ``running_tasks`` (count unchanged) and only admits when a slot freed.
        await self._free_durable_slot_on_exit(task_id, spec_dir_hint)
        await self._drain_queue()

    async def _free_durable_slot_on_exit(
        self, task_id: str, spec_dir: Path | None = None
    ) -> None:
        """Move a finished build's durable row out of ``running`` (RFC-0016 #668).

        Called after the process monitor returns. If the task is still in
        ``running_tasks`` the monitor handed off to a failover/continuation
        restart (same slot, still running) — leave the row ``running``. Only
        when the subprocess is truly gone do we record the terminal state so
        the cap frees up. The terminal lifecycle mirrors the control-plane
        status the monitor just wrote (review_pending/human_review → ``review``;
        failures → ``failed``). Best-effort: never breaks the exit path.
        """
        if not self._store_enabled:
            return
        if task_id in self.running_tasks:
            # Restart/continuation kept the slot — not terminal yet.
            return
        # Map the control-plane status (task_control.json) to a lifecycle_state.
        lifecycle = "done"
        error: str | None = None
        try:
            if spec_dir is None:
                spec_dir = self._spec_dirs.get(task_id)
            if spec_dir is not None:
                ctrl = task_control.read_control(spec_dir)
                status = (ctrl.get("status") or "").lower()
                reason = (ctrl.get("review_reason") or "").lower()
                if status in ("failed",) or reason in ("errors", "invalid_plan"):
                    lifecycle = "failed"
                    error = reason or "build failed"
                elif status in ("human_review", "ai_review", "review_pending"):
                    lifecycle = "review"
                elif status == "done":
                    lifecycle = "done"
        except Exception:  # noqa: BLE001 - status read is best-effort
            _log.debug(
                "[AgentService] could not read control status for %s on exit",
                sanitize_log(task_id),
                exc_info=True,
            )
        try:
            await self._store().mark_terminal(task_id, lifecycle, error=error)
        except Exception:  # noqa: BLE001 - never break the exit/drain path
            _log.exception(
                "[AgentService] could not free durable slot for %s",
                sanitize_log(task_id),
            )
        # Running-cost: a build that paused at review still spent real tokens, but
        # emit_terminal_completion only fires on terminal states (done/failed/
        # stuck), so a review-paused build's usage never reaches the cockpit. Emit
        # a non-terminal usage snapshot here so the accrued cost is recorded
        # regardless of whether the task terminally completes. Best-effort.
        if lifecycle == "review" and spec_dir is not None:
            try:
                from .completion import emit_usage_snapshot

                pid = task_id.split(":", 1)[0] if ":" in task_id else ""
                sid = task_id.split(":", 1)[1] if ":" in task_id else spec_dir.name
                emit_usage_snapshot(
                    spec_dir,
                    task_id=task_id,
                    project_id=pid,
                    spec_id=sid,
                    status="human_review",
                )
            except Exception:  # noqa: BLE001 - usage reporting is best-effort
                _log.debug(
                    "[AgentService] usage snapshot emit failed for %s",
                    sanitize_log(task_id),
                    exc_info=True,
                )

    async def _update_plan_status(
        self,
        project_path: Path,
        spec_id: str,
        status: str,
        task_id: str,
        *,
        emit_events: bool = True,
    ) -> None:
        """Update the status field in implementation_plan.json after task completion.

        Also emits WebSocket events so the frontend updates in real-time UNLESS
        ``emit_events=False`` is passed — used by ``_monitor_process`` at the
        terminal exit branch (Issue #14) where the subsequent ``_emit_progress``
        is the single canonical terminal emission. Mid-run callers
        (plan_review / human_review checkpoints) keep the default ``True`` so
        kanban gets subtask data immediately.
        """
        import logging

        logger = logging.getLogger(__name__)
        plan_file = (
            project_path / ".aifactory" / "specs" / spec_id / "implementation_plan.json"
        )
        logger.info(
            "[AgentService._update_plan_status] CALLED for spec_id=%s, status=%s, task_id=%s",
            sanitize_log(spec_id),
            sanitize_log(status),
            sanitize_log(task_id),
        )
        logger.info(
            f"[AgentService._update_plan_status] plan_file path: {sanitize_log(plan_file)}"
        )
        logger.info(
            f"[AgentService._update_plan_status] plan_file exists: {plan_file.exists()}"
        )
        if not plan_file.exists():
            logger.warning(
                "[AgentService._update_plan_status] plan_file does not exist, returning early"
            )
            return

        # Map internal status to frontend-compatible status using the canonical helpers
        # (defined before try so it's available in the except fallback)
        phase_enum_map = {
            "completed": TaskPhase.COMPLETED,
            "failed": TaskPhase.FAILED,
            "human_review": TaskPhase.PLAN_REVIEW,
        }
        phase_enum = phase_enum_map.get(status)

        try:
            plan = json.loads(plan_file.read_text())

            # Don't overwrite if user explicitly marked task as done via kanban.
            # Issue #259: the control store is the source of truth for this; fall
            # back to the plan file for pre-#259 specs (read_control migrates it).
            spec_dir = plan_file.parent
            control_status = task_control.read_control(spec_dir).get("status")
            if control_status == "done" or plan.get("status") == "done":
                logger.info(
                    "[AgentService._update_plan_status] Status is 'done' (user-set), skipping overwrite for %s",
                    sanitize_log(spec_id),
                )
                return

            # Fix 2: Validate that the plan is not just a minimal status object
            # A valid plan should have phases and subtasks from spec creation
            if "phases" not in plan or not plan.get("phases"):
                logger.error(
                    "[AgentService] Invalid or minimal implementation plan detected for %s",
                    sanitize_log(spec_id),
                )
                if emit_events:
                    await self._safe_emit_task_status(task_id, "failed", "invalid_plan")
                return

            # Compute the control-plane status/reviewReason this checkpoint sets.
            new_review_reason: str | None = None
            if phase_enum:
                new_status = phase_to_status(phase_enum)
                new_review_reason = phase_to_review_reason(phase_enum)
            else:
                new_status = status

            # Keep writing status/reviewReason into the plan file for backward
            # compatibility with any reader that hasn't migrated, but the
            # authoritative copy is task_control.json below.
            plan["status"] = new_status
            if new_review_reason:
                plan["reviewReason"] = new_review_reason

            logger.info(
                f"[AgentService._update_plan_status] About to write file with status={plan.get('status')}, reviewReason={plan.get('reviewReason')}"
            )
            plan_file.write_text(json.dumps(plan, indent=2))
            logger.info(
                "[AgentService._update_plan_status] Successfully wrote plan_file"
            )

            # Issue #259: persist the authoritative control-plane state. This is
            # the web-server's OWN orchestration writing a terminal/checkpoint
            # status — it is control-plane, not an agent artifact.
            task_control.write_control(
                spec_dir,
                status=new_status,
                review_reason=new_review_reason,
                clear_review_reason=new_review_reason is None,
                updated_by="web_server",
            )
            logger.info(
                "[AgentService] Updated plan status to '%s' for %s",
                sanitize_log(plan["status"]),
                sanitize_log(spec_id),
            )

            # Emit the RFC-0001 completion event on a terminal build phase so the
            # cockpit (CFactory) threads the unit end to end. Both COMPLETED and
            # FAILED fire here — FAILED especially, since a failed build is never
            # marked "done" and would otherwise never emit. The route's "done"
            # transition emits the later human-approval event; CFactory dedups by
            # (service, correlation_key, status), so completed/failed/done are
            # distinct, complementary events. Best-effort; never breaks the build.
            # NOT gated on emit_events. emit_events controls WS double-emission
            # (Issue #14) and is False on the _monitor_process terminal path — so
            # gating the completion event on it meant a successful build (which
            # ends at human_review via that path) NEVER emitted the RFC-0001 event
            # to CFactory, and never emitted the per-worker OTel metrics that ride
            # inside it. Result: an empty cockpit + empty OpenObserve dashboard.
            # Mirror the side-effects block below: fire on the terminal phase with
            # a fire-once marker so the multiple COMPLETED call paths
            # (~emit_events=True and ~emit_events=False) don't double-emit. CFactory
            # also dedups by (service, correlation_key, status), but the OTel
            # metrics are NOT deduped, so the marker is what protects them.

            # Fire-once terminal-completion emission + side-effects (the RFC-0001
            # completion event on COMPLETED/FAILED, and the TFactory handoff + PR
            # endgame on COMPLETED). Extracted to completion_orchestration.py (#556)
            # so the marker-based fire-once gating is isolated and testable; gating
            # is NOT tied to emit_events (see #71). Behaviour-preserving — see
            # tests/test_terminal_completion_characterization.py.
            from .completion_orchestration import run_terminal_completion

            # backend_path is only used by the (best-effort) TFactory handoff and
            # was previously accessed inside that try/except; access it defensively
            # here so a half-built service still emits the completion event.
            try:
                _backend_path = self.backend_path
            except Exception:  # noqa: BLE001
                _backend_path = None
            await run_terminal_completion(
                spec_dir=spec_dir,
                project_path=project_path,
                spec_id=spec_id,
                task_id=task_id,
                backend_path=_backend_path,
                # human_review (PLAN_REVIEW) is also terminal for usage-reporting:
                # a successful build parks there (auto-merge off) and would
                # otherwise NEVER emit its RFC-0001 usage block to CFactory — the
                # cockpit then shows $0/0 tokens for real, completed spend. Emitting
                # here is usage-reporting only: is_completed (COMPLETED-only) still
                # gates the PR endgame/TFactory handoff, so human_review stays
                # resumable. The fire-once marker keeps it idempotent.
                is_terminal=phase_enum
                in (TaskPhase.COMPLETED, TaskPhase.FAILED, TaskPhase.PLAN_REVIEW),
                is_completed=phase_enum == TaskPhase.COMPLETED,
                terminal_status=(
                    "completed"
                    if phase_enum == TaskPhase.COMPLETED
                    else "human_review"
                    if phase_enum == TaskPhase.PLAN_REVIEW
                    else "failed"
                ),
                logger=logger,
            )

            # Extract subtasks for WebSocket broadcast
            subtasks_data = []
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

            # Emit WebSocket events so frontend updates in real-time. Skipped
            # at the terminal exit branch (Issue #14) — the _monitor_process
            # caller will emit a single canonical _emit_progress(COMPLETED|FAILED)
            # that fires both task:update and task:status itself.
            if emit_events:
                review_reason = plan.get("reviewReason")
                # First emit status change
                await self._safe_emit_task_status(
                    task_id, plan["status"], review_reason
                )
                # Then emit task update with subtasks so they appear immediately
                # in UI. Payload is ENRICHED with an executionProgress block (Issue #14)
                # so the frontend's log doesn't render `phase: N/A` and the store
                # receives a coherent terminal phase value.
                completed_count = sum(
                    1 for s in subtasks_data if s["status"] == "completed"
                )
                # Use the caller-supplied `status` argument (the raw terminal
                # signal — "completed" / "failed") rather than the already-mapped
                # `plan["status"]` (which for completed tasks becomes
                # "human_review" via phase_to_status). The dedup-signature
                # consumers downstream want the raw phase value.
                terminal_phases = {"completed": "completed", "failed": "failed"}
                terminal_phase_value = terminal_phases.get(status)
                update_payload: dict = {
                    "subtasks": subtasks_data,
                    "subtasksCompleted": completed_count,
                    "subtasksTotal": len(subtasks_data),
                }
                if terminal_phase_value:
                    update_payload["phase"] = terminal_phase_value
                    update_payload["executionProgress"] = {
                        "phase": terminal_phase_value,
                        "phaseProgress": 100,
                        "overallProgress": 100,
                    }
                await self._safe_emit_task_update(task_id, update_payload)
        except Exception as e:
            logger.error(f"[AgentService] Failed to update plan status: {e}")
            # Still emit status event so frontend updates even if plan file write failed
            if emit_events:
                try:
                    fallback_status = (
                        phase_to_status(phase_enum) if phase_enum else status
                    )
                    fallback_reason = (
                        phase_to_review_reason(phase_enum) if phase_enum else None
                    )
                    await self._safe_emit_task_status(
                        task_id, fallback_status, fallback_reason
                    )
                except Exception:
                    logger.error(
                        "[AgentService] Failed to emit fallback task:status for %s",
                        sanitize_log(task_id),
                    )

    def _read_parallel_opts(
        self, project_path: Path, spec_id: str
    ) -> tuple[bool | None, int | None]:
        """Read persisted parallel/workers from a spec's task_metadata.json (#376).

        The auto-continue build path (spec→plan→build) honors the same parallel
        settings the /start route accepts, so a normal create→auto-build run can
        go parallel without an explicit manual start.
        """
        try:
            import json

            meta_file = (
                project_path / ".aifactory" / "specs" / spec_id / "task_metadata.json"
            )
            if meta_file.exists():
                meta = json.loads(meta_file.read_text())
                return meta.get("parallel"), meta.get("workers")
        except (OSError, ValueError):
            pass
        return None, None

    def _concurrency_cap(self) -> int:
        """Global concurrent-build cap (RFC-0016 #668).

        Reads ``settings.MAX_CONCURRENT_TASKS`` (env override
        ``APP_MAX_CONCURRENT_TASKS``). A value <= 0 means *unlimited* — the
        previous behaviour before this cap existed — so existing deployments
        that set 0 keep oversubscribing exactly as before. Default is 5.
        """
        try:
            return int(self.settings.MAX_CONCURRENT_TASKS)
        except (TypeError, ValueError):
            return 5

    async def start_task_execution(  # noqa: PLR0913 - public API parity with _spawn_task_execution
        self,
        task_id: str,
        project_path: Path,
        spec_id: str,
        auto_continue: bool = True,
        base_branch: str | None = None,
        mode: str | None = "full",
        force: bool = False,
        user_id: str = "",
        stop_after_planning: bool = False,
        parallel: bool | None = None,
        workers: int | None = None,
    ) -> asyncio.subprocess.Process | None:
        """Admit a build: start it now, or queue it when at the concurrency cap.

        Admission control (RFC-0016 #668). Behaviour:

        * The per-task "already running" guard is preserved — re-starting the
          SAME ``task_id`` still raises ``ValueError`` (the route maps this to a
          409). The cap below is across DIFFERENT task_ids.
        * Below the cap (or cap <= 0 = unlimited): spawn immediately and return
          the ``Process``, exactly as before.
        * At the cap: the task is NOT rejected and the event loop is NOT
          blocked. It is parked FIFO in ``_task_queue``, marked ``queued`` in
          the control-plane store (so the cockpit/poller can see it), and
          auto-started when a running build finishes (see ``_drain_queue``).
          Returns ``None`` to signal "queued, not started".

        See ``_spawn_task_execution`` for the full argument semantics.
        """
        # Per-task guard stays OUTSIDE the admission lock so a duplicate start of
        # the same running task still fast-fails with 409 regardless of the cap.
        if task_id in self.running_tasks:
            raise ValueError(f"Task {task_id} is already running")
        if any(q.task_id == task_id for q in self._task_queue):
            raise ValueError(f"Task {task_id} is already queued")

        # #376 shipped the parallel harness but left it dark for every caller
        # except the auto-continue monitor, which read task_metadata itself.
        # Resolve the spec's persisted setting HERE — the one place every entry
        # point funnels through (intake from-issue, /start, plan-approval,
        # auto-fix, delegation, and both queue-drain paths, which inherit the
        # values resolved before parking). An explicit argument still wins, so
        # callers that pass parallel=False keep their serial build.
        if parallel is None:
            parallel, meta_workers = self._read_parallel_opts(project_path, spec_id)
            if workers is None:
                workers = meta_workers

        # RFC-0016 #668: durable, multi-replica-safe path. The store decides
        # admit-vs-queue under SELECT ... FOR UPDATE so two replicas can't
        # exceed the cap or double-start the same job_id. The in-memory path
        # below is the single-pod-dev fallback (no DATABASE_URL).
        if self._store_enabled:
            return await self._start_task_execution_durable(
                task_id=task_id,
                project_path=project_path,
                spec_id=spec_id,
                auto_continue=auto_continue,
                base_branch=base_branch,
                mode=mode,
                force=force,
                user_id=user_id,
                stop_after_planning=stop_after_planning,
                parallel=parallel,
                workers=workers,
            )

        async with self._admission_lock:
            cap = self._concurrency_cap()
            # cap <= 0 => unlimited (back-compat). Otherwise admit immediately
            # only when strictly below the cap.
            if cap <= 0 or len(self.running_tasks) < cap:
                return await self._spawn_task_execution(
                    task_id=task_id,
                    project_path=project_path,
                    spec_id=spec_id,
                    auto_continue=auto_continue,
                    base_branch=base_branch,
                    mode=mode,
                    force=force,
                    user_id=user_id,
                    stop_after_planning=stop_after_planning,
                    parallel=parallel,
                    workers=workers,
                )

            # At the cap — park FIFO and mark queued. Do not start, do not 409.
            self._task_queue.append(
                QueuedTask(
                    task_id=task_id,
                    project_path=project_path,
                    spec_id=spec_id,
                    auto_continue=auto_continue,
                    base_branch=base_branch,
                    mode=mode,
                    force=force,
                    user_id=user_id,
                    stop_after_planning=stop_after_planning,
                    parallel=parallel,
                    workers=workers,
                )
            )
            _log.info(
                "[AgentService] At concurrency cap (%s running, cap=%s) — "
                "queued task %s (position %s)",
                len(self.running_tasks),
                sanitize_log(cap),
                sanitize_log(task_id),
                len(self._task_queue),
            )

        # Mark queued + emit OUTSIDE the lock (I/O + WS fan-out): the task is
        # already safely parked in the queue, so releasing the lock first keeps
        # admissions snappy and avoids holding it across non-admission work.
        await self._mark_task_queued(task_id, project_path, spec_id)
        return None

    async def _start_task_execution_durable(  # noqa: PLR0913 - parity with start_task_execution
        self,
        task_id: str,
        project_path: Path,
        spec_id: str,
        auto_continue: bool,
        base_branch: str | None,
        mode: str | None,
        force: bool,
        user_id: str,
        stop_after_planning: bool,
        parallel: bool | None,
        workers: int | None,
    ) -> asyncio.subprocess.Process | None:
        """Postgres-backed admit-or-queue (RFC-0016 #668).

        Asks the durable store to grant a slot or queue the build inside a
        ``SELECT ... FOR UPDATE`` transaction (cap read from live Postgres
        counts, not memory). On ``"started"`` it spawns the subprocess exactly
        as the in-memory path does; on ``"queued"`` it marks the build queued
        and returns ``None``. A duplicate active ``job_id`` raises ``ValueError``
        (→ 409), preserving the per-task guard across replicas + restarts.
        """
        spawn_args = self._make_spawn_args(
            project_path=project_path,
            spec_id=spec_id,
            auto_continue=auto_continue,
            base_branch=base_branch,
            mode=mode,
            force=force,
            user_id=user_id,
            stop_after_planning=stop_after_planning,
            parallel=parallel,
            workers=workers,
        )
        correlation_key = self._read_correlation_key(project_path, spec_id)
        cap = self._concurrency_cap()

        # ValueError (already running/queued) propagates to the 409 mapping.
        outcome = await self._store().admit(
            task_id, spawn_args, cap, correlation_key=correlation_key
        )

        if outcome == "started":
            # The store reserved the slot durably; now start the real execution
            # unit. RFC-0016 #671: the control/execution split picks the backend
            # by env — ``kubejob`` dispatches a k8s Job that runs run.py, default
            # ``subprocess`` keeps today's in-pod behaviour. Either way, if the
            # start itself fails, free the durable slot so it isn't leaked (a
            # stuck "running" row would shrink the cap forever).
            try:
                # Single backend-selector (#671): kubejob Job vs in-pod subprocess.
                # The drain paths route through the SAME helper (agent_queue) so a
                # flipped backend applies to queued builds too.
                return await self._start_build_unit(
                    task_id=task_id,
                    project_path=project_path,
                    spec_id=spec_id,
                    correlation_key=correlation_key,
                    auto_continue=auto_continue,
                    base_branch=base_branch,
                    mode=mode,
                    force=force,
                    user_id=user_id,
                    stop_after_planning=stop_after_planning,
                    parallel=parallel,
                    workers=workers,
                )
            except Exception:
                await self._store().mark_terminal(
                    task_id, "failed", error="spawn failed during admission"
                )
                raise

        # outcome == "queued": park + surface the queued status for the cockpit.
        await self._mark_task_queued(task_id, project_path, spec_id)
        return None

    async def _spawn_task_execution(
        self,
        task_id: str,
        project_path: Path,
        spec_id: str,
        auto_continue: bool = True,
        base_branch: str | None = None,
        mode: str | None = "full",
        force: bool = False,
        user_id: str = "",
        stop_after_planning: bool = False,
        parallel: bool | None = None,
        workers: int | None = None,
    ) -> asyncio.subprocess.Process:
        """Spawn task execution (run.py) — the real subprocess start.

        Admission/queueing is handled by ``start_task_execution``; by the time
        this runs a slot has already been reserved under the admission lock.

        Args:
            mode: "quick" for simplified prompts (~70% fewer tokens), "full" for comprehensive prompts.
            force: If True, bypasses approval checks (use when plan was already manually approved).
            stop_after_planning: Passes ``--stop-after-planning`` to run.py.
                Used by the Copilot delegation flow (#94) — the planner writes
                implementation_plan.json and run.py exits cleanly before the
                coder/QA phases.
            parallel: When True, passes ``--parallel`` to run.py so independent
                subtasks run concurrently in dependency-graph waves (#376).
            workers: When set with ``parallel``, passes ``--workers N`` to cap
                concurrent subtasks per wave.
        """
        import logging

        logger = logging.getLogger(__name__)

        if task_id in self.running_tasks:
            raise ValueError(f"Task {task_id} is already running")

        # Build command
        cmd = [
            sys.executable,
            str(self.backend_path / "run.py"),
            "--spec",
            spec_id,
            "--project-dir",
            str(project_path),
        ]

        if auto_continue:
            cmd.append("--auto-continue")

            spec_dir = project_path / ".aifactory" / "specs" / spec_id

            # Write skill context file based on selectedSkills in task_metadata
            self._write_skill_context(spec_dir)

            # --force iff review is not required, or the plan was manually
            # approved (force=True from the approve_plan endpoint). Shared with
            # the kubejob manifest builder so the two paths cannot drift (#916).
            if should_pass_force(spec_dir, force):
                cmd.append("--force")  # Bypass approval check for headless execution
                if force:
                    logger.info(
                        "[AgentService] Using --force for %s (plan manually approved)",
                        sanitize_log(task_id),
                    )
            else:
                logger.info(
                    "[AgentService] Human review before coding enabled for task %s - not using --force",
                    sanitize_log(task_id),
                )

        if base_branch:
            cmd.extend(["--base-branch", base_branch])

        # Skip QA for quick mode (simple tasks) - coder_quick.md validates inline.
        # Shared with the kubejob manifest builder so the two paths cannot drift (#916).
        if _append_quick_mode_flag(cmd, mode):
            logger.info(
                f"[AgentService] Skipping QA for quick mode task {sanitize_log(task_id)}"
            )

        # Stop after planning for Copilot delegation flow (#94)
        if stop_after_planning:
            cmd.append("--stop-after-planning")
            logger.info(
                "[AgentService] Stop-after-planning for %s (Copilot delegation)",
                sanitize_log(task_id),
            )

        # Parallel subtask execution (#376): run independent subtasks in
        # dependency-graph waves. Previously these flags were accepted by the
        # API but silently dropped here.
        if _append_parallel_flags(cmd, parallel, workers):
            logger.info(
                f"[AgentService] Parallel execution enabled for {sanitize_log(task_id)} "
                f"(workers={sanitize_log(workers or 'default')})"
            )

        # Set environment — scrub ANTHROPIC_API_KEY so spawned subprocesses
        # can never silently bill the direct-API account (OAuth-only policy;
        # see apps/backend/core/auth.py).
        env = make_subprocess_env()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        # Run Claude in non-interactive mode - bypass permission prompts
        env["CLAUDE_CODE_ENTRYPOINT"] = "cli"  # Signal non-interactive mode
        env["CI"] = "true"  # Many CLI tools use this to detect non-interactive mode

        # Quick Mode: Use simplified prompts (~70% fewer tokens)
        if mode == "quick":
            env["QUICK_MODE"] = "true"
            logger.info(
                f"[AgentService] Quick Mode enabled for task {sanitize_log(task_id)}"
            )

        # Load backend .env file for graphiti and other settings
        backend_env_file = self.backend_path / ".env"
        if backend_env_file.exists():
            try:
                with open(backend_env_file) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            key, value = line.split("=", 1)
                            key = key.strip()
                            value = value.strip()
                            # Don't override existing env vars
                            if key not in env:
                                env[key] = value
                logger.info(
                    f"[AgentService] Loaded backend .env from {backend_env_file}"
                )
            except Exception as e:
                logger.warning(f"[AgentService] Failed to load backend .env: {e}")

        # Load project .aifactory/.env for project-level settings (USE_CLAUDE_MD, etc.)
        project_env_file = project_path / ".aifactory" / ".env"
        if project_env_file.exists():
            try:
                with open(project_env_file) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            key, value = line.split("=", 1)
                            key = key.strip()
                            value = value.strip()
                            if key not in env:
                                env[key] = value
                logger.info("[AgentService] Loaded project .env for task execution")
            except Exception as e:
                logger.warning(f"[AgentService] Failed to load project .env: {e}")

        # Get OAuth token from the pool (#670) so concurrent builds draw DISTINCT
        # credentials; returned to the pool when this build ends.
        token, profile_id, profile_name = self._resolve_claude_token_pooled(task_id)
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
            logger.info(
                f"[AgentService] Using Claude profile: {profile_name} ({profile_id})"
            )
            # Store for potential retry — read model from task_metadata.json
            exec_model = "sonnet"  # default
            exec_spec_dir = project_path / ".aifactory" / "specs" / spec_id
            exec_metadata_file = exec_spec_dir / "task_metadata.json"
            if exec_metadata_file.exists():
                try:
                    exec_metadata = json.loads(exec_metadata_file.read_text())
                    exec_model = exec_metadata.get("model", "sonnet")
                except (json.JSONDecodeError, OSError):
                    pass
            self._task_profiles[task_id] = {
                "profileId": profile_id,
                "profileName": profile_name,
                "attempt": 1,
                "model": exec_model,
            }
        else:
            logger.warning("[AgentService] No Claude OAuth token available")

        exec_model_display = self._task_profiles.get(task_id, {}).get("model", "sonnet")
        logger.info(
            "[AgentService] [Model: %s] Starting task execution for %s",
            sanitize_log(exec_model_display),
            sanitize_log(task_id),
        )
        logger.info(f"[AgentService] Command: {sanitize_log(' '.join(cmd))}")

        # Claude Code Remote Control (Issue #50 / native --remote-control flag).
        # When enabled per-task, the spawned `claude` registers a session with
        # Anthropic's API that the user can drive from claude.ai/code or the
        # Claude mobile app.  Two prerequisites are tightly coupled:
        #   1. Append ``--remote-control "AIFactory: <spec-id>"`` to cmd so the
        #      session is named and discoverable in the claude.ai/code session list.
        #   2. Scrub ``CLAUDE_CODE_OAUTH_TOKEN`` (and ``ANTHROPIC_AUTH_TOKEN``)
        #      from env so the subprocess falls back to ~/.claude/.credentials.json.
        #      Remote Control rejects setup-token-issued tokens with the error
        #      "Remote Control requires a full-scope login token".  The full-scope
        #      token lives in ~/.claude/.credentials.json (from ``claude auth login``)
        #      and is what core/auth.py's fallback chain reaches when env vars are
        #      absent (priority 4 in get_auth_token).
        #
        # Toggle source (in order):
        #   1. task_metadata.json :: enableRemoteControl  (per-task, frontend-set)
        #   2. project.settings.remoteControlByDefault    (per-project default)
        # Default off — Remote Control requires a paid Anthropic subscription
        # (Pro/Max/Team/Enterprise) so we can't enable it for everyone.
        _rc_enabled = False
        _rc_spec_dir = project_path / ".aifactory" / "specs" / spec_id
        _rc_metadata_file = _rc_spec_dir / "task_metadata.json"
        if _rc_metadata_file.exists():
            try:
                _rc_meta = json.loads(_rc_metadata_file.read_text())
                _rc_enabled = bool(_rc_meta.get("enableRemoteControl", False))
            except (json.JSONDecodeError, OSError):
                pass
        if not _rc_enabled:
            try:
                from ..project_store import load_projects

                _rc_projs = load_projects()
                _rc_pid = task_id.split(":", 1)[0]
                _rc_proj = _rc_projs.get(_rc_pid, {})
                if (_rc_proj.get("settings") or {}).get("remoteControlByDefault"):
                    _rc_enabled = True
            except Exception:  # noqa: BLE001 - default-remote-control probe is best-effort
                logger.debug(
                    "[AgentService] remoteControlByDefault probe failed (ignored)",
                    exc_info=True,
                )

        if _rc_enabled:
            _rc_session_name = f"AIFactory: {spec_id}"
            cmd.extend(["--remote-control", _rc_session_name])
            env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
            logger.warning(
                "[AgentService] Remote Control ENABLED for task_id=%s — "
                "session %r will appear in claude.ai/code. "
                "Scrubbed CLAUDE_CODE_OAUTH_TOKEN/ANTHROPIC_AUTH_TOKEN — "
                "agent will fall back to ~/.claude/.credentials.json "
                "(must be a full-scope token from `claude auth login`).",
                sanitize_log(task_id),
                sanitize_log(_rc_session_name),
            )

        # E2E test mode (Epic #44 R4): when AIFACTORY_TEST_AGENT_CMD is
        # set, the agent subprocess is replaced with the override (e.g.
        # ``sleep 300``).  The rmux create hook below still fires because
        # it derives the session purely from spec_id/project_path — so the
        # Playwright suite can exercise the Live Console without burning
        # LLM tokens.  MUST NOT be set in production — bypasses the agent
        # entirely.  We log loudly when it kicks in.
        _test_cmd = os.environ.get("AIFACTORY_TEST_AGENT_CMD", "").strip()
        if _test_cmd:
            import shlex

            cmd = shlex.split(_test_cmd)
            logger.warning(
                "[AgentService] AIFACTORY_TEST_AGENT_CMD active — replacing "
                "agent command with %r (task_id=%s). MUST NOT be set in prod.",
                sanitize_log(cmd),
                sanitize_log(task_id),
            )

        # Start subprocess with a pseudo-TTY to prevent "Stream closed" errors
        # Claude Code CLI expects a TTY for permission handling
        import pty

        master_fd, slave_fd = pty.openpty()

        # Tee stderr to a per-spec file so failures that happen before
        # the agent writes task_logs.json are still debuggable (#146).
        # _process_output still drains the PIPE; this is an additional
        # post-mortem capture, not a replacement.
        spec_stderr_log = (
            project_path / ".aifactory" / "specs" / spec_id / "spawn_stderr.log"
        )
        try:
            spec_stderr_log.parent.mkdir(parents=True, exist_ok=True)
            spec_stderr_log.write_text("")  # truncate any previous capture
        except OSError as _e:
            logger.debug(f"[AgentService] could not prep spawn_stderr.log: {_e}")

        # #363: optional OS sandbox — passthrough unless AIFACTORY_AGENT_SANDBOX
        # is set and bwrap is installed (zero behaviour change by default).
        from .sandbox import build_sandboxed_command

        cmd = build_sandboxed_command(cmd, project_path)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=slave_fd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(project_path),
            env=env,
            # Own session/process group so stop_task can kill the whole tree
            # (run.py + coder/git children) instead of orphaning them.
            start_new_session=True,
        )

        # Close slave fd in parent process
        os.close(slave_fd)

        # Track the per-spec stderr file so _process_output can mirror
        # stderr lines into it.
        self._spec_stderr_logs[task_id] = spec_stderr_log

        self.running_tasks[task_id] = proc

        # Initialize tracking for sequence numbers and start time
        self._task_sequence_numbers[task_id] = 0
        self._task_start_times[task_id] = datetime.now().isoformat()
        # Store spec directory for reading implementation plans during progress updates
        self._spec_dirs[task_id] = spec_dir

        # Create TaskLogWriter for detailed phase logs
        # Write to worktree spec dir (will be synced to main spec dir)
        worktree_spec_dir = (
            project_path
            / ".aifactory"
            / "worktrees"
            / "tasks"
            / spec_id
            / ".aifactory"
            / "specs"
            / spec_id
        )
        worktree_spec_dir.mkdir(parents=True, exist_ok=True)
        log_writer = TaskLogWriter(worktree_spec_dir)

        # Also write to main spec dir for immediate visibility
        main_spec_dir = project_path / ".aifactory" / "specs" / spec_id
        main_spec_dir.mkdir(parents=True, exist_ok=True)
        main_log_writer = TaskLogWriter(main_spec_dir)

        # Store log writers for cleanup
        self._task_log_writers[task_id] = (log_writer, main_log_writer)

        # Emit initial progress (100% within planning phase → 20% overall)
        await self._emit_progress(
            TaskProgress(
                task_id=task_id,
                phase=TaskPhase.PLANNING,
                message="Starting task execution...",
                percentage=100,
            )
        )

        # Initialize planning phase in logs
        log_writer.set_phase_status(spec_id, TaskPhase.PLANNING, "active")
        main_log_writer.set_phase_status(spec_id, TaskPhase.PLANNING, "active")

        # Start output processing in background with log writers
        asyncio.create_task(
            self._process_output(
                task_id,
                proc.stdout,
                is_stderr=False,
                log_writer=log_writer,
                spec_id=spec_id,
            )
        )
        asyncio.create_task(self._process_output(task_id, proc.stderr, is_stderr=True))

        # Start process monitor to clean up when finished (with file syncing and failover support)
        asyncio.create_task(
            self._monitor_process(task_id, proc, project_path, spec_id, cmd, env)
        )

        # Epic #44 R1 — opt-in Live Agent Console. No-op when
        # AIFACTORY_RMUX_ENABLED is unset/false (the default), so the
        # bank-pilot image's behaviour is byte-for-byte unchanged.
        from ..rmux.integration import create_if_enabled as _rmux_create

        try:
            # #322: thread the owning project id so the console bridge can
            # authorize attach/stream against the task's org.
            _rmux_project_id = task_id.split(":", 1)[0] if ":" in task_id else None
            await _rmux_create(
                spec_id, project_path, " ".join(cmd), project_id=_rmux_project_id
            )
        except Exception:
            # Already swallowed inside _rmux_create; this except is a
            # belt-and-suspenders guard so a wrapper bug here cannot
            # take down task execution.
            logger.warning(
                f"[AgentService] rmux create hook raised (ignored); spec_id={sanitize_log(spec_id)}"
            )

        return proc

    @staticmethod
    def _signal_process_tree(proc: "asyncio.subprocess.Process", sig: int) -> None:
        """Send ``sig`` to the whole process group, falling back to the proc.

        Builds are spawned with start_new_session=True, so the run.py pid leads a
        process group containing all its descendants (coder subprocesses, git).
        Signalling the group terminates the entire tree; without this the children
        orphan and the build keeps running after a stop (the stop-resistance bug).
        """
        pid = getattr(proc, "pid", None)
        if pid is None:
            return
        try:
            os.killpg(os.getpgid(pid), sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
        # Fall back to signalling just the process (e.g. non-POSIX, or no pgrp).
        try:
            proc.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass

    async def stop_task(self, task_id: str) -> bool:
        """Stop a running task."""
        import logging

        logger = logging.getLogger(__name__)
        if task_id not in self.running_tasks:
            # RFC-0016 #668: a task can be parked in the admission queue rather
            # than running. Stopping it just removes it from the queue (no
            # process to kill) so it never auto-starts later. With the durable
            # store the queued row may live in Postgres (queued on another
            # replica, or after a restart), so check there too.
            if self._store_enabled:
                try:
                    if await self._store().remove_queued(task_id):
                        logger.info(
                            f"[AgentService] Removed queued task {sanitize_log(task_id)} "
                            "from durable admission queue"
                        )
                        return True
                except Exception:  # noqa: BLE001 - never break stop on a store hiccup
                    logger.warning(
                        "[AgentService] durable remove_queued failed for %s",
                        sanitize_log(task_id),
                        exc_info=True,
                    )
            if any(q.task_id == task_id for q in self._task_queue):
                async with self._admission_lock:
                    self._task_queue = deque(
                        q for q in self._task_queue if q.task_id != task_id
                    )
                logger.info(
                    "[AgentService] Removed queued task %s from admission queue",
                    sanitize_log(task_id),
                )
                return True
            # RFC-0016 #671: a build may run as a k8s Job (no in-pod process to
            # signal). Stopping it deletes the Job + frees the durable slot.
            if self._kubejob_backend_enabled():
                if await self._stop_kubejob_build(task_id):
                    return True
            logger.info(
                "[AgentService] Task %s not in running_tasks (already stopped or never started)",
                sanitize_log(task_id),
            )
            return False

        # Mark as stopped BEFORE termination so _monitor_process defers to us
        self._task_stopped.add(task_id)

        proc = self.running_tasks[task_id]
        # Kill the whole process tree (run.py + coder/git children), not just
        # run.py — otherwise the children orphan and keep running.
        self._signal_process_tree(proc, signal.SIGTERM)

        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            self._signal_process_tree(proc, signal.SIGKILL)
            await proc.wait()

        # Get actual phase and spec info BEFORE cleanup
        actual_phase = self._get_current_phase(task_id)
        spec_dir = self._spec_dirs.get(task_id)

        # Finalize log writers — flush pending text, mark phase as failed
        if task_id in self._task_log_writers:
            log_writer, main_log_writer = self._task_log_writers[task_id]
            # Parse spec_id from task_id (format: "project_id:spec_id")
            spec_id = task_id.split(":", 1)[1] if ":" in task_id else task_id
            log_writer.finalize(spec_id, actual_phase)
            log_writer.set_phase_status(spec_id, actual_phase, "failed")
            main_log_writer.finalize(spec_id, actual_phase)
            main_log_writer.set_phase_status(spec_id, actual_phase, "failed")
            del self._task_log_writers[task_id]
            logger.debug(
                f"[AgentService] Finalized task logs for stopped task {sanitize_log(task_id)}"
            )

        # Persist failed status to implementation_plan.json
        if spec_dir:
            # Derive project_path: spec_dir is .aifactory/specs/XXX, project root is 3 levels up
            project_path = spec_dir.parent.parent.parent
            spec_id = task_id.split(":", 1)[1] if ":" in task_id else task_id
            await self._update_plan_status(project_path, spec_id, "failed", task_id)

        # Epic #44 R1 — reap rmux session if the feature was on. Idempotent
        # so safe even though _monitor_process may also reap on the natural
        # exit path.
        from ..rmux.integration import reap_if_enabled as _rmux_reap

        _reap_spec_id = task_id.split(":", 1)[1] if ":" in task_id else task_id
        try:
            await _rmux_reap(_reap_spec_id)
        except Exception:
            logger.warning(
                "[AgentService] rmux reap hook raised in stop_task (ignored); spec_id=%s",
                sanitize_log(_reap_spec_id),
            )

        # Use pop with default to handle race condition where _monitor_process
        # might have already removed the task
        self.running_tasks.pop(task_id, None)
        self._task_sequence_numbers.pop(task_id, None)
        self._last_emitted_task_update.pop(task_id, None)
        self._task_start_times.pop(task_id, None)
        self._task_subtask_states.pop(task_id, None)
        self._spec_dirs.pop(task_id, None)
        self._task_current_phases.pop(task_id, None)
        # #670: return the pooled Claude credential AND pop _task_profiles.
        self._release_task_credential(task_id)
        self._task_rate_limits.pop(task_id, None)
        self._task_user_ids.pop(task_id, None)

        # RFC-0016 #668: free the durable slot for a user-stopped build. The
        # store-terminal write is idempotent, so the exit monitor's own
        # ``_free_durable_slot_on_exit`` after the killed process reaps is a
        # harmless no-op. Best-effort; never breaks the stop path.
        if self._store_enabled:
            try:
                await self._store().mark_terminal(
                    task_id, "failed", error="stopped by user"
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "[AgentService] could not free durable slot on stop for %s",
                    sanitize_log(task_id),
                    exc_info=True,
                )

        # Emit human_review with errors reason (not just FAILED phase)
        await self._safe_emit_task_status(task_id, "human_review", "errors")
        await self._emit_progress(
            TaskProgress(
                task_id=task_id,
                phase=TaskPhase.FAILED,
                message="Task stopped by user",
            )
        )

        return True

    async def wait_for_task(self, task_id: str) -> int:
        """Wait for a task to complete and return exit code."""
        if task_id not in self.running_tasks:
            raise ValueError(f"Task {task_id} is not running")

        proc = self.running_tasks[task_id]
        return_code = await proc.wait()

        del self.running_tasks[task_id]
        self._task_sequence_numbers.pop(task_id, None)
        self._last_emitted_task_update.pop(task_id, None)
        self._task_start_times.pop(task_id, None)
        self._task_subtask_states.pop(task_id, None)
        self._spec_dirs.pop(task_id, None)

        if return_code == 0:
            await self._emit_progress(
                TaskProgress(
                    task_id=task_id,
                    phase=TaskPhase.COMPLETED,
                    message="Task completed successfully",
                )
            )
        else:
            await self._emit_progress(
                TaskProgress(
                    task_id=task_id,
                    phase=TaskPhase.FAILED,
                    message=f"Task failed with exit code {return_code}",
                )
            )

        return return_code

    async def reconcile_on_startup(self) -> dict[str, int]:
        """Reconstruct in-flight admission state after a restart (RFC-0016 #668).

        A fresh control-plane replica calls this on boot so the cap/queue
        reflect Postgres rather than an empty in-memory dict (concurrency
        conventions §1 durability rule). It does NOT re-spawn subprocesses
        here — the build subprocesses live in whichever pod started them; a
        Phase-2 (k8s Job) reaper handles orphans. What it does:

          * surfaces the durable running/queued counts to the log, and
          * drains any queued rows into now-free slots (e.g. the replica that
            owned a running build died, freeing capacity).

        Returns ``{"running": n, "queued": m}`` for observability/tests.
        No-op (returns zeros) in the in-memory fallback mode.
        """
        if not self._store_enabled:
            return {"running": 0, "queued": 0}
        try:
            state = await self._store().reconstruct()
        except Exception:  # noqa: BLE001 - startup must not crash on a store hiccup
            _log.exception("[AgentService] startup reconcile failed")
            return {"running": 0, "queued": 0}
        running = len(state.get("running", []))
        queued = len(state.get("queued", []))
        _log.info(
            "[AgentService] RFC-0016 startup reconcile: %d running, %d queued "
            "(durable store)",
            running,
            queued,
        )
        # Fill any slots freed by a dead replica's running builds.
        await self._drain_queue()
        return {"running": running, "queued": queued}

    def is_running(self, task_id: str) -> bool:
        """Check if a task is currently running."""
        return task_id in self.running_tasks

    def get_running_tasks(self) -> list[str]:
        """Get list of running task IDs."""
        return list(self.running_tasks.keys())

    def is_queued(self, task_id: str) -> bool:
        """Check if a task is parked in the admission queue (RFC-0016 #668)."""
        return any(q.task_id == task_id for q in self._task_queue)

    def get_queued_tasks(self) -> list[str]:
        """Get queued task IDs in FIFO order (RFC-0016 #668)."""
        return [q.task_id for q in self._task_queue]


# Global service instance
@functools.cache
def get_agent_service() -> AgentService:
    """Get the global agent service instance."""
    return AgentService()
