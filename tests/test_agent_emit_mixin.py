"""Event emission + output processing lives in agent_emit.py::EmitMixin (#703).

Contract: AgentService inherits the mixin (methods via MRO), _dedup_signature is
re-exported from agent_service, and the module imports standalone.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

_METHODS = (
    "_emit_log",
    "_safe_emit_task_update",
    "_safe_emit_task_status",
    "_emit_progress",
    "_process_output",
)


def test_emit_mixin_imports_standalone():
    from server.services import agent_emit

    assert hasattr(agent_emit, "EmitMixin")
    assert hasattr(agent_emit, "_dedup_signature")


def test_agent_service_inherits_emit_mixin_and_reexports_dedup():
    from server.services import agent_emit
    from server.services.agent_service import AgentService, _dedup_signature

    assert issubclass(AgentService, agent_emit.EmitMixin)
    assert _dedup_signature is agent_emit._dedup_signature
    for name in _METHODS:
        assert hasattr(AgentService, name), f"AgentService lost {name}"
