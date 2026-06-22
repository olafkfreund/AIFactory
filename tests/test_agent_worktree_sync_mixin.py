"""Worktree file-sync lives in agent_worktree_sync.py::WorktreeSyncMixin (#703).

Contract: AgentService inherits the mixin (method via MRO) + standalone import.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))


def test_worktree_sync_mixin_imports_standalone():
    from server.services import agent_worktree_sync

    assert hasattr(agent_worktree_sync, "WorktreeSyncMixin")


def test_agent_service_inherits_worktree_sync_mixin():
    from server.services.agent_service import AgentService
    from server.services.agent_worktree_sync import WorktreeSyncMixin

    assert issubclass(AgentService, WorktreeSyncMixin)
    assert hasattr(AgentService, "_sync_worktree_files")
