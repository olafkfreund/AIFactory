"""Agent task runtime models — from services/agent_service.py (god-file split).

Small dataclasses (TaskProgress, QueuedTask, TaskLog) describing in-flight task
state, lifted out of agent_service.py. agent_service.py re-exports them so
existing callers are unchanged. Imports only stdlib + TaskPhase
(services/task_phase) -> no circular import with agent_service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .task_phase import TaskPhase


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
