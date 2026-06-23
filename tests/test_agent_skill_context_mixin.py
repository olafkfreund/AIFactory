"""Skill-context writing lives in agent_skill_context.py::SkillContextMixin (#703).

Contract: AgentService inherits the mixin (method via MRO) + standalone import.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))


def test_skill_context_mixin_imports_standalone():
    from server.services import agent_skill_context

    assert hasattr(agent_skill_context, "SkillContextMixin")


def test_agent_service_inherits_skill_context_mixin():
    from server.services.agent_service import AgentService
    from server.services.agent_skill_context import SkillContextMixin

    assert issubclass(AgentService, SkillContextMixin)
    assert hasattr(AgentService, "_write_skill_context")
