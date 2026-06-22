"""Credential/token/profile/retry lives in agent_credential.py::CredentialMixin (#703).

Contract: AgentService inherits the mixin (methods bound via MRO) + standalone import.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

_METHODS = (
    "_resolve_claude_token",
    "_resolve_claude_token_pooled",
    "_release_task_credential",
    "_update_active_profile",
    "_retry_task_with_profile",
)


def test_credential_mixin_imports_standalone():
    from server.services import agent_credential

    assert hasattr(agent_credential, "CredentialMixin")


def test_agent_service_inherits_credential_mixin():
    from server.services.agent_credential import CredentialMixin
    from server.services.agent_service import AgentService

    assert issubclass(AgentService, CredentialMixin)
    for name in _METHODS:
        assert hasattr(AgentService, name), f"AgentService lost {name}"
