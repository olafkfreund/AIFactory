"""Task helpers live in routes/task_service.py — #556 (god-file split).

Locks the contract: the helpers import standalone (no routes/tasks.py load), and
routes/tasks.py re-exports the SAME objects so existing
``from ..routes.tasks import spec_to_task`` callers are unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

_HELPER_NAMES = (
    "get_spec_dirs",
    "get_next_spec_id",
    "get_worktree_spec_dir",
    "sync_worktree_to_main_spec",
    "validate_done_status",
    "get_plan_with_worktree_sync",
    "load_spec_metadata",
    "spec_to_task",
    "map_backend_status_to_frontend",
    "get_execution_progress",
    "task_to_dict",
)


def test_task_service_is_self_contained():
    # Importing the helpers must NOT require importing routes.tasks.
    from server.routes import task_service

    for name in _HELPER_NAMES:
        assert hasattr(task_service, name), f"task_service missing {name}"


def test_routes_tasks_reexports_same_helpers():
    from server.routes import task_service, tasks

    for name in _HELPER_NAMES:
        assert getattr(tasks, name) is getattr(task_service, name), (
            f"routes.tasks.{name} is not the same object as task_service.{name}"
        )
