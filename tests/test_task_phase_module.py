"""Task-phase helpers live in services/task_phase.py (agent_service decomposition).

Contract: the phase model/helpers import standalone (no agent_service load), and
agent_service re-exports the SAME objects so existing callers are unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

_NAMES = (
    "TaskPhase",
    "phase_to_status",
    "phase_to_review_reason",
    "is_failed_build",
    "scale_progress",
    "PHASE_RANGES",
)


def test_task_phase_is_self_contained():
    from server.services import task_phase

    for name in _NAMES:
        assert hasattr(task_phase, name), f"task_phase missing {name}"
    # Sanity: the enum + a mapping still behave.
    assert task_phase.phase_to_status(task_phase.TaskPhase.COMPLETED)


def test_agent_service_reexports_same_objects():
    from server.services import agent_service, task_phase

    for name in _NAMES:
        assert getattr(agent_service, name) is getattr(task_phase, name), (
            f"agent_service.{name} is not the same object as task_phase.{name}"
        )
