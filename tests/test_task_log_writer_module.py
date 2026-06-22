"""TaskLogWriter lives in services/task_log_writer.py (agent_service decomposition).

Contract: it imports standalone (no agent_service load) and agent_service
re-exports the SAME object so existing callers are unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))


def test_task_log_writer_is_self_contained():
    from server.services import task_log_writer

    assert hasattr(task_log_writer, "TaskLogWriter")


def test_agent_service_reexports_task_log_writer():
    from server.services import agent_service, task_log_writer

    assert agent_service.TaskLogWriter is task_log_writer.TaskLogWriter
