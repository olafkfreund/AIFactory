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


def test_kubejob_stops_the_in_pod_build_chain():
    """#1538: spec_runner chains into the build via os.execv unless --no-build.

    Under the kubejob backend that chain is a SECOND execution: the build runs
    in-pod on the data-PVC worktree while the dispatched Job re-runs it from a
    main-based clone, finds the build already complete, gates a tree holding
    none of the work, and pushes that tree's HEAD — so the task branch lands at
    main and the real commits stay stranded on the PVC. Four tasks in a row
    shipped that way.
    """
    from server.services.agent_spec_creation import build_chain_args

    assert build_chain_args(True) == ["--no-build"]
    # In-pod backend still chains: that path has no Job to do the build.
    assert build_chain_args(False) == []


def test_spec_creation_uses_the_chain_helper_not_a_literal():
    """The call site must go through the helper, or the test above proves nothing
    about what actually gets spawned."""
    import inspect

    from server.services import agent_spec_creation

    src = inspect.getsource(agent_spec_creation.SpecCreationMixin)
    assert "build_chain_args(self._kubejob_backend_enabled())" in src
    # And the result must reach the argv.
    assert "cmd.extend(chain_args)" in src
