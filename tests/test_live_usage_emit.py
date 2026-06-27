"""PR 2 (running cost): the worktree-sync tick emits a THROTTLED live usage
snapshot so the cockpit shows accruing cost mid-build, without flooding the
completion webhook."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

from server.services import completion  # noqa: E402
from server.services.agent_service import AgentService  # noqa: E402


@pytest.mark.asyncio
async def test_sync_emits_throttled_live_usage(tmp_path, monkeypatch):
    calls: list[str] = []
    # Stub the emit so we count calls + capture the (non-terminal) status, no network.
    monkeypatch.setattr(
        completion,
        "emit_usage_snapshot",
        lambda *a, **k: (calls.append(k.get("status")), {"ok": 1})[1],
    )
    clock = {"v": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["v"])

    svc = AgentService()
    spec = "001-x"
    (tmp_path / ".aifactory" / "specs" / spec).mkdir(parents=True)

    # Two ticks within the throttle window → exactly one emit.
    await svc._sync_worktree_files(tmp_path, spec, task_id="p:001-x")
    await svc._sync_worktree_files(tmp_path, spec, task_id="p:001-x")
    assert calls == ["running"]

    # Advance past the window → emit again.
    clock["v"] += 20.0
    await svc._sync_worktree_files(tmp_path, spec, task_id="p:001-x")
    assert calls == ["running", "running"]
