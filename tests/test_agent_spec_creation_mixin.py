"""Spec-creation entry point lives in agent_spec_creation.py::SpecCreationMixin (#703).

Contract: AgentService inherits the mixin (method via MRO) + standalone import.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))


def test_spec_creation_mixin_imports_standalone():
    from server.services import agent_spec_creation

    assert hasattr(agent_spec_creation, "SpecCreationMixin")


def test_agent_service_inherits_spec_creation_mixin():
    from server.services.agent_service import AgentService
    from server.services.agent_spec_creation import SpecCreationMixin

    assert issubclass(AgentService, SpecCreationMixin)
    assert hasattr(AgentService, "start_spec_creation")
