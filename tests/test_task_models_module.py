"""Core task models live in routes/task_models.py — #556 (god-file split).

Locks the contract that lets sub-routers import the models without a circular
import:
  1. task_models imports standalone (no routes/tasks.py load needed).
  2. routes/tasks.py re-exports the SAME objects, so existing
     ``from ..routes.tasks import Task`` callers are unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

_MODEL_NAMES = (
    "Task",
    "TaskBase",
    "TaskList",
    "TaskCreate",
    "TaskUpdate",
    "TaskMetadata",
    "TaskMetadataUpdate",
    "SelectedSkill",
    "Subtask",
    "SubtaskVerification",
    "TaskStatus",
    "ClarificationQuestion",
    "ClarificationResponse",
    "ClarificationAnswer",
    "ClarificationAnswersRequest",
)


def test_task_models_module_is_self_contained():
    # Importing the models must NOT require importing routes.tasks.
    from server.routes import task_models

    for name in _MODEL_NAMES:
        assert hasattr(task_models, name), f"task_models missing {name}"


def test_routes_tasks_reexports_same_objects():
    from server.routes import task_models, tasks

    for name in _MODEL_NAMES:
        assert getattr(tasks, name) is getattr(task_models, name), (
            f"routes.tasks.{name} is not the same object as task_models.{name}"
        )
