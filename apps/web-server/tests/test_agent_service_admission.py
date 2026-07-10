"""Admission-control + concurrency-queue tests (RFC-0016 #668).

The audited bug: builds were spawned one OS subprocess per task with NO global
concurrency cap (``MAX_CONCURRENT_TASKS`` was dead code), so N concurrent
different tasks oversubscribed the pod. These tests pin the new behaviour:

* under the cap -> start immediately;
* at the cap -> admit to a FIFO queue (status ``queued``), NOT a 409, NOT started;
* a finishing build dequeues + starts the next queued task FIFO;
* cap <= 0 -> unlimited (back-compat);
* the per-task "already running" guard still raises (route -> 409).

``_spawn_task_execution`` is stubbed so no real subprocess is created -- the
stub mimics the real method's side effect (registering ``task_id`` in
``running_tasks``) so the slot accounting under test is exercised faithfully.
"""

import sys
from pathlib import Path
from typing import Any, cast

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from server.services.agent_service import AgentService  # noqa: E402


class _FakeProc:
    """Stand-in for asyncio.subprocess.Process (only identity matters here)."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id


def _make_service(
    cap: int, monkeypatch: pytest.MonkeyPatch
) -> tuple[AgentService, list[str]]:
    """AgentService with a stubbed spawn that just reserves the running slot.

    Returns the service plus the FIFO list of task_ids that were actually
    spawned, so tests can assert on start order without a dynamic attribute.
    """
    service = AgentService()
    service.settings.MAX_CONCURRENT_TASKS = cap

    spawned: list[str] = []

    async def fake_spawn(*, task_id: str, **_kwargs: Any) -> _FakeProc:
        if task_id in service.running_tasks:
            raise ValueError(f"Task {task_id} is already running")
        proc = _FakeProc(task_id)
        # Mirror the real method's side effect: reserve the running slot.
        service.running_tasks[task_id] = cast(Any, proc)
        spawned.append(task_id)
        return proc

    async def fake_mark_queued(*_args: Any, **_kwargs: Any) -> None:
        # Avoid touching disk / websockets in the queued path.
        return None

    monkeypatch.setattr(service, "_spawn_task_execution", fake_spawn)
    monkeypatch.setattr(service, "_mark_task_queued", fake_mark_queued)
    return service, spawned


async def _start(service: AgentService, task_id: str, tmp_path: Path) -> Any:
    return await service.start_task_execution(
        task_id=task_id,
        project_path=tmp_path,
        spec_id=task_id.rsplit(":", 1)[-1],
    )


@pytest.mark.asyncio
async def test_under_cap_starts_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, spawned = _make_service(cap=2, monkeypatch=monkeypatch)

    proc = await _start(service, "p:001", tmp_path)

    assert proc is not None  # started, not queued
    assert service.is_running("p:001")
    assert not service.is_queued("p:001")
    assert spawned == ["p:001"]


@pytest.mark.asyncio
async def test_at_cap_enqueues_not_409_not_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, spawned = _make_service(cap=2, monkeypatch=monkeypatch)

    await _start(service, "p:001", tmp_path)
    await _start(service, "p:002", tmp_path)
    # Third different task is at the cap -> must queue, not raise, not start.
    proc = await _start(service, "p:003", tmp_path)

    assert proc is None  # queued, NOT started
    assert not service.is_running("p:003")
    assert service.is_queued("p:003")
    assert service.get_queued_tasks() == ["p:003"]
    assert "p:003" not in spawned


@pytest.mark.asyncio
async def test_finishing_build_dequeues_next_fifo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, spawned = _make_service(cap=1, monkeypatch=monkeypatch)

    await _start(service, "p:001", tmp_path)  # starts
    await _start(service, "p:002", tmp_path)  # queued
    await _start(service, "p:003", tmp_path)  # queued

    assert service.get_queued_tasks() == ["p:002", "p:003"]

    # Simulate the running build finishing: the monitor removes it from
    # running_tasks, then _drain_queue pulls the next FIFO task.
    service.running_tasks.pop("p:001", None)
    await service._drain_queue()

    assert service.is_running("p:002")  # next FIFO promoted
    assert not service.is_queued("p:002")
    assert service.get_queued_tasks() == ["p:003"]  # third still waits (cap=1)

    # Finish p:002 -> p:003 promoted.
    service.running_tasks.pop("p:002", None)
    await service._drain_queue()
    assert service.is_running("p:003")
    assert service.get_queued_tasks() == []
    assert spawned == ["p:001", "p:002", "p:003"]


@pytest.mark.asyncio
async def test_cap_zero_is_unlimited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _spawned = _make_service(cap=0, monkeypatch=monkeypatch)

    procs = [await _start(service, f"p:{i:03d}", tmp_path) for i in range(10)]

    assert all(p is not None for p in procs)  # all started
    assert service.get_queued_tasks() == []
    assert len(service.running_tasks) == 10


@pytest.mark.asyncio
async def test_cap_negative_is_unlimited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _spawned = _make_service(cap=-1, monkeypatch=monkeypatch)

    procs = [await _start(service, f"p:{i:03d}", tmp_path) for i in range(5)]

    assert all(p is not None for p in procs)
    assert service.get_queued_tasks() == []


@pytest.mark.asyncio
async def test_same_task_already_running_still_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _spawned = _make_service(cap=5, monkeypatch=monkeypatch)

    await _start(service, "p:001", tmp_path)
    # Re-starting the SAME running task must 409 (ValueError), regardless of cap.
    with pytest.raises(ValueError, match="already running"):
        await _start(service, "p:001", tmp_path)


@pytest.mark.asyncio
async def test_same_task_already_queued_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _spawned = _make_service(cap=1, monkeypatch=monkeypatch)

    await _start(service, "p:001", tmp_path)  # running
    await _start(service, "p:002", tmp_path)  # queued
    # Re-admitting an already-queued task must not double-enqueue.
    with pytest.raises(ValueError, match="already queued"):
        await _start(service, "p:002", tmp_path)
    assert service.get_queued_tasks() == ["p:002"]


@pytest.mark.asyncio
async def test_stop_removes_queued_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _spawned = _make_service(cap=1, monkeypatch=monkeypatch)

    await _start(service, "p:001", tmp_path)  # running
    await _start(service, "p:002", tmp_path)  # queued

    stopped = await service.stop_task("p:002")

    assert stopped is True
    assert not service.is_queued("p:002")
    assert service.get_queued_tasks() == []
