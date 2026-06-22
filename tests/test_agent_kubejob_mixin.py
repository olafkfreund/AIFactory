"""Kubejob backend lives in services/agent_kubejob.py::KubejobMixin (#703 spike).

Contract: AgentService inherits the mixin (so the kubejob methods are bound on
the instance via the MRO), and the mixin module imports standalone.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

_KUBEJOB_METHODS = (
    "_kubejob_backend_enabled",
    "_dispatch_build_job",
    "reconcile_kubejob_builds",
    "reap_kubejob_builds",
    "_stop_kubejob_build",
)


def test_kubejob_mixin_imports_standalone():
    from server.services import agent_kubejob

    assert hasattr(agent_kubejob, "KubejobMixin")


def test_agent_service_inherits_kubejob_mixin():
    from server.services.agent_kubejob import KubejobMixin
    from server.services.agent_service import AgentService

    assert issubclass(AgentService, KubejobMixin)
    for name in _KUBEJOB_METHODS:
        assert hasattr(AgentService, name), f"AgentService lost {name}"
