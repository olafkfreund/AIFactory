"""#1619 — ``is_running`` must answer for kubejob builds, not only subprocesses.

``AgentService.is_running`` was ``task_id in self.running_tasks``, and the only
write to that dict happens right after ``asyncio.create_subprocess_exec`` — the
in-pod subprocess backend. A kubejob build dispatches a k8s Job instead, so the
answer was ``False`` for its whole run. The cockpit renders that as **Stuck**
and offers **Recover**, which resets the task's record while the Job keeps
writing. Measured live: a Job ``Running`` for 49 minutes, with its pod log
advancing, while ``GET /api/tasks/<id>/running`` answered ``is_running: false``.

The live set is rebuilt wholesale on every reconcile tick. These tests pin both
directions: a running build is reported running, and a build that left the rows
stops being reported running on the very next tick.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))
_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from server.services.agent_service import AgentService  # noqa: E402

TASK = "proj-uuid:022-some-spec"
OTHER = "proj-uuid:023-other-spec"


class _StillRunningBackend:
    """Every polled job is still running (``reconcile_by_poll`` -> None)."""

    async def reconcile_by_poll(self, _job_id: str) -> str | None:
        return None


class _TerminalBackend:
    """Every polled job has finished."""

    async def reconcile_by_poll(self, _job_id: str) -> str | None:
        return "completed"


def _service(
    monkeypatch: pytest.MonkeyPatch, rows: list[str], backend: Any
) -> AgentService:
    monkeypatch.setenv("AIFACTORY_BUILD_BACKEND", "kubejob")
    service = AgentService()
    service._store_enabled = True

    class _FakeStore:
        async def get_active_kubejobs(self) -> list[dict[str, Any]]:
            return [
                {"job_id": r, "job_name": "j", "namespace": "factory"} for r in rows
            ]

    monkeypatch.setattr(service, "_store", lambda: _FakeStore())
    monkeypatch.setattr(service, "_build_backend", lambda: backend)
    monkeypatch.setattr(service, "_redrive_kubejob_review", _noop)
    monkeypatch.setattr(service, "_drain_queue", _noop)
    monkeypatch.setattr(service, "_cancel_kubejob_log_stream", lambda *_a, **_k: None)
    monkeypatch.setattr(service, "_reap_kubejob_console", _noop)
    monkeypatch.setattr(service, "_release_task_credential", lambda *_a, **_k: None)
    return service


async def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


@pytest.mark.asyncio
async def test_running_kubejob_reads_as_running(monkeypatch: pytest.MonkeyPatch):
    """THE regression: a dispatched Job is running, and nothing else knows it.

    Mutation guard: drop ``_active_kubejob_task_ids`` from ``is_running`` and
    this fails — which is precisely the production behaviour #1619 reports.
    """
    service = _service(monkeypatch, [TASK], _StillRunningBackend())
    assert service.is_running(TASK) is False, "nothing polled yet"

    await service.reconcile_kubejob_builds()

    assert service.is_running(TASK) is True
    assert service.running_tasks == {}, "no subprocess was involved"


@pytest.mark.asyncio
async def test_finished_kubejob_stops_reading_as_running(
    monkeypatch: pytest.MonkeyPatch,
):
    """The set is replaced, not accumulated.

    A set that is only added to would keep claiming a finished build is alive,
    which strands it and makes ``/start`` refuse with "already running" — the
    failure the intent calls worse than the one being fixed.
    """
    service = _service(monkeypatch, [TASK], _StillRunningBackend())
    await service.reconcile_kubejob_builds()
    assert service.is_running(TASK) is True

    # Next tick: the row is gone from the store entirely.
    class _EmptyStore:
        async def get_active_kubejobs(self) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr(service, "_store", lambda: _EmptyStore())
    await service.reconcile_kubejob_builds()

    assert service.is_running(TASK) is False


@pytest.mark.asyncio
async def test_terminal_job_is_not_reported_running(monkeypatch: pytest.MonkeyPatch):
    """A row still present but polled terminal must not join the live set."""
    service = _service(monkeypatch, [TASK], _TerminalBackend())
    await service.reconcile_kubejob_builds()
    assert service.is_running(TASK) is False


@pytest.mark.asyncio
async def test_unrelated_task_is_not_reported_running(
    monkeypatch: pytest.MonkeyPatch,
):
    """Only the polled ids are live — the answer is not a blanket True."""
    service = _service(monkeypatch, [TASK], _StillRunningBackend())
    await service.reconcile_kubejob_builds()
    assert service.is_running(OTHER) is False


@pytest.mark.asyncio
async def test_store_failure_leaves_the_previous_answer_standing(
    monkeypatch: pytest.MonkeyPatch,
):
    """A store hiccup must not report every live build dead.

    The set is only replaced when the poll succeeded; an early return keeps the
    last known answer rather than inventing "nothing is running", which would
    make the cockpit flap between running and Stuck on a transient DB error.
    """
    service = _service(monkeypatch, [TASK], _StillRunningBackend())
    await service.reconcile_kubejob_builds()
    assert service.is_running(TASK) is True

    class _BrokenStore:
        async def get_active_kubejobs(self) -> list[dict[str, Any]]:
            raise RuntimeError("connection reset")

    monkeypatch.setattr(service, "_store", lambda: _BrokenStore())
    await service.reconcile_kubejob_builds()

    assert service.is_running(TASK) is True


@pytest.mark.asyncio
async def test_streamer_liveness_check_follows_the_same_set(
    monkeypatch: pytest.MonkeyPatch,
):
    """#1619: the log streamer's reattach authority is the same live set.

    If these two could disagree, the cockpit would say "running" while the
    streamer gave up (or the reverse), which is how the original defect stayed
    invisible — two views of the same build, neither checked against the other.
    """
    service = _service(monkeypatch, [TASK], _StillRunningBackend())
    await service.reconcile_kubejob_builds()

    active = service._kubejob_still_active(TASK)
    assert await active() is True
    assert service.is_running(TASK) is True

    # A build that has left the rows is no longer followed...
    gone = service._kubejob_still_active(OTHER)
    import server.services.agent_kubejob as kj

    monkeypatch.setattr(kj, "_DISPATCH_GRACE_SECONDS", 0.0)
    assert await gone() is False
    assert service.is_running(OTHER) is False


@pytest.mark.asyncio
async def test_just_dispatched_build_counts_as_active(
    monkeypatch: pytest.MonkeyPatch,
):
    """The set is empty until the first tick; a fresh dispatch is not "dead".

    The streamer's first EOF arrives inside that window — it is exactly the
    container-still-initialising case — so treating an unknown id as dead would
    reproduce #1619 rather than fix it.
    """
    service = _service(monkeypatch, [], _StillRunningBackend())
    active = service._kubejob_still_active("proj-uuid:999-never-polled")
    assert await active() is True, "within the dispatch grace window"
