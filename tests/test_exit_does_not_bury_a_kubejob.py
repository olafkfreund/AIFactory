"""#1628 — a subprocess exit must not mark a k8s-Job-owned task terminal.

On the `/start` path a task with no plan runs **spec creation** as an in-pod
subprocess, and the build that follows runs as a Kubernetes Job on the same
job-state row. `_free_durable_slot_on_exit` fires when that subprocess exits
and marked the whole task terminal, so the row ended up carrying a live Job
reference *and* `done`.

`get_active_kubejobs` selects `lifecycle_state == "running"`, so the build was
invisible to the reconcile loop, the reaper, the #1249 review re-drive, streamer
cancellation and credential release — measured live as `Job active=1` with the
row `done`, unchanged ten minutes later.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1] / "apps" / "web-server"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from server.services.agent_service import AgentService  # noqa: E402

TASK = "proj:023-some-spec"


class _Store:
    def __init__(self, ref: dict[str, Any] | None) -> None:
        self._ref = ref
        self.terminal_calls: list[tuple[str, str]] = []

    async def get_state(self, _job_id: str) -> dict[str, Any]:
        return {"lifecycle_state": "running", "worker_ref": self._ref or {}}

    async def mark_terminal(self, job_id: str, lifecycle: str, **_kw: Any) -> None:
        self.terminal_calls.append((job_id, lifecycle))


def _service(monkeypatch: pytest.MonkeyPatch, store: _Store) -> AgentService:
    service = AgentService()
    service._store_enabled = True
    monkeypatch.setattr(service, "_store", lambda: store)
    return service


@pytest.mark.asyncio
async def test_k8s_job_row_is_not_buried_by_a_subprocess_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """THE regression: spec creation's exit must not end the build's task."""
    store = _Store({"kind": "k8s-job", "namespace": "factory", "job_name": "j"})
    service = _service(monkeypatch, store)

    await service._free_durable_slot_on_exit(TASK, spec_dir=tmp_path)

    assert store.terminal_calls == [], "a live Job owns this task's lifecycle"


@pytest.mark.asyncio
async def test_subprocess_row_still_goes_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The guard must not stop recording terminal states for real subprocesses.

    Leaving a dead build `running` strands its slot and makes /start refuse with
    "already running" — the failure the intent calls as bad as the one fixed.
    """
    store = _Store({"kind": "subprocess"})
    service = _service(monkeypatch, store)

    await service._free_durable_slot_on_exit(TASK, spec_dir=tmp_path)

    assert [c[0] for c in store.terminal_calls] == [TASK]


@pytest.mark.asyncio
async def test_row_without_a_ref_still_goes_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """No ref at all is the ordinary in-memory/legacy case — unchanged."""
    store = _Store(None)
    service = _service(monkeypatch, store)

    await service._free_durable_slot_on_exit(TASK, spec_dir=tmp_path)

    assert [c[0] for c in store.terminal_calls] == [TASK]
