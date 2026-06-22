"""Agent task runtime models live in services/agent_task_models.py.

Contract: standalone import (no agent_service load) + agent_service re-exports
the SAME objects so existing callers are unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

_NAMES = ("TaskProgress", "QueuedTask", "TaskLog")


def test_agent_task_models_self_contained():
    from server.services import agent_task_models

    for name in _NAMES:
        assert hasattr(agent_task_models, name)


def test_agent_service_reexports_same_objects():
    from server.services import agent_service, agent_task_models

    for name in _NAMES:
        assert getattr(agent_service, name) is getattr(agent_task_models, name)
