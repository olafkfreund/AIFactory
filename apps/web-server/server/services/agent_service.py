"""
Agent execution service.

Wraps the existing run.py and spec_runner.py CLI tools as async services,
enabling task execution with real-time streaming of logs and progress.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import sys
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from ..config import get_settings
from ..utils.subprocess_env import make_subprocess_env
from ..websockets.events import (
    emit_subtask_update,
    emit_task_logs_stream,
    emit_task_status,
    emit_task_update,
)
from . import task_control

# Module-level logger for the admission-control paths (RFC-0016 #668). The rest
# of this (legacy) module still uses local ``import logging`` inside methods.
_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tenant Isolation Mode — namespace routing (Epic #35 #36 PR-2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TenantTarget:
    """Where an agent task for one org should land.

    ``namespace`` and ``service_account`` are None when the org runs
    in legacy shared-namespace mode — the caller falls back to the
    deployment-default namespace + SA. ``isolation_mode`` mirrors
    ``tenant_states.isolation_mode``: ``shared`` | ``isolated`` |
    ``deleted``.

    The ``deleted`` mode is surfaced so the spawner can refuse to
    create new agent pods for soft-deleted orgs (design §7 stage-1).
    """

    isolation_mode: str
    namespace: str | None
    service_account: str | None


async def resolve_tenant_target(
    db: Any, org_id: str | None,
) -> TenantTarget:
    """Look up the tenant routing target for an agent task.

    The agent spawner calls this before pod-spawn:
      - When the row is missing OR ``isolation_mode='shared'``, the
        caller targets the deployment-default namespace + SA
        (backward compat with pre-#36 deployments).
      - When ``isolation_mode='isolated'``, the caller spawns into
        the per-tenant namespace as the per-tenant SA.
      - When ``isolation_mode='deleted'``, the caller MUST refuse
        to spawn new tasks (existing pods may finish but no new
        creates).

    ``org_id`` may be None for legacy single-tenant deployments
    where projects don't carry an org_id yet; we return shared mode
    so the spawner falls back gracefully.

    Failure-safe: ANY exception (DB error, missing model, etc.)
    falls back to shared mode + logs a warning. The agent spawner
    must never crash because the tenant_state row couldn't be read.
    """
    # WHY: deferred import. The web-server's agent_service is imported
    # by paths that don't always have the database set up (CLI tools,
    # tests); the lazy import keeps that path clean.
    from ..database.models import TenantState

    if not org_id:
        return TenantTarget(
            isolation_mode="shared", namespace=None, service_account=None,
        )
    try:
        state = await db.get(TenantState, org_id)
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "resolve_tenant_target: DB lookup failed for org=%s; "
            "falling back to shared mode",
            org_id, exc_info=True,
        )
        return TenantTarget(
            isolation_mode="shared", namespace=None, service_account=None,
        )

    if state is None or state.isolation_mode == "shared":
        return TenantTarget(
            isolation_mode="shared", namespace=None, service_account=None,
        )
    return TenantTarget(
        isolation_mode=state.isolation_mode,
        namespace=state.namespace_name,
        service_account=state.service_account,
    )


class TaskPhase(str, Enum):
    """Task execution phases."""

    SPEC_CREATION = "spec_creation"
    PLANNING = "planning"
    PLAN_REVIEW = "plan_review"  # Paused for human plan approval
    CODING = "coding"
    QA_REVIEW = "qa_review"
    QA_FIXING = "qa_fixing"
    COMPLETED = "completed"
    FAILED = "failed"


def _append_parallel_flags(
    cmd: list[str], parallel: bool | None, workers: int | None
) -> bool:
    """Append run.py parallel flags (#376) to ``cmd`` in place.

    Returns True when ``--parallel`` was added (so the caller can log it).
    Extracted as a pure helper so the route→executor flag threading is unit
    testable without spawning a subprocess.
    """
    if not parallel:
        return False
    cmd.append("--parallel")
    if workers and workers > 0:
        cmd.extend(["--workers", str(workers)])
    return True


def phase_to_status(phase: TaskPhase) -> str:
    """Map execution phase to task status for kanban column placement."""
    mapping = {
        TaskPhase.SPEC_CREATION: "in_progress",
        TaskPhase.PLANNING: "in_progress",
        TaskPhase.PLAN_REVIEW: "human_review",  # Paused for human plan approval
        TaskPhase.CODING: "in_progress",
        TaskPhase.QA_REVIEW: "ai_review",
        TaskPhase.QA_FIXING: "in_progress",
        TaskPhase.COMPLETED: "human_review",
        TaskPhase.FAILED: "human_review",
    }
    return mapping.get(phase, "in_progress")


def phase_to_review_reason(phase: TaskPhase) -> str | None:
    """Map execution phase to reviewReason field value.

    Returns the appropriate reviewReason for phases that result in human_review status:
    - PLAN_REVIEW: "plan_review" (waiting for plan approval before coding)
    - COMPLETED: "completed" (task finished successfully, needs final approval)
    - FAILED: "errors" (task failed, needs human intervention)

    Returns None for phases that don't require a reviewReason.
    """
    mapping = {
        TaskPhase.PLAN_REVIEW: "plan_review",
        TaskPhase.COMPLETED: "completed",
        TaskPhase.FAILED: "errors",
    }
    return mapping.get(phase)


# Subtask statuses that count as "did not succeed" when deciding whether a
# build that exited cleanly actually produced anything (Issue #287).
_FAILED_SUBTASK_STATUSES = frozenset({"failed", "stuck", "error", "blocked"})


def is_failed_build(plan: dict) -> bool:
    """Return True when a finished build did NOT actually succeed.

    Issue #287: a build whose process exits 0 but where NO subtask completed
    and at least one subtask failed/stuck still got mapped to the COMPLETED
    phase → ``human_review`` + reviewReason ``"completed"``, masking total
    failure as review-ready success (empty diff, "0 done / N failed").

    Conservative by design — only flips to failure when there was genuinely
    no progress:

    - At least one subtask exists (an empty/invalid plan is handled elsewhere).
    - ZERO subtasks reached ``completed``.
    - At least one subtask is in a failed/stuck state.

    A build with SOME completed subtasks (even alongside failures) is a real
    partial-review case and returns False, preserving the genuine human-review
    path. An all-pending plan (e.g. nothing ran) also returns False so we don't
    mislabel other flows.
    """
    completed = 0
    failed = 0
    total = 0
    for phase in plan.get("phases", []):
        for subtask in phase.get("subtasks", []):
            total += 1
            status = subtask.get("status", "pending")
            if status == "completed":
                completed += 1
            elif status in _FAILED_SUBTASK_STATUSES:
                failed += 1

    return total > 0 and completed == 0 and failed >= 1


# Phase ranges for overall progress scaling (start%, end%)
# Maps within-phase progress (0-100) to an overall range so progress is monotonically increasing.
PHASE_RANGES: dict[str, tuple[float, float]] = {
    "spec_creation": (0, 20),
    "planning": (0, 20),
    "plan_review": (20, 20),   # Fixed at 20%
    "coding": (20, 80),
    "qa_review": (80, 95),
    "qa_fixing": (80, 95),
    "completed": (95, 100),
    "failed": (0, 0),          # Keep whatever was last
}


def scale_progress(phase: str, phase_progress: float) -> float:
    """Scale within-phase progress (0-100) to overall progress range.

    Example: coding phase at 50% → 20 + (50/100) × 60 = 50% overall.
    """
    start, end = PHASE_RANGES.get(phase, (0, 100))
    width = end - start
    return round(start + (phase_progress / 100) * width)


@dataclass
class TaskProgress:
    """Real-time task progress information."""

    task_id: str
    phase: TaskPhase
    message: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    subtask: str | None = None
    subtask_index: int | None = None
    subtask_total: int | None = None
    percentage: float | None = None
    overall_progress: float | None = None  # Override scaled overall progress
    sequence_number: int = 0  # For frontend out-of-order detection
    started_at: str | None = None  # Task start time for UI display
    data: dict = field(default_factory=dict)


@dataclass
class QueuedTask:
    """A build admitted to the concurrency queue (RFC-0016 #668).

    Holds the full set of arguments needed to start the build later, when a
    running slot frees up. Captured verbatim from ``start_task_execution`` so
    a dequeued task spawns identically to one that was admitted immediately.
    """

    task_id: str
    project_path: Path
    spec_id: str
    auto_continue: bool
    base_branch: str | None
    mode: str | None
    force: bool
    user_id: str
    stop_after_planning: bool
    parallel: bool | None
    workers: int | None


@dataclass
class TaskLog:
    """A single log entry from task execution."""

    task_id: str
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    level: str = "info"  # info, warning, error, debug
    source: str = "agent"  # agent, stdout, stderr


class TaskLogWriter:
    """Writes detailed phase logs to task_logs.json."""

    # Tool patterns for Claude Code CLI output
    TOOL_PATTERNS = [
        # Pattern: "⏺ ToolName" or emoji + tool name
        (r'[⏺🔧📖✏️📝🔍💻]\s*(Read|Write|Edit|Bash|Glob|Grep|Task|WebFetch|WebSearch|LSP|NotebookEdit)\b', 'tool_start'),
        # Pattern: "Tool: ToolName" format
        (r'^Tool:\s*(Read|Write|Edit|Bash|Glob|Grep|Task|WebFetch|WebSearch|LSP|NotebookEdit)\b', 'tool_start'),
        # Pattern: Claude Code verbose format "Using Read tool"
        (r'Using\s+(Read|Write|Edit|Bash|Glob|Grep|Task|WebFetch|WebSearch|LSP|NotebookEdit)\s+tool', 'tool_start'),
        # Pattern: Tool invocation with parameters like "Read(file_path=...)"
        (r'^(Read|Write|Edit|Bash|Glob|Grep|Task|WebFetch|WebSearch|LSP|NotebookEdit)\s*\(', 'tool_start'),
    ]

    # Phase mapping from TaskPhase to task_logs.json phases
    # Note: COMPLETED and FAILED are NOT mapped here - they represent task
    # completion states, not execution phases. Use _get_current_phase() to
    # determine which phase the task was actually in when it completed/failed.
    PHASE_MAP = {
        TaskPhase.SPEC_CREATION: "planning",
        TaskPhase.PLANNING: "planning",
        TaskPhase.PLAN_REVIEW: "planning",
        TaskPhase.CODING: "coding",
        TaskPhase.QA_REVIEW: "validation",
        TaskPhase.QA_FIXING: "validation",
    }

    def __init__(self, spec_dir: Path):
        self.spec_dir = spec_dir
        self.log_file = spec_dir / "task_logs.json"
        self._current_tool: str | None = None
        self._tool_start_time: str | None = None
        self._tool_input: str | None = None
        self._pending_tool_output: list[str] = []
        self._initialized = False
        # Throttling for text emission (avoid flooding WebSocket)
        self._last_text_emit_time: float = 0
        self._text_emit_interval: float = 1.0  # seconds
        self._pending_text_lines: list[str] = []

    def _ensure_initialized(self, spec_id: str) -> dict:
        """Ensure task_logs.json exists with proper structure."""
        if self.log_file.exists():
            try:
                with open(self.log_file) as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError):
                pass

        # Create new structure
        now = datetime.now().isoformat()
        return {
            "spec_id": spec_id,
            "created_at": now,
            "updated_at": now,
            "phases": {
                "planning": {
                    "phase": "planning",
                    "status": "pending",
                    "started_at": None,
                    "completed_at": None,
                    "entries": []
                },
                "coding": {
                    "phase": "coding",
                    "status": "pending",
                    "started_at": None,
                    "completed_at": None,
                    "entries": []
                },
                "validation": {
                    "phase": "validation",
                    "status": "pending",
                    "started_at": None,
                    "completed_at": None,
                    "entries": []
                }
            }
        }

    def _save(self, data: dict) -> None:
        """Save task_logs.json."""
        self.spec_dir.mkdir(parents=True, exist_ok=True)
        data["updated_at"] = datetime.now().isoformat()
        with open(self.log_file, 'w') as f:
            json.dump(data, f, indent=2)

    def _detect_tool(self, line: str) -> tuple[str, str] | None:
        """Detect tool invocation in a line. Returns (tool_name, tool_input) or None."""
        for pattern, _ in self.TOOL_PATTERNS:
            match = re.search(pattern, line, re.IGNORECASE)
            if match:
                tool_name = match.group(1)
                # Try to extract input after tool name
                input_match = re.search(rf'{tool_name}\s*\(([^)]*)\)', line)
                tool_input = input_match.group(1) if input_match else ""
                # Also check for file paths or other context
                if not tool_input:
                    path_match = re.search(r'["\']([^"\']+)["\']', line)
                    if path_match:
                        tool_input = path_match.group(1)
                return (tool_name, tool_input[:200] if tool_input else "")
        return None

    def _maybe_emit_text(self, spec_id: str, phase: TaskPhase) -> None:
        """Emit accumulated text if enough time has passed (throttled)."""
        import time
        now = time.time()
        if now - self._last_text_emit_time >= self._text_emit_interval:
            self._flush_pending_text(spec_id, phase)

    def _flush_pending_text(self, spec_id: str, phase: TaskPhase) -> None:
        """Flush accumulated text lines as a single entry."""
        import time
        if self._pending_text_lines:
            # Take last 20 lines to avoid huge entries
            content = "\n".join(self._pending_text_lines[-20:])
            self.add_entry(spec_id, phase, "text", content)
            self._pending_text_lines = []
            self._last_text_emit_time = time.time()

    def add_entry(self, spec_id: str, phase: TaskPhase, entry_type: str,
                  content: str, tool_name: str | None = None,
                  tool_input: str | None = None, detail: str | None = None,
                  subphase: str | None = None) -> None:
        """Add a log entry to the appropriate phase."""
        data = self._ensure_initialized(spec_id)
        phase_key = self.PHASE_MAP.get(phase, "coding")

        entry = {
            "timestamp": datetime.now().isoformat(),
            "type": entry_type,
            "content": content,
        }

        if tool_name:
            entry["tool_name"] = tool_name
        if tool_input:
            entry["tool_input"] = tool_input
        if detail:
            entry["detail"] = detail[:5000]  # Limit detail size
        if subphase:
            entry["subphase"] = subphase

        data["phases"][phase_key]["entries"].append(entry)

        # Update phase status
        if data["phases"][phase_key]["status"] == "pending":
            data["phases"][phase_key]["status"] = "active"
            data["phases"][phase_key]["started_at"] = datetime.now().isoformat()

        self._save(data)

        # Emit WebSocket event for real-time streaming to open task detail modals
        # Format as TaskLogStreamChunk to match frontend interface
        stream_chunk = {
            "type": entry_type,
            "content": content,
            "phase": phase_key,
            "timestamp": entry["timestamp"],
        }
        # Add tool info if present
        if tool_name:
            stream_chunk["tool"] = {"name": tool_name}
            if tool_input:
                stream_chunk["tool"]["input"] = tool_input
        # Add subtask info if present (from subphase)
        if subphase:
            stream_chunk["subtask_id"] = subphase

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(emit_task_logs_stream(spec_id, stream_chunk))
        except RuntimeError:
            # No event loop running, skip WebSocket emit
            pass

    def process_line(self, spec_id: str, phase: TaskPhase, line: str) -> None:
        """Process a line of output and detect tool usage."""
        if not line.strip():
            return

        # Check for tool invocation
        tool_info = self._detect_tool(line)

        if tool_info:
            # Flush pending text before starting a new tool
            self._flush_pending_text(spec_id, phase)

            # If there was a previous tool, close it
            if self._current_tool:
                self.add_entry(
                    spec_id, phase, "tool_end",
                    f"Completed {self._current_tool}",
                    tool_name=self._current_tool,
                    detail="\n".join(self._pending_tool_output[-50:]) if self._pending_tool_output else None
                )

            # Start new tool
            tool_name, tool_input = tool_info
            self._current_tool = tool_name
            self._tool_start_time = datetime.now().isoformat()
            self._tool_input = tool_input
            self._pending_tool_output = []

            self.add_entry(
                spec_id, phase, "tool_start",
                f"Using {tool_name}",
                tool_name=tool_name,
                tool_input=tool_input
            )
        elif self._current_tool:
            # Accumulate output for current tool
            self._pending_tool_output.append(line)

            # Check for tool completion patterns
            if any(p in line.lower() for p in ['done', 'completed', 'success', 'error', 'failed']):
                # Might be end of tool, but don't close yet - let next tool close it
                pass
        else:
            # Not in a tool context - accumulate text and emit periodically
            self._pending_text_lines.append(line)
            self._maybe_emit_text(spec_id, phase)

    def set_phase_status(self, spec_id: str, phase: TaskPhase, status: str) -> None:
        """Update phase status (active, completed, failed)."""
        data = self._ensure_initialized(spec_id)
        phase_key = self.PHASE_MAP.get(phase, "coding")

        data["phases"][phase_key]["status"] = status

        if status == "active" and not data["phases"][phase_key]["started_at"]:
            data["phases"][phase_key]["started_at"] = datetime.now().isoformat()
        elif status in ("completed", "failed"):
            data["phases"][phase_key]["completed_at"] = datetime.now().isoformat()

            # Flush any pending text
            self._flush_pending_text(spec_id, phase)

            # Close any pending tool
            if self._current_tool:
                self.add_entry(
                    spec_id, phase, "tool_end",
                    f"Completed {self._current_tool}",
                    tool_name=self._current_tool,
                    detail="\n".join(self._pending_tool_output[-50:]) if self._pending_tool_output else None
                )
                self._current_tool = None
                self._pending_tool_output = []

        self._save(data)

    def finalize(self, spec_id: str, phase: TaskPhase) -> None:
        """Finalize logging - close any pending tools and flush text."""
        # Flush any pending text first
        self._flush_pending_text(spec_id, phase)

        if self._current_tool:
            self.add_entry(
                spec_id, phase, "tool_end",
                f"Completed {self._current_tool}",
                tool_name=self._current_tool,
                detail="\n".join(self._pending_tool_output[-50:]) if self._pending_tool_output else None
            )
            self._current_tool = None
            self._pending_tool_output = []


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


class AgentService:
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

    def _read_correlation_key(
        self, project_path: Path, spec_id: str
    ) -> str | None:
        """Read the upstream GitHub issue number for the correlation key (#612).

        Threads ``requirements.json -> provenance.issue_number`` into the
        durable job-state row so a build is joinable across PFactory/TFactory/
        CFactory. Best-effort: returns None when the spec has no provenance.
        """
        try:
            req_file = (
                project_path / ".aifactory" / "specs" / spec_id
                / "requirements.json"
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

    def _resolve_claude_token(self, exclude_profile_id: str | None = None) -> tuple[str | None, str | None, str | None]:
        """Resolve Claude OAuth token from profiles with fallback chain.

        Resolution order:
        1. Environment override (CLAUDE_CODE_OAUTH_TOKEN already set)
        2. Active profile from ~/.aifactory/claude-profiles.json
        3. Best available profile (excluding failed profile if provided)
        4. Fallback to ~/.claude/oauth_token

        Args:
            exclude_profile_id: Profile ID to exclude (for retry after failure)

        Returns:
            Tuple of (token, profile_id, profile_name) or (None, None, None) if no token found
        """
        import logging
        logger = logging.getLogger(__name__)

        # Check environment override first
        if "CLAUDE_CODE_OAUTH_TOKEN" in os.environ:
            # Allow failover when this "env-override" profile is excluded.
            if exclude_profile_id != "env-override":
                logger.info("[AgentService] Using CLAUDE_CODE_OAUTH_TOKEN from environment")
                return (os.environ["CLAUDE_CODE_OAUTH_TOKEN"], "env-override", "Environment Override")
            logger.info("[AgentService] Skipping environment token due to exclude_profile_id=env-override (failover enabled)")

        # Load claude-profiles.json
        profiles_file = Path(self.settings.PROJECTS_DATA_DIR) / "claude-profiles.json"
        from ..paths import get_data_file
        legacy_profiles_file = get_data_file("claude-profiles.json")
        if not profiles_file.exists() and legacy_profiles_file.exists():
            profiles_file = legacy_profiles_file
            logger.debug(f"[AgentService] Using legacy profiles file at {profiles_file}")

        if profiles_file.exists():
            try:
                data = json.loads(profiles_file.read_text())
                profiles = data.get("profiles", [])
                active_id = data.get("activeProfileId")

                # Filter usable profiles (has token, not excluded)
                usable = [
                    p for p in profiles
                    if p.get("id") != exclude_profile_id
                    and (p.get("oauthToken") or p.get("token"))  # Support both field names
                ]

                if usable:
                    # Prefer active profile if it's usable
                    for p in usable:
                        if p.get("id") == active_id:
                            token = p.get("oauthToken") or p.get("token")
                            profile_id = p.get("id")
                            profile_name = p.get("name", "Active Profile")
                            logger.info(f"[AgentService] Using active profile: {profile_name} ({profile_id})")
                            return (token, profile_id, profile_name)

                    # Use first usable profile
                    p = usable[0]
                    token = p.get("oauthToken") or p.get("token")
                    profile_id = p.get("id")
                    profile_name = p.get("name", "Default Profile")
                    logger.info(f"[AgentService] Using profile: {profile_name} ({profile_id})")
                    return (token, profile_id, profile_name)

            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"[AgentService] Failed to load claude-profiles.json: {e}")

        # Fallback to static token file
        token_file = Path.home() / ".claude" / "oauth_token"
        if token_file.exists():
            token = token_file.read_text().strip()
            logger.info("[AgentService] Using fallback token from ~/.claude/oauth_token")
            return (token, "static-fallback", "Static Token")

        logger.warning("[AgentService] No Claude token found")
        return (None, None, None)

    def _get_token_pool(self) -> Any:
        """Lazily build the Claude token pool (RFC-0016 #670).

        Double-checked locking: concurrent first-checkouts must share ONE pool,
        else each thread would build a private pool and they'd all hand out the
        same LRU credential.
        """
        if self._token_pool is None:
            with self._token_pool_build_lock:
                if self._token_pool is None:
                    from ..paths import get_data_file
                    from .claude_token_pool import ClaudeTokenPool

                    self._token_pool = ClaudeTokenPool.from_sources(
                        self.settings.PROJECTS_DATA_DIR,
                        legacy_profiles_file=get_data_file("claude-profiles.json"),
                    )
                    _log.info(
                        "[AgentService] Claude token pool built with %d distinct "
                        "credential(s)",
                        self._token_pool.size,
                    )
        return self._token_pool

    def _resolve_claude_token_pooled(
        self, task_id: str
    ) -> tuple[str | None, str | None, str | None]:
        """Check a DISTINCT credential out of the pool for ``task_id``.

        Concurrent builds get distinct tokens when several are configured; the
        single shared token otherwise (identical to the legacy single-token
        behaviour). The env-override token (CLAUDE_CODE_OAUTH_TOKEN) keeps its
        legacy precedence — but is also poolable when multiple are configured.

        Falls back to the legacy resolver if the pool is empty (e.g. a token
        source the pool can't see). The checked-out credential is returned to
        the pool by :meth:`_release_task_credential` when the build ends.
        """
        try:
            pool = self._get_token_pool()
            if not pool.is_empty():
                cred = pool.checkout(task_id)
                if cred is not None:
                    return (cred.token, cred.profile_id, cred.profile_name)
        except Exception:  # noqa: BLE001 - pool must never break a build start
            _log.warning(
                "[AgentService] token pool checkout failed; falling back to "
                "single-token resolver",
                exc_info=True,
            )
        # Fallback: legacy single-token resolution (no pool tracking to release).
        return self._resolve_claude_token()

    def _release_task_credential(self, task_id: str) -> None:
        """Return a build's pooled credential when it ends. Idempotent/no-raise.

        Also pops ``_task_profiles[task_id]`` so this can stand in for the bare
        ``_task_profiles.pop`` calls at every task-terminal site.
        """
        try:
            if self._token_pool is not None:
                self._token_pool.release(task_id)
        except Exception:  # noqa: BLE001
            _log.debug(
                "[AgentService] token pool release raised for %s (ignored)",
                task_id,
                exc_info=True,
            )
        self._task_profiles.pop(task_id, None)

    def _is_early_failure(self, spec_dir: Path, exit_code: int) -> bool:
        """Check if task failure is an early failure (no logs written).

        Early failure criteria:
        - Exit code is non-zero
        - task_logs.json either doesn't exist OR has no entries in any phase

        This indicates the agent failed immediately without making progress,
        typically due to auth/rate-limit issues.

        Args:
            spec_dir: Path to the spec directory containing task_logs.json
            exit_code: Process exit code

        Returns:
            True if this is an early failure eligible for retry
        """
        if exit_code == 0:
            return False

        task_logs_file = spec_dir / "task_logs.json"

        # If file doesn't exist, it's an early failure
        if not task_logs_file.exists():
            return True

        try:
            data = json.loads(task_logs_file.read_text())
            phases = data.get("phases", {})

            # Check if any phase has entries
            for phase_name, phase_data in phases.items():
                entries = phase_data.get("entries", [])
                if entries:
                    # Found entries - this is NOT an early failure
                    return False

            # No entries in any phase - early failure
            return True

        except (json.JSONDecodeError, OSError):
            # Can't read logs - assume early failure to be safe
            return True

    def _should_retry_with_failover(self) -> bool:
        """Check if auto-switch settings allow profile failover.

        Checks:
        - enabled: Master switch for auto-switching
        - autoSwitchOnRateLimit: Reactive recovery toggle

        Returns:
            True if both settings are enabled
        """
        import logging
        logger = logging.getLogger(__name__)

        # Primary path: ~/.aifactory/auto-switch.json
        settings_file = Path(self.settings.PROJECTS_DATA_DIR) / "auto-switch.json"

        if not settings_file.exists():
            logger.debug(f"[AgentService] Auto-switch settings not found at {settings_file}, failover disabled")
            return False

        try:
            data = json.loads(settings_file.read_text())
            enabled = data.get("enabled", False)
            auto_switch_on_rate_limit = data.get("autoSwitchOnRateLimit", False)

            if enabled and auto_switch_on_rate_limit:
                logger.info("[AgentService] Auto-switch enabled - failover allowed")
                return True
            else:
                logger.debug(f"[AgentService] Auto-switch disabled - enabled: {enabled}, autoSwitchOnRateLimit: {auto_switch_on_rate_limit}")
                return False

        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[AgentService] Failed to read auto-switch settings: {e}")
            return False

    def _is_rate_limit_line(self, line: str) -> bool:
        """Detect rate limit messages in agent output."""
        text = line.lower()
        patterns = [
            "you've hit your limit",
            "you’ve hit your limit",  # curly apostrophe
            "youve hit your limit",
        ]
        return any(p in text for p in patterns)

    async def _emit_profile_switch(
        self,
        task_id: str,
        old_profile_id: str,
        new_profile_id: str,
        new_profile_name: str,
        reason: str
    ) -> None:
        """Emit profile switch event via WebSocket.

        Args:
            task_id: Task identifier
            old_profile_id: Previous profile ID that failed
            new_profile_id: New profile ID being used
            new_profile_name: New profile display name
            reason: Reason for switch (e.g., "early_failure")
        """
        from ..websockets.events import broadcast_event

        await broadcast_event("task:profile-switch", {
            "taskId": task_id,
            "oldProfileId": old_profile_id,
            "newProfileId": new_profile_id,
            "newProfileName": new_profile_name,
            "reason": reason,
            "timestamp": datetime.now().isoformat()
        })

    def _update_active_profile(self, profile_id: str, profile_name: str, reason: str = "rate_limit") -> None:
        """Update active profile system-wide when reactive failover occurs.

        This updates the activeProfileId in claude-profiles.json so that all future
        tasks automatically use the new profile instead of repeatedly failing.

        Args:
            profile_id: ID of new profile to make active
            profile_name: Name for logging
            reason: Why the switch occurred (e.g., "rate_limit", "reactive_failover")
        """
        import logging
        logger = logging.getLogger(__name__)

        profiles_file = Path(self.settings.PROJECTS_DATA_DIR) / "claude-profiles.json"
        from ..paths import get_data_file
        legacy_profiles_file = get_data_file("claude-profiles.json")

        if not profiles_file.exists() and legacy_profiles_file.exists():
            profiles_file = legacy_profiles_file
            logger.debug(f"[AgentService] Using legacy profiles file at {profiles_file}")

        if not profiles_file.exists():
            logger.warning("[AgentService] claude-profiles.json not found, skipping active profile update")
            return

        try:
            # Read current profiles
            data = json.loads(profiles_file.read_text())
            old_active = data.get("activeProfileId")

            # Update active profile
            data["activeProfileId"] = profile_id

            # Write back with secure permissions
            profiles_file.write_text(json.dumps(data, indent=2))
            profiles_file.chmod(0o600)

            # Update env token to match active profile (if available)
            token = None
            for profile in data.get("profiles", []):
                if profile.get("id") == profile_id:
                    token = profile.get("oauthToken") or profile.get("token")
                    break

            if token:
                os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token
                logger.info("[AgentService] Updated CLAUDE_CODE_OAUTH_TOKEN for active profile")
            else:
                logger.warning("[AgentService] Active profile has no token; env not updated")

            logger.info(f"[AgentService] Updated active profile: {old_active} → {profile_id} (reason: {reason})")

            # Emit WebSocket event for system-wide profile change
            from ..websockets.events import broadcast_event
            asyncio.create_task(broadcast_event("profile:changed", {
                "oldProfileId": old_active,
                "newProfileId": profile_id,
                "newProfileName": profile_name,
                "reason": reason,
                "timestamp": datetime.now().isoformat()
            }))

        except Exception as e:
            logger.error(f"[AgentService] Failed to update active profile: {e}")

    async def _retry_task_with_fallback_model(
        self,
        task_id: str,
        project_path: Path,
        spec_id: str,
        cmd: list[str],
        env: dict,
    ) -> asyncio.subprocess.Process | None:
        """Retry task execution with Claude Sonnet as fallback model.

        Called when a non-Claude model (Codex, Gemini, Ollama) fails.
        Swaps the --model flag in the command to 'sonnet'.

        Returns:
            New subprocess or None if retry not possible
        """
        import logging
        logger = logging.getLogger(__name__)

        profile_info = self._task_profiles.get(task_id, {})
        failed_model = profile_info.get("model", "unknown")

        # Build new command with sonnet model
        new_cmd = list(cmd)
        if "--model" in new_cmd:
            model_idx = new_cmd.index("--model")
            if model_idx + 1 < len(new_cmd):
                new_cmd[model_idx + 1] = "sonnet"
        else:
            new_cmd.extend(["--model", "sonnet"])

        logger.info(f"[AgentService] [Model: sonnet] Fallback triggered for {task_id} (original: {failed_model})")

        # Emit WebSocket event for model fallback
        from ..websockets.events import broadcast_event
        await broadcast_event("task:log", {
            "taskId": task_id,
            "type": "model_fallback",
            "message": f"Model '{failed_model}' failed. Falling back to Claude Sonnet.",
        })

        # Update tracking
        if task_id in self._task_profiles:
            self._task_profiles[task_id]["model"] = "sonnet"
            self._task_profiles[task_id]["attempt"] = 2
            self._task_profiles[task_id]["fallbackFrom"] = failed_model

        # Relaunch subprocess
        import pty
        master_fd, slave_fd = pty.openpty()

        proc = await asyncio.create_subprocess_exec(
            *new_cmd,
            stdin=slave_fd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(project_path),
            env=env,
        )

        os.close(slave_fd)
        os.close(master_fd)

        return proc

    async def _retry_task_with_profile(
        self,
        task_id: str,
        project_path: Path,
        spec_id: str,
        cmd: list[str],
        env: dict,
        failed_profile_id: str,
        reason: str,
    ) -> asyncio.subprocess.Process | None:
        """Retry task execution with a different Claude profile.

        Args:
            task_id: Task identifier
            project_path: Project directory
            spec_id: Spec identifier
            cmd: Command to execute (same as original)
            env: Environment dict (will update token)
            failed_profile_id: Profile ID that failed (to exclude)

        Returns:
            New subprocess or None if retry not possible
        """
        import logging
        logger = logging.getLogger(__name__)

        # Resolve alternate token (excluding failed profile)
        token, profile_id, profile_name = self._resolve_claude_token(exclude_profile_id=failed_profile_id)

        if not token:
            logger.warning(f"[AgentService] No alternate profile available for retry (excluded: {failed_profile_id})")
            return None

        if profile_id == failed_profile_id:
            logger.warning(f"[AgentService] Only profile available is the one that failed ({failed_profile_id})")
            return None

        # Update environment with new token
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token

        # Log profile switch
        logger.info(f"[AgentService] Retrying with profile: {profile_name} ({profile_id})")

        # Emit WebSocket event for profile switch
        await self._emit_profile_switch(
            task_id=task_id,
            old_profile_id=failed_profile_id,
            new_profile_id=profile_id,
            new_profile_name=profile_name,
            reason=reason,
        )

        # Update active profile system-wide (only for rate limit, not early failure)
        if reason == "rate_limit":
            self._update_active_profile(profile_id, profile_name, reason="reactive_failover")

        # Update tracking
        if task_id in self._task_profiles:
            self._task_profiles[task_id] = {
                "profileId": profile_id,
                "profileName": profile_name,
                "attempt": 2,  # Second attempt
                "previousProfileId": failed_profile_id
            }

        # Relaunch subprocess with new token
        import pty
        master_fd, slave_fd = pty.openpty()

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=slave_fd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(project_path),
            env=env,
        )

        os.close(slave_fd)

        return proc

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
        self, task_id: str, phase: str,
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
                task_id, phase, exc_info=True,
            )

    async def _emit_progress(self, progress: TaskProgress, previous_phase: TaskPhase | None = None) -> None:
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
                                subtasks_data.append({
                                    "id": subtask.get("id", ""),
                                    "status": subtask.get("status", "pending"),
                                    "title": subtask.get("description", ""),
                                })
            except Exception as e:
                import logging
                logging.getLogger(__name__).debug(f"[AgentService] Could not read subtasks for {progress.task_id}: {e}")

            await self._safe_emit_task_update(progress.task_id, {
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
            })

            # If phase changed, also emit status change for kanban column movement
            if previous_phase is not None and progress.phase != previous_phase:
                new_status = phase_to_status(progress.phase)
                review_reason = phase_to_review_reason(progress.phase)
                await self._safe_emit_task_status(progress.task_id, new_status, review_reason)

        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"[AgentService] WebSocket broadcast failed: {e}")

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
                json_str = line[len(exec_phase_prefix):]
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
                logger.warning(f"[AgentService] Rate limit detected for task {task_id} (will attempt failover if enabled)")

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
                            log_writer.set_phase_status(spec_id, current_phase, "active")
                        # Ensure validation phase is properly marked completed when task completes
                        if current_phase == TaskPhase.COMPLETED and old_phase in (TaskPhase.QA_REVIEW, TaskPhase.QA_FIXING):
                            log_writer.set_phase_status(spec_id, old_phase, "completed")

                # Always emit progress for phase events (even if phase didn't change)
                progress = TaskProgress(
                    task_id=task_id,
                    phase=current_phase,
                    message=event.get("message", ""),
                    subtask=event.get("subtask"),
                    subtask_index=int(event["subtask_index"]) if "subtask_index" in event else None,
                    subtask_total=int(event["subtask_total"]) if "subtask_total" in event else None,
                    percentage=event.get("percentage"),  # Include percentage from event
                    data=event,
                )
                # Pass previous phase if it changed, so status event can be emitted
                await self._emit_progress(progress, previous_phase=old_phase if old_phase != current_phase else None)

            # Check for JSON progress data
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    if "phase" in data or "status" in data:
                        phase_str = data.get("phase", data.get("status", ""))
                        if phase_str in ["coding", "planning", "qa_review", "qa_fixing"]:
                            old_phase = current_phase
                            current_phase = TaskPhase(phase_str)

                            # Track current phase for proper status on task completion
                            self._task_current_phases[task_id] = current_phase

                            # Update log writer phase status
                            if log_writer and spec_id:
                                if old_phase != current_phase:
                                    log_writer.set_phase_status(spec_id, old_phase, "completed")
                                log_writer.set_phase_status(spec_id, current_phase, "active")

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
                        await self._emit_progress(progress, previous_phase=old_phase if old_phase != current_phase else None)
                except json.JSONDecodeError:
                    pass

        return current_phase

    async def _sync_worktree_files(self, project_path: Path, spec_id: str, task_id: str | None = None) -> None:
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
        worktree_spec = project_path / ".aifactory" / "worktrees" / "tasks" / spec_id / ".aifactory" / "specs" / spec_id
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
                            STATUS_ORDER = {"pending": 0, "in_progress": 1, "completed": 2, "failed": 2}
                            main_subtask_statuses = {}
                            for phase in main_plan.get("phases", []):
                                for subtask in phase.get("subtasks", []):
                                    sid = subtask.get("id")
                                    if sid:
                                        main_subtask_statuses[sid] = subtask.get("status", "pending")

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
                                        main_rank = STATUS_ORDER.get(main_subtask_statuses[sid], 0)
                                        wt_rank = STATUS_ORDER.get(subtask.get("status", "pending"), 0)
                                        if main_rank > wt_rank:
                                            subtask["status"] = main_subtask_statuses[sid]

                            dst.write_text(json.dumps(merged_plan, indent=2))
                        except (json.JSONDecodeError, OSError) as merge_err:
                            # Even the error path must not reintroduce control
                            # fields: strip them before copying the raw worktree
                            # plan in.
                            logger.warning(f"[AgentService] Failed to merge implementation_plan.json, falling back to stripped copy: {merge_err}")
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
                        logger.warning(f"[AgentService] Failed to sync extra file {src_file.name}: {e}")
        except OSError as e:
            logger.warning(f"[AgentService] Failed to scan worktree spec dir for extra files: {e}")

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
                    logger.warning(f"[AgentService] Failed to sync directory {dirname}: {e}")

        if synced_count > 0:
            logger.debug(f"[AgentService] Synced {synced_count} files from worktree to main spec dir")

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
                        with bp_main.open("r", encoding="utf-8", errors="replace") as fh:
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

                completed = sum(1 for s in all_subtasks if s.get("status") == "completed")
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
                    {"id": s.get("id"), "status": s.get("status")}
                    for s in all_subtasks
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
                            previous_status=previous_status
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
                    actual_phase = self._task_current_phases.get(task_id, TaskPhase.PLANNING).value if task_id else "coding"
                    await self._safe_emit_task_update(
                        task_id or spec_id,
                        {
                            "executionProgress": {
                                "phase": actual_phase,
                                "phaseProgress": progress,
                                "overallProgress": scale_progress(actual_phase, progress),
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

    async def _monitor_process(
        self,
        task_id: str,
        proc: asyncio.subprocess.Process,
        project_path: Path | None = None,
        spec_id: str | None = None,
        cmd: list[str] | None = None,
        env: dict | None = None
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

        await monitor_process(
            self, task_id, proc, project_path, spec_id, cmd, env
        )

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
                task_id, exc_info=True,
            )
        try:
            await self._store().mark_terminal(task_id, lifecycle, error=error)
        except Exception:  # noqa: BLE001 - never break the exit/drain path
            _log.exception(
                "[AgentService] could not free durable slot for %s", task_id
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
        plan_file = project_path / ".aifactory" / "specs" / spec_id / "implementation_plan.json"
        logger.info(f"[AgentService._update_plan_status] CALLED for spec_id={spec_id}, status={status}, task_id={task_id}")
        logger.info(f"[AgentService._update_plan_status] plan_file path: {plan_file}")
        logger.info(f"[AgentService._update_plan_status] plan_file exists: {plan_file.exists()}")
        if not plan_file.exists():
            logger.warning("[AgentService._update_plan_status] plan_file does not exist, returning early")
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
                logger.info(f"[AgentService._update_plan_status] Status is 'done' (user-set), skipping overwrite for {spec_id}")
                return

            # Fix 2: Validate that the plan is not just a minimal status object
            # A valid plan should have phases and subtasks from spec creation
            if "phases" not in plan or not plan.get("phases"):
                logger.error(f"[AgentService] Invalid or minimal implementation plan detected for {spec_id}")
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

            logger.info(f"[AgentService._update_plan_status] About to write file with status={plan.get('status')}, reviewReason={plan.get('reviewReason')}")
            plan_file.write_text(json.dumps(plan, indent=2))
            logger.info("[AgentService._update_plan_status] Successfully wrote plan_file")

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
            logger.info(f"[AgentService] Updated plan status to '{plan['status']}' for {spec_id}")

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
                is_terminal=phase_enum in (TaskPhase.COMPLETED, TaskPhase.FAILED),
                is_completed=phase_enum == TaskPhase.COMPLETED,
                terminal_status=(
                    "completed" if phase_enum == TaskPhase.COMPLETED else "failed"
                ),
                logger=logger,
            )

            # Extract subtasks for WebSocket broadcast
            subtasks_data = []
            phases = plan.get("phases", [])
            for phase in phases:
                phase_subtasks = phase.get("subtasks", [])
                for subtask in phase_subtasks:
                    subtasks_data.append({
                        "id": subtask.get("id", ""),
                        "status": subtask.get("status", "pending"),
                        "title": subtask.get("description", ""),
                    })

            # Emit WebSocket events so frontend updates in real-time. Skipped
            # at the terminal exit branch (Issue #14) — the _monitor_process
            # caller will emit a single canonical _emit_progress(COMPLETED|FAILED)
            # that fires both task:update and task:status itself.
            if emit_events:
                review_reason = plan.get("reviewReason")
                # First emit status change
                await self._safe_emit_task_status(task_id, plan["status"], review_reason)
                # Then emit task update with subtasks so they appear immediately
                # in UI. Payload is ENRICHED with an executionProgress block (Issue #14)
                # so the frontend's log doesn't render `phase: N/A` and the store
                # receives a coherent terminal phase value.
                completed_count = sum(1 for s in subtasks_data if s["status"] == "completed")
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
                    fallback_status = phase_to_status(phase_enum) if phase_enum else status
                    fallback_reason = phase_to_review_reason(phase_enum) if phase_enum else None
                    await self._safe_emit_task_status(task_id, fallback_status, fallback_reason)
                except Exception:
                    logger.error(f"[AgentService] Failed to emit fallback task:status for {task_id}")

    def _write_skill_context(self, spec_dir: Path) -> None:
        """Write skill_context.md to spec_dir based on selectedSkills in task_metadata.json.

        If selectedSkills is non-empty, loads up to 5 skill files and writes them
        as a structured markdown file that the agent system will auto-include as
        context (the agent reads all .md files in spec_dir).

        If no skills are selected, removes any existing skill_context.md.
        """
        import logging
        logger = logging.getLogger(__name__)

        skill_context_file = spec_dir / "skill_context.md"
        task_metadata_file = spec_dir / "task_metadata.json"

        # Load task metadata to get selected skills
        selected_skill_ids: list[str] = []
        if task_metadata_file.exists():
            try:
                task_metadata = json.loads(task_metadata_file.read_text())
                # Hybrid skill selection (#394): prefer the planner-confirmed
                # selectedSkills; fall back to the auto-proposed suggestedSkills
                # so relevant skills are always applied even if the planner
                # didn't refine them.
                raw_skills = task_metadata.get("selectedSkills") or task_metadata.get(
                    "suggestedSkills", []
                )
                # skills are stored as list[dict] with {id, name, category, source}
                # Also handle plain string IDs for backward compatibility
                for item in raw_skills:
                    if isinstance(item, dict):
                        sid = item.get("id", "")
                    else:
                        sid = str(item)
                    if sid:
                        selected_skill_ids.append(sid)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"[AgentService] Could not read task_metadata.json for skills: {e}")

        # If no skills selected, remove any existing skill_context.md
        if not selected_skill_ids:
            if skill_context_file.exists():
                try:
                    skill_context_file.unlink()
                    logger.info("[AgentService] Removed skill_context.md (no skills selected)")
                except OSError as e:
                    logger.warning(f"[AgentService] Could not remove skill_context.md: {e}")
            return

        # Load skill contents (max 5 skills to stay within token budget)
        from .skills_service import get_skills_service
        skills_service = get_skills_service()

        sections: list[str] = []
        loaded_count = 0

        for skill_id in selected_skill_ids[:5]:
            # Parse skill_id format: "{category}/{skill_name}"
            if "/" not in skill_id:
                logger.warning(f"[AgentService] Invalid skill_id format (missing '/'): {skill_id}")
                continue

            category, name = skill_id.split("/", 1)
            skill_summary = skills_service.get_skill(category, name)
            skill_content = skills_service.get_skill_content(category, name)

            if skill_content is None:
                logger.warning(f"[AgentService] Skill not found in index: {skill_id}")
                continue

            # Truncate each skill to 2500 chars to manage token budget
            skill_content_truncated = skill_content[:2500]
            if len(skill_content) > 2500:
                skill_content_truncated += "\n\n*[Content truncated for token budget]*"

            display_name = skill_summary.name if skill_summary else name
            sections.append(
                f"## {display_name} ({category})\n\n"
                f"{skill_content_truncated}\n\n"
                "---"
            )
            loaded_count += 1

        if not sections:
            # No skills could be loaded — clean up stale file if present
            if skill_context_file.exists():
                try:
                    skill_context_file.unlink()
                except OSError:
                    pass
            return

        # Format as structured markdown
        header = (
            "# Selected Skills Context\n\n"
            "The following skill documentation has been included to assist with this task.\n"
            "Reference these skills when implementing the solution.\n\n"
            "---"
        )
        skill_context_content = header + "\n\n" + "\n\n".join(sections) + "\n"

        try:
            spec_dir.mkdir(parents=True, exist_ok=True)
            skill_context_file.write_text(skill_context_content, encoding="utf-8")
            logger.info(f"[AgentService] Wrote skill_context.md with {loaded_count} skill(s)")
        except OSError as e:
            logger.error(f"[AgentService] Failed to write skill_context.md: {e}")

    async def start_spec_creation(
        self,
        task_id: str,
        project_path: Path,
        title: str,
        description: str,
        complexity: str | None = None,
        auto_continue: bool = True,
        user_id: str = "",
    ) -> asyncio.subprocess.Process:
        """Start spec creation for a task."""
        import logging
        logger = logging.getLogger(__name__)
        if task_id in self.running_tasks:
            raise ValueError(f"Task {task_id} is already running")

        # Parse spec_id from task_id (format: "project_id:spec_id")
        if ":" in task_id:
            _, spec_id = task_id.split(":", 1)
            spec_dir = project_path / ".aifactory" / "specs" / spec_id
        else:
            # Fallback: no project ID prefix (shouldn't happen in web mode)
            spec_dir = None

        # Fix 5: Check if task requires manual review before coding
        # If requireReviewBeforeCoding is true, DON'T auto-approve (let user review the plan)
        should_auto_approve = True  # Default for web mode
        spec_phase_model = None  # Model for spec creation phase
        if spec_dir:
            task_metadata_file = spec_dir / "task_metadata.json"
            if task_metadata_file.exists():
                try:
                    import json
                    metadata = json.loads(task_metadata_file.read_text())
                    if metadata.get("requireReviewBeforeCoding", False):
                        should_auto_approve = False
                        logger.info(f"[AgentService] Task {task_id} requires manual review - NOT auto-approving spec")
                    # Read spec phase model from auto profile config
                    if metadata.get("isAutoProfile") and metadata.get("phaseModels"):
                        spec_phase_model = metadata["phaseModels"].get("spec")
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(f"[AgentService] Failed to read task_metadata.json: {e}")

            # PFactory governed specs (epic #327 / #329): PFactory already ran its
            # architecture/security/best-practice/feasibility gates AND a human
            # approved the plan, so AIFactory skips its own up-front plan-review
            # gate and proceeds straight to execution planning — force
            # auto-approve, overriding any requireReviewBeforeCoding.
            requirements_file = spec_dir / "requirements.json"
            if requirements_file.exists():
                try:
                    import json

                    backend_path = str(self.backend_path)
                    if backend_path not in sys.path:
                        sys.path.insert(0, backend_path)
                    from pfactory.taxonomy import is_governed_requirements

                    requirements = json.loads(requirements_file.read_text())
                    if is_governed_requirements(requirements):
                        should_auto_approve = True
                        logger.info(
                            f"[AgentService] Task {task_id} is a governed PFactory "
                            "spec — auto-approving (skipping plan-review gate)"
                        )
                except (json.JSONDecodeError, OSError, ImportError) as e:
                    logger.warning(
                        f"[AgentService] PFactory governance check failed for {task_id}: {e}"
                    )

        # Build command
        cmd = [
            sys.executable,
            str(self.backend_path / "runners" / "spec_runner.py"),
            "--task", f"{title}\n\n{description}",
            "--project-dir", str(project_path),
        ]

        # Pass spec phase model if configured (multi-model support)
        if spec_phase_model:
            cmd.extend(["--model", spec_phase_model])
            logger.info(f"[AgentService] [Model: {spec_phase_model}] Starting spec creation for {task_id}")
        else:
            logger.info(f"[AgentService] [Model: sonnet] Starting spec creation for {task_id} (default)")

        # Fix 1: Only auto-approve if task doesn't require manual review
        if should_auto_approve:
            cmd.append("--auto-approve")

        # Fix 4: Pass existing spec directory to prevent duplicate task creation
        if spec_dir:
            cmd.extend(["--spec-dir", str(spec_dir)])

        if complexity:
            cmd.extend(["--complexity", complexity])

        # Set environment — scrub ANTHROPIC_API_KEY so spawned subprocesses
        # can never silently bill the direct-API account (OAuth-only policy;
        # see apps/backend/core/auth.py).
        env = make_subprocess_env()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        # Run Claude in non-interactive mode - bypass permission prompts
        env["CLAUDE_CODE_ENTRYPOINT"] = "cli"  # Signal non-interactive mode
        env["CI"] = "true"  # Many CLI tools use this to detect non-interactive mode

        # Quick Mode for simple tasks (safety net if simple task reaches spec creation)
        if complexity == "simple":
            env["QUICK_MODE"] = "true"
            logger.info(f"[AgentService] Quick Mode enabled for spec creation task {task_id}")

        # Load backend .env file for graphiti and other settings
        backend_env_file = self.backend_path / ".env"
        if backend_env_file.exists():
            try:
                with open(backend_env_file) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            key, value = line.split('=', 1)
                            key = key.strip()
                            value = value.strip()
                            # Don't override existing env vars
                            if key not in env:
                                env[key] = value
                logger.info("[AgentService] Loaded backend .env for spec creation")
            except Exception as e:
                logger.warning(f"[AgentService] Failed to load backend .env: {e}")

        # Load project .aifactory/.env for project-level settings (USE_CLAUDE_MD, etc.)
        project_env_file = project_path / ".aifactory" / ".env"
        if project_env_file.exists():
            try:
                with open(project_env_file) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            key, value = line.split('=', 1)
                            key = key.strip()
                            value = value.strip()
                            if key not in env:
                                env[key] = value
                logger.info("[AgentService] Loaded project .env for spec creation")
            except Exception as e:
                logger.warning(f"[AgentService] Failed to load project .env: {e}")

        # Get OAuth token from the pool (#670) so concurrent builds draw DISTINCT
        # credentials; returned to the pool when this build ends.
        token, profile_id, profile_name = self._resolve_claude_token_pooled(task_id)
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
            logger.info(
                f"[AgentService] Using Claude profile for spec creation: {profile_name} ({profile_id})"
            )
            # Store for potential retry tracking
            self._task_profiles[task_id] = {
                "profileId": profile_id,
                "profileName": profile_name,
                "attempt": 1,
                "model": spec_phase_model or "sonnet",
            }
        else:
            logger.warning("[AgentService] No Claude OAuth token available for spec creation")
            self._task_profiles[task_id] = {"attempt": 1, "model": spec_phase_model or "sonnet"}

        # Start subprocess with a pseudo-TTY to prevent "Stream closed" errors
        # Claude Code CLI expects a TTY for permission handling
        import pty

        master_fd, slave_fd = pty.openpty()

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
            # Own session/process group so stop_task can kill the WHOLE tree —
            # run.py spawns coder subprocesses (Claude SDK, git); without this,
            # proc.terminate() only signals run.py and the children orphan and
            # keep running (the stop-resistance bug).
            start_new_session=True,
        )

        # Close slave fd in parent process
        os.close(slave_fd)

        self.running_tasks[task_id] = proc

        # Initialize tracking for sequence numbers and start time
        self._task_sequence_numbers[task_id] = 0
        self._task_start_times[task_id] = datetime.now().isoformat()
        if user_id:
            self._task_user_ids[task_id] = user_id
        # Store spec directory for reading implementation plans during progress updates
        self._spec_dirs[task_id] = spec_dir

        # Emit initial progress (50% within spec_creation phase → 10% overall)
        await self._emit_progress(TaskProgress(
            task_id=task_id,
            phase=TaskPhase.SPEC_CREATION,
            message="Starting spec creation...",
            percentage=50,
        ))

        # Start output processing in background
        asyncio.create_task(self._process_output(task_id, proc.stdout, is_stderr=False))
        asyncio.create_task(self._process_output(task_id, proc.stderr, is_stderr=True))

        # Start process monitor to clean up when finished
        # Pass project_path so monitor can detect created spec and check for review state
        # Pass cmd and env so model fallback can retry with a different model on failure
        asyncio.create_task(self._monitor_process(task_id, proc, project_path=project_path, cmd=cmd, env=env))

        # Epic #44 — Live Console also covers the spec-creation phase, not just
        # the build phase, so the whole agent run is streamable. No-op when
        # rmux is off; _process_output tees this subprocess's output into the
        # passive FIFO. The build phase re-uses the same spec_id session.
        from ..rmux.integration import create_if_enabled as _rmux_create
        try:
            await _rmux_create(spec_id, project_path, " ".join(cmd))
        except Exception:
            logger.warning(f"[AgentService] rmux create hook (spec creation) raised (ignored); spec_id={spec_id}")

        return proc

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
                "[AgentService] At concurrency cap (%d running, cap=%d) — "
                "queued task %s (position %d)",
                len(self.running_tasks),
                cap,
                task_id,
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
                if self._kubejob_backend_enabled():
                    await self._dispatch_build_job(
                        task_id=task_id,
                        project_path=project_path,
                        spec_id=spec_id,
                        correlation_key=correlation_key,
                    )
                    # No in-pod Process — the Job owns execution and reports its
                    # own terminal state; the control plane reconciles by poll.
                    return None
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
            except Exception:
                await self._store().mark_terminal(
                    task_id, "failed", error="spawn failed during admission"
                )
                raise

        # outcome == "queued": park + surface the queued status for the cockpit.
        await self._mark_task_queued(task_id, project_path, spec_id)
        return None

    def _kubejob_backend_enabled(self) -> bool:
        """True when builds run as a k8s Job (RFC-0016 #671 control/exec split).

        Env-gated, default OFF (``AIFACTORY_BUILD_BACKEND=subprocess``). The
        kubejob backend requires the durable store (it reconciles by polling
        Postgres + reaps via worker_ref), so when it is requested WITHOUT a
        DATABASE_URL we log loudly and fall back to the in-pod subprocess rather
        than silently stranding builds with no reconcile loop.
        """
        from .build_backend import kubejob_enabled

        if not kubejob_enabled():
            return False
        if not self._store_enabled:
            _log.warning(
                "[AgentService] AIFACTORY_BUILD_BACKEND=kubejob requires the "
                "durable job-state store (DATABASE_URL) for reconcile/reap — "
                "it is unset; falling back to the in-pod subprocess backend."
            )
            return False
        return True

    def _build_backend(self) -> Any:
        """Lazily build + cache the k8s-Job build backend (RFC-0016 #671)."""
        if getattr(self, "_kubejob_build_backend", None) is None:
            from .build_backend import KubeJobBuildBackend
            self._kubejob_build_backend = KubeJobBuildBackend(self._store())
        return self._kubejob_build_backend

    async def _dispatch_build_job(
        self,
        *,
        task_id: str,
        project_path: Path,
        spec_id: str,
        correlation_key: str | None,
    ) -> None:
        """Dispatch a k8s Job that runs run.py for this build (RFC-0016 #671).

        The durable slot is already reserved (the row is ``running`` with
        ``worker_ref={kind:subprocess}``); the backend overwrites worker_ref
        with the k8s-job reference so the reconcile-by-poll + reaper loops can
        find the Job. The Job flips the row to its terminal state when it
        finishes — we do not block on it. Surfaces the coding status so the
        cockpit shows the build moving even though no in-pod process exists.

        #671 OAuth-env defect: a dispatched Job is a fresh pod that inherits none
        of the control-plane env, so we resolve the build's credential from the
        SAME token pool the in-pod path uses (#670) — concurrent Jobs draw
        DISTINCT tokens — and hand it to the backend, which injects it (plus the
        provider/runtime SDK env) into the Job container env (never argv). Without
        it run.py started but died ``No OAuth token found``. The pooled credential
        is released when the Job reaches a terminal state (reconcile / reap /
        stop), mirroring the subprocess path's _release_task_credential.
        """
        # Pooled credential checkout (#670) — distinct token per concurrent Job.
        token, profile_id, profile_name = self._resolve_claude_token_pooled(task_id)
        if token:
            self._task_profiles[task_id] = {
                "profileId": profile_id,
                "profileName": profile_name,
                "attempt": 1,
            }
            _log.info(
                "[AgentService] kubejob build %s using Claude profile %s (%s)",
                task_id, profile_name, profile_id,
            )
        else:
            _log.warning(
                "[AgentService] no Claude OAuth token available for kubejob "
                "build %s — run.py will fail with 'No OAuth token found'",
                task_id,
            )
        try:
            await self._build_backend().dispatch(
                task_id=task_id,
                project_path=project_path,
                spec_id=spec_id,
                correlation_key=correlation_key,
                oauth_token=token,
            )
        except Exception:
            # Dispatch failed → the Job will never run, so return the credential
            # now rather than leaking it until a reaper that never fires.
            self._release_task_credential(task_id)
            raise
        # RFC-0017 #680: feed the cockpit log stream + rmux Live Console from the
        # Job pod's logs, exactly as the in-pod subprocess path does — the
        # prerequisite to making kubejob the default. Best-effort: any failure
        # here never affects dispatch or reconcile.
        await self._start_kubejob_log_stream(
            task_id=task_id, project_path=project_path, spec_id=spec_id
        )
        try:
            await self._safe_emit_task_status(task_id, "in_progress")
        except Exception:  # noqa: BLE001 - status emit must not break dispatch
            _log.debug(
                "[AgentService] coding status emit raised after Job dispatch "
                "(ignored)", exc_info=True,
            )

    async def _start_kubejob_log_stream(
        self, *, task_id: str, project_path: Path, spec_id: str
    ) -> None:
        """Start Job-native log streaming for a dispatched build (#680).

        Creates the passive rmux session (so the Live Console pane FIFO exists
        for viewers, mirroring the in-pod path's ``create_if_enabled``) and
        spawns a background task that follows the build Job's pod logs into the
        cockpit log sink + the rmux feed. The Job ref (namespace/job_name) is
        read from the durable worker_ref the backend just wrote. Wholly
        best-effort — never raises, never blocks dispatch.
        """
        ref = await self._kubejob_worker_ref(task_id)
        if ref is None:
            return
        namespace, job_name = ref

        # Passive rmux session so the WS bridge has a pane FIFO to stream from.
        try:
            from ..rmux.integration import create_if_enabled as _rmux_create

            project_id = task_id.split(":", 1)[0] if ":" in task_id else None
            await _rmux_create(spec_id, project_path, "", project_id=project_id)
        except Exception:  # noqa: BLE001 - rmux session is optional; degrade
            _log.debug(
                "[AgentService] rmux create for kubejob build raised (ignored); "
                "spec_id=%s", spec_id, exc_info=True,
            )

        from .build_log_stream import KubeJobLogStreamer

        async def _cockpit_sink(line: str) -> None:
            await self._emit_log(
                TaskLog(task_id=task_id, content=line, source="stdout", level="info")
            )

        rmux_feed = self._kubejob_rmux_feed()
        streamer = KubeJobLogStreamer(log_sink=_cockpit_sink, rmux_feed=rmux_feed)

        async def _run_stream() -> None:
            try:
                await streamer.stream(
                    namespace=namespace, job_name=job_name, spec_id=spec_id
                )
            finally:
                self._kubejob_log_streamers.pop(task_id, None)

        self._cancel_kubejob_log_stream(task_id)
        self._kubejob_log_streamers[task_id] = asyncio.create_task(
            _run_stream(), name=f"kubejob-log-stream-{task_id}"
        )

    async def _kubejob_worker_ref(self, task_id: str) -> tuple[str, str] | None:
        """Read (namespace, job_name) from the build's durable worker_ref (#680).

        Returns None when the row/ref is missing or not a k8s-job — Job-native
        log streaming is simply skipped (the build is unaffected).
        """
        try:
            state = await self._store().get_state(task_id)
        except Exception:  # noqa: BLE001 - store read failure → skip streaming
            _log.debug(
                "[AgentService] worker_ref read failed for %s (no log stream)",
                task_id, exc_info=True,
            )
            return None
        if not state:
            return None
        ref = state.get("worker_ref") or {}
        if ref.get("kind") != "k8s-job":
            return None
        namespace = ref.get("namespace")
        job_name = ref.get("job_name")
        if not namespace or not job_name:
            return None
        return str(namespace), str(job_name)

    @staticmethod
    def _kubejob_rmux_feed() -> Any:
        """Return the rmux feed callable, or None when rmux is off (#680)."""
        try:
            from ..rmux.integration import feed_if_enabled as _feed
            from ..rmux.integration import is_enabled as _on

            return _feed if _on() else None
        except Exception:  # noqa: BLE001 - rmux integration optional
            return None

    def _cancel_kubejob_log_stream(self, task_id: str) -> None:
        """Cancel + drop a build's Job-native log streamer if present (#680)."""
        streamer = self._kubejob_log_streamers.pop(task_id, None)
        if streamer is not None and not streamer.done():
            streamer.cancel()

    async def _reap_kubejob_console(self, task_id: str) -> None:
        """Reap the passive rmux pane created for a kubejob build (#680).

        ``spec_id`` is the suffix of the composite ``task_id``. No-op + never
        raises when rmux is off or no session exists.
        """
        spec_id = task_id.split(":", 1)[1] if ":" in task_id else task_id
        try:
            from ..rmux.integration import reap_if_enabled as _rmux_reap

            await _rmux_reap(spec_id)
        except Exception:  # noqa: BLE001 - console reap is best-effort
            _log.debug(
                "[AgentService] rmux reap for kubejob build raised (ignored); "
                "spec_id=%s", spec_id, exc_info=True,
            )

    async def reconcile_kubejob_builds(self) -> dict[str, str]:
        """Reconcile k8s-Job builds from durable state (RFC-0016 #671).

        The conventions' reconcile-by-poll: for each ``running`` k8s-job row,
        observe whether the Job wrote a terminal lifecycle_state and, if so,
        drain any freed slot. A missed completion event therefore never strands
        a build. Returns ``{job_id: terminal_state}`` for builds that reached a
        terminal state this pass. No-op unless the kubejob backend is enabled.
        """
        out: dict[str, str] = {}
        if not self._kubejob_backend_enabled():
            return out
        try:
            rows = await self._store().get_active_kubejobs()
        except Exception:  # noqa: BLE001 - reconcile must never crash the loop
            _log.exception("[AgentService] kubejob reconcile: store read failed")
            return out
        backend = self._build_backend()
        for row in rows:
            job_id = row["job_id"]
            try:
                terminal = await backend.reconcile_by_poll(job_id)
            except Exception:  # noqa: BLE001
                _log.exception(
                    "[AgentService] kubejob reconcile failed for %s", job_id
                )
                continue
            if terminal is not None:
                out[job_id] = terminal
                # RFC-0017 #680: the Job is terminal → its pod log stream is
                # ending; cancel the streamer + reap the rmux pane so we don't
                # leak the background task or the console FIFO.
                self._cancel_kubejob_log_stream(job_id)
                await self._reap_kubejob_console(job_id)
                # #671 OAuth-env: return the pooled credential checked out at
                # dispatch now that the Job is done (mirrors the subprocess path).
                self._release_task_credential(job_id)
        if out:
            # Builds finished → fill freed slots from the FIFO queue.
            await self._drain_queue()
        return out

    async def kubejob_reconcile_loop(
        self, *, stop: "asyncio.Event", interval_seconds: float = 15.0
    ) -> None:
        """Periodic reconcile-by-poll + reaper for k8s-Job builds (#671).

        Started from the app lifespan only when the kubejob backend is enabled.
        Each tick polls Postgres for terminal transitions the Jobs wrote
        (so a missed completion event never strands a build) and reaps vanished
        Jobs. Never raises — a bad tick is logged and the loop continues.
        """
        _log.info(
            "[AgentService] kubejob reconcile loop started (interval=%.0fs)",
            interval_seconds,
        )
        while not stop.is_set():
            try:
                await self.reconcile_kubejob_builds()
                await self.reap_kubejob_builds()
            except Exception:  # noqa: BLE001 - loop must survive a bad tick
                _log.exception("[AgentService] kubejob reconcile tick failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                pass
        _log.info("[AgentService] kubejob reconcile loop stopped")

    async def reap_kubejob_builds(
        self, *, deadline_seconds: int | None = None
    ) -> list[str]:
        """Reap stranded k8s-Job builds (RFC-0016 #671 reaper).

        Marks a ``running`` k8s-job row ``failed`` when its Job disappears /
        exceeds the deadline without a terminal write, then drains. Returns the
        reaped job_ids. No-op unless the kubejob backend is enabled.
        """
        if not self._kubejob_backend_enabled():
            return []
        try:
            reaped = await self._build_backend().reap_vanished_jobs(
                deadline_seconds=deadline_seconds
            )
        except Exception:  # noqa: BLE001 - reaper must never crash the loop
            _log.exception("[AgentService] kubejob reaper failed")
            return []
        if reaped:
            # #671 OAuth-env: return each reaped build's pooled credential.
            for job_id in reaped:
                self._release_task_credential(job_id)
            await self._drain_queue()
        return reaped

    async def _stop_kubejob_build(self, task_id: str) -> bool:
        """Stop a build running as a k8s Job (RFC-0016 #671).

        Deletes the Job (best-effort) and marks the durable row failed so the
        slot frees and the reconcile/reaper loops don't keep watching it.
        Returns True when a running k8s-job row was found + stopped.
        """
        try:
            state = await self._store().get_state(task_id)
        except Exception:  # noqa: BLE001
            _log.warning(
                "[AgentService] could not read state to stop kubejob %s",
                task_id, exc_info=True,
            )
            return False
        if state is None or state.get("lifecycle_state") != "running":
            return False
        if (state.get("worker_ref") or {}).get("kind") != "k8s-job":
            return False
        # RFC-0017 #680: stop the Job-native log streamer + reap the console
        # pane before deleting the Job (the pod log stream is about to vanish).
        self._cancel_kubejob_log_stream(task_id)
        await self._reap_kubejob_console(task_id)
        try:
            await self._build_backend().delete_job(task_id)
        except Exception:  # noqa: BLE001 - delete is best-effort; row mark below frees the slot
            _log.warning(
                "[AgentService] could not delete k8s Job for %s (ignored)",
                task_id, exc_info=True,
            )
        try:
            await self._store().mark_terminal(
                task_id, "failed", error="stopped by user"
            )
        except Exception:  # noqa: BLE001
            _log.warning(
                "[AgentService] could not free durable slot on kubejob stop %s",
                task_id, exc_info=True,
            )
        # #671 OAuth-env: return the pooled credential checked out at dispatch.
        self._release_task_credential(task_id)
        await self._safe_emit_task_status(task_id, "human_review", "errors")
        await self._drain_queue()
        _log.info("[AgentService] Stopped k8s-Job build %s", task_id)
        return True

    async def _mark_task_queued(
        self, task_id: str, project_path: Path, spec_id: str
    ) -> None:
        """Persist + emit the ``queued`` status for a parked build (#668)."""
        spec_dir = project_path / ".aifactory" / "specs" / spec_id
        try:
            task_control.write_control(
                spec_dir,
                status="queued",
                clear_review_reason=True,
                updated_by="web_server",
            )
        except OSError as e:
            _log.warning(
                "[AgentService] could not persist queued status for %s: %s", task_id, e
            )
        try:
            await emit_task_status(task_id, "queued")
        except Exception:  # noqa: BLE001 - status emit must not break admission
            _log.debug(
                "[AgentService] queued status emit raised (ignored)", exc_info=True
            )

    async def _drain_queue(self) -> None:
        """Start queued builds FIFO while there is free capacity (#668).

        Called from the subprocess-exit monitor after a build finishes so a
        freed slot pulls the next queued task. Guarded by the admission lock so
        it can never race with ``start_task_execution`` on the same slot. Each
        spawn happens under the lock, keeping ``running_tasks`` counting exact;
        a failed spawn drops that task and moves on (no busy-wait, no requeue
        loop). Never raises — it runs in the monitor's teardown path.
        """
        if self._store_enabled:
            await self._drain_queue_durable()
            return

        async with self._admission_lock:
            cap = self._concurrency_cap()
            while self._task_queue and (cap <= 0 or len(self.running_tasks) < cap):
                qt = self._task_queue.popleft()
                # Skip a task that was started/stopped out of band while queued.
                if qt.task_id in self.running_tasks:
                    continue
                try:
                    await self._spawn_task_execution(
                        task_id=qt.task_id,
                        project_path=qt.project_path,
                        spec_id=qt.spec_id,
                        auto_continue=qt.auto_continue,
                        base_branch=qt.base_branch,
                        mode=qt.mode,
                        force=qt.force,
                        user_id=qt.user_id,
                        stop_after_planning=qt.stop_after_planning,
                        parallel=qt.parallel,
                        workers=qt.workers,
                    )
                    _log.info(
                        "[AgentService] Dequeued + started %s (%d running, %d queued)",
                        qt.task_id,
                        len(self.running_tasks),
                        len(self._task_queue),
                    )
                except Exception:  # noqa: BLE001 - one bad spawn must not stall the queue
                    _log.exception(
                        "[AgentService] failed to start queued task %s — dropping",
                        qt.task_id,
                    )

    async def _drain_queue_durable(self) -> None:
        """Promote queued builds into freed slots via the durable store (#668).

        The store's ``drain`` runs the same SELECT ... FOR UPDATE + advisory
        lock as ``admit``, so a finishing build in this replica and a fresh
        admit in another can't both claim the freed slot. Each promoted row
        comes back already flipped to ``running`` in Postgres; we then spawn the
        local subprocess. A spawn failure frees the durable slot (marks the row
        failed) so a slot is never leaked. Never raises (monitor teardown path).
        """
        cap = self._concurrency_cap()
        try:
            promoted = await self._store().drain(cap)
        except Exception:  # noqa: BLE001 - drain must not break the exit monitor
            _log.exception("[AgentService] durable drain failed")
            return

        for task_id, spawn_args in promoted:
            # Skip a row we already run locally (a restart-recovery row, or a
            # task started out of band) — the store flipped it to running but
            # this replica owns the live subprocess.
            if task_id in self.running_tasks:
                continue
            try:
                await self._spawn_task_execution(
                    task_id=task_id,
                    project_path=Path(spawn_args.project_path),
                    spec_id=spawn_args.spec_id,
                    auto_continue=spawn_args.auto_continue,
                    base_branch=spawn_args.base_branch,
                    mode=spawn_args.mode,
                    force=spawn_args.force,
                    user_id=spawn_args.user_id,
                    stop_after_planning=spawn_args.stop_after_planning,
                    parallel=spawn_args.parallel,
                    workers=spawn_args.workers,
                )
                _log.info(
                    "[AgentService] Dequeued + started %s (durable store)",
                    task_id,
                )
            except Exception:  # noqa: BLE001 - one bad spawn must not stall the queue
                _log.exception(
                    "[AgentService] failed to start queued task %s — "
                    "freeing durable slot",
                    task_id,
                )
                try:
                    await self._store().mark_terminal(
                        task_id, "failed", error="spawn failed on dequeue"
                    )
                except Exception:  # noqa: BLE001
                    _log.exception(
                        "[AgentService] could not free durable slot for %s",
                        task_id,
                    )

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
            "--spec", spec_id,
            "--project-dir", str(project_path),
        ]

        if auto_continue:
            cmd.append("--auto-continue")

            # Check if human review before coding is required
            # If so, don't pass --force to allow the approval gate
            spec_dir = project_path / ".aifactory" / "specs" / spec_id
            requirements_file = spec_dir / "requirements.json"
            task_metadata_file = spec_dir / "task_metadata.json"
            require_review = False

            # Sync metadata from requirements.json to task_metadata.json (Bug fix)
            # Frontend writes to requirements.json, backend reads task_metadata.json
            # Ensure they stay in sync to prevent requireReviewBeforeCoding mismatches
            if requirements_file.exists():
                try:
                    import json
                    requirements = json.loads(requirements_file.read_text())
                    frontend_metadata = requirements.get("metadata", {})

                    # Read existing task_metadata or create new
                    if task_metadata_file.exists():
                        task_metadata = json.loads(task_metadata_file.read_text())
                    else:
                        task_metadata = {}

                    # Sync requireReviewBeforeCoding from frontend to backend
                    if "requireReviewBeforeCoding" in frontend_metadata:
                        task_metadata["requireReviewBeforeCoding"] = frontend_metadata["requireReviewBeforeCoding"]

                    # Save updated task_metadata.json
                    task_metadata_file.write_text(json.dumps(task_metadata, indent=2))

                    require_review = task_metadata.get("requireReviewBeforeCoding", False)
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(f"[AgentService] Could not sync metadata for {task_id}: {e}")
            elif task_metadata_file.exists():
                try:
                    import json
                    task_metadata = json.loads(task_metadata_file.read_text())
                    require_review = task_metadata.get("requireReviewBeforeCoding", False)
                    # Note: Quick Mode no longer forces review - respect requireReviewBeforeCoding setting
                except (json.JSONDecodeError, OSError):
                    pass

            # Write skill context file based on selectedSkills in task_metadata
            self._write_skill_context(spec_dir)

            # Add --force flag if:
            # 1. Review is not required OR
            # 2. Plan was manually approved (force=True from approve_plan endpoint)
            if not require_review or force:
                cmd.append("--force")  # Bypass approval check for headless execution
                if force:
                    logger.info(f"[AgentService] Using --force for {task_id} (plan manually approved)")
            else:
                logger.info(f"[AgentService] Human review before coding enabled for task {task_id} - not using --force")

        if base_branch:
            cmd.extend(["--base-branch", base_branch])

        # Skip QA for quick mode (simple tasks) - coder_quick.md validates inline
        if mode == "quick":
            cmd.append("--skip-qa")
            logger.info(f"[AgentService] Skipping QA for quick mode task {task_id}")

        # Stop after planning for Copilot delegation flow (#94)
        if stop_after_planning:
            cmd.append("--stop-after-planning")
            logger.info(f"[AgentService] Stop-after-planning for {task_id} (Copilot delegation)")

        # Parallel subtask execution (#376): run independent subtasks in
        # dependency-graph waves. Previously these flags were accepted by the
        # API but silently dropped here.
        if _append_parallel_flags(cmd, parallel, workers):
            logger.info(
                f"[AgentService] Parallel execution enabled for {task_id} "
                f"(workers={workers or 'default'})"
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
            logger.info(f"[AgentService] Quick Mode enabled for task {task_id}")

        # Load backend .env file for graphiti and other settings
        backend_env_file = self.backend_path / ".env"
        if backend_env_file.exists():
            try:
                with open(backend_env_file) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            key, value = line.split('=', 1)
                            key = key.strip()
                            value = value.strip()
                            # Don't override existing env vars
                            if key not in env:
                                env[key] = value
                logger.info(f"[AgentService] Loaded backend .env from {backend_env_file}")
            except Exception as e:
                logger.warning(f"[AgentService] Failed to load backend .env: {e}")

        # Load project .aifactory/.env for project-level settings (USE_CLAUDE_MD, etc.)
        project_env_file = project_path / ".aifactory" / ".env"
        if project_env_file.exists():
            try:
                with open(project_env_file) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            key, value = line.split('=', 1)
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
            logger.info(f"[AgentService] Using Claude profile: {profile_name} ({profile_id})")
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
        logger.info(f"[AgentService] [Model: {exec_model_display}] Starting task execution for {task_id}")
        logger.info(f"[AgentService] Command: {' '.join(cmd)}")

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
                from ..routes.projects import load_projects
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
                task_id, _rc_session_name,
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
                cmd, task_id,
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
        worktree_spec_dir = project_path / ".aifactory" / "worktrees" / "tasks" / spec_id / ".aifactory" / "specs" / spec_id
        worktree_spec_dir.mkdir(parents=True, exist_ok=True)
        log_writer = TaskLogWriter(worktree_spec_dir)

        # Also write to main spec dir for immediate visibility
        main_spec_dir = project_path / ".aifactory" / "specs" / spec_id
        main_spec_dir.mkdir(parents=True, exist_ok=True)
        main_log_writer = TaskLogWriter(main_spec_dir)

        # Store log writers for cleanup
        self._task_log_writers[task_id] = (log_writer, main_log_writer)

        # Emit initial progress (100% within planning phase → 20% overall)
        await self._emit_progress(TaskProgress(
            task_id=task_id,
            phase=TaskPhase.PLANNING,
            message="Starting task execution...",
            percentage=100,
        ))

        # Initialize planning phase in logs
        log_writer.set_phase_status(spec_id, TaskPhase.PLANNING, "active")
        main_log_writer.set_phase_status(spec_id, TaskPhase.PLANNING, "active")

        # Start output processing in background with log writers
        asyncio.create_task(self._process_output(
            task_id, proc.stdout, is_stderr=False,
            log_writer=log_writer, spec_id=spec_id
        ))
        asyncio.create_task(self._process_output(task_id, proc.stderr, is_stderr=True))

        # Start process monitor to clean up when finished (with file syncing and failover support)
        asyncio.create_task(self._monitor_process(task_id, proc, project_path, spec_id, cmd, env))

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
            logger.warning(f"[AgentService] rmux create hook raised (ignored); spec_id={spec_id}")

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
                            f"[AgentService] Removed queued task {task_id} "
                            "from durable admission queue"
                        )
                        return True
                except Exception:  # noqa: BLE001 - never break stop on a store hiccup
                    logger.warning(
                        "[AgentService] durable remove_queued failed for %s",
                        task_id, exc_info=True,
                    )
            if any(q.task_id == task_id for q in self._task_queue):
                async with self._admission_lock:
                    self._task_queue = deque(
                        q for q in self._task_queue if q.task_id != task_id
                    )
                logger.info(
                    f"[AgentService] Removed queued task {task_id} from admission queue"
                )
                return True
            # RFC-0016 #671: a build may run as a k8s Job (no in-pod process to
            # signal). Stopping it deletes the Job + frees the durable slot.
            if self._kubejob_backend_enabled():
                if await self._stop_kubejob_build(task_id):
                    return True
            logger.info(f"[AgentService] Task {task_id} not in running_tasks (already stopped or never started)")
            return False

        # Mark as stopped BEFORE termination so _monitor_process defers to us
        self._task_stopped.add(task_id)

        proc = self.running_tasks[task_id]
        # Kill the whole process tree (run.py + coder/git children), not just
        # run.py — otherwise the children orphan and keep running.
        self._signal_process_tree(proc, signal.SIGTERM)

        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
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
            logger.debug(f"[AgentService] Finalized task logs for stopped task {task_id}")

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
            logger.warning(f"[AgentService] rmux reap hook raised in stop_task (ignored); spec_id={_reap_spec_id}")

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
                    task_id, exc_info=True,
                )

        # Emit human_review with errors reason (not just FAILED phase)
        await self._safe_emit_task_status(task_id, "human_review", "errors")
        await self._emit_progress(TaskProgress(
            task_id=task_id,
            phase=TaskPhase.FAILED,
            message="Task stopped by user",
        ))

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
            await self._emit_progress(TaskProgress(
                task_id=task_id,
                phase=TaskPhase.COMPLETED,
                message="Task completed successfully",
            ))
        else:
            await self._emit_progress(TaskProgress(
                task_id=task_id,
                phase=TaskPhase.FAILED,
                message=f"Task failed with exit code {return_code}",
            ))

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
            running, queued,
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
_agent_service: AgentService | None = None


def get_agent_service() -> AgentService:
    """Get the global agent service instance."""
    global _agent_service
    if _agent_service is None:
        _agent_service = AgentService()
    return _agent_service
