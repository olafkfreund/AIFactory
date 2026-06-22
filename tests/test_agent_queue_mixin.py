"""Task queue draining + admission lives in agent_queue.py::QueueMixin (#703).

Contract: AgentService inherits the mixin (methods via MRO) + standalone import.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

_METHODS = ("_mark_task_queued", "_drain_queue", "_drain_queue_durable")


def test_queue_mixin_imports_standalone():
    from server.services import agent_queue

    assert hasattr(agent_queue, "QueueMixin")


def test_agent_service_inherits_queue_mixin():
    from server.services.agent_queue import QueueMixin
    from server.services.agent_service import AgentService

    assert issubclass(AgentService, QueueMixin)
    for name in _METHODS:
        assert hasattr(AgentService, name), f"AgentService lost {name}"
