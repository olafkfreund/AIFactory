"""#2144: status, running list and logs for a kubejob-backed build.

The MCP proxy delegates to the REST routes the portal uses, so the routes are
exercised directly with a kubejob-backed fixture: the task is in
``_active_kubejob_task_ids`` (no in-pod subprocess), its durable row carries a
``k8s-job`` ref, and the Job pod log is a fake.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO = Path(__file__).resolve().parents[1]
for _p in (_REPO / "apps" / "web-server", _REPO / "apps" / "backend"):
    if str(_p) not in sys.path:
        sys.path.append(str(_p))

from server.routes import execution, tasks_logs  # noqa: E402
from server.services import build_log_stream  # noqa: E402
from server.services.agent_service import AgentService  # noqa: E402

TASK = "proj:001-demo"
REF = {"kind": "k8s-job", "namespace": "factory", "job_name": "factory-aifactory-demo"}


class _Store:
    def __init__(self, lifecycle: str = "running", ref: dict | None = None) -> None:
        self._state = {"lifecycle_state": lifecycle, "worker_ref": ref or REF}

    async def get_state(self, task_id: str) -> dict | None:
        return self._state if task_id == TASK else None


def _service(lifecycle: str = "running", queued: bool = False) -> SimpleNamespace:
    svc = SimpleNamespace(
        _store_enabled=True,
        _active_kubejob_task_ids={TASK},
        running_tasks={},
        _store=lambda: _Store(lifecycle),
        is_queued=lambda _t: queued,
        _redact_nonce=lambda _t, line: line.replace("NONCE", "***"),
    )

    svc.get_running_task_ids = lambda: AgentService.get_running_task_ids(svc)

    async def is_running_anywhere(_t: str) -> bool:
        return lifecycle == "running"

    svc.is_running_anywhere = is_running_anywhere
    return svc


def _progress(monkeypatch: pytest.MonkeyPatch, progress: dict | None) -> None:
    async def get_task(_task_id: str, _access: object = None) -> dict:
        return {"executionProgress": progress} if progress else {}

    monkeypatch.setattr("server.routes.tasks.get_task", get_task)


def test_running_ids_include_kubejob_builds() -> None:
    svc = AgentService.__new__(AgentService)
    svc.running_tasks = {"p:inpod": object()}
    svc._active_kubejob_task_ids = {TASK}
    assert svc.get_running_task_ids() == sorted(["p:inpod", TASK])


async def test_running_route_lists_kubejob_task(monkeypatch) -> None:
    monkeypatch.setattr(execution, "get_agent_service", lambda: _service())
    res = await execution.get_running_tasks()
    assert res.tasks == [TASK]
    assert res.count == 1


async def test_status_reports_phase_and_progress(monkeypatch) -> None:
    monkeypatch.setattr(execution, "get_agent_service", lambda: _service())
    _progress(
        monkeypatch,
        {
            "phase": "coding",
            "overallProgress": 40,
            "currentSubtask": "Add pagination",
            "message": "2/5 subtasks completed",
        },
    )
    res = await execution.get_task_status(TASK, _access={})
    assert res.is_running is True
    assert res.phase == "coding"
    assert res.overall_progress == 40
    assert res.current_subtask == "Add pagination"
    assert res.message == "2/5 subtasks completed"
    assert res.queued is False


async def test_status_running_without_progress_is_starting(monkeypatch) -> None:
    monkeypatch.setattr(execution, "get_agent_service", lambda: _service())
    _progress(monkeypatch, None)
    res = await execution.get_task_status(TASK, _access={})
    assert res.phase == "starting"


async def test_status_queued_wins_over_stale_progress(monkeypatch) -> None:
    svc = _service(lifecycle="queued")
    monkeypatch.setattr(execution, "get_agent_service", lambda: svc)
    _progress(monkeypatch, {"phase": "complete", "overallProgress": 100})
    res = await execution.get_task_status(TASK, _access={})
    assert res.queued is True
    assert res.phase == "queued"
    assert res.overall_progress is None


async def test_status_in_memory_queue(monkeypatch) -> None:
    svc = _service(lifecycle="none", queued=True)
    monkeypatch.setattr(execution, "get_agent_service", lambda: svc)
    _progress(monkeypatch, None)
    assert (await execution.get_task_status(TASK, _access={})).phase == "queued"


async def test_job_log_tail_redacts_and_tails(monkeypatch) -> None:
    calls: list = []

    async def fake_tail(ns: str, job: str, n: int, **_: object) -> list[str]:
        calls.append((ns, job, n))
        return ["planning started", "token NONCE leaked"]

    monkeypatch.setattr(build_log_stream, "tail_job_logs", fake_tail)
    monkeypatch.setattr(
        "server.services.agent_service.get_agent_service", lambda: _service()
    )
    entries = await tasks_logs._job_log_entries(TASK, 50)
    assert calls == [("factory", "factory-aifactory-demo", 50)]
    assert [e["content"] for e in entries] == ["planning started", "token *** leaked"]
    assert entries[0]["source"] == "job"


async def test_job_log_tail_skips_non_kubejob(monkeypatch) -> None:
    svc = _service()
    svc._store = lambda: _Store(ref={"kind": "subprocess"})
    monkeypatch.setattr("server.services.agent_service.get_agent_service", lambda: svc)
    assert await tasks_logs._job_log_entries(TASK, None) == []


async def test_logs_route_falls_back_to_job_pod(monkeypatch, tmp_path: Path) -> None:
    """Dispatch leaves an empty task_logs.json; the pod tail fills ``logs``."""
    spec = tmp_path / ".aifactory" / "specs" / "001-demo"
    spec.mkdir(parents=True)
    (spec / "task_logs.json").write_text(
        json.dumps({"spec_id": "001-demo", "phases": {}})
    )
    monkeypatch.setattr(
        tasks_logs, "load_projects", lambda: {"proj": {"path": str(tmp_path)}}
    )

    async def fake_tail(*_a: object, **_k: object) -> list[str]:
        return ["building"]

    monkeypatch.setattr(build_log_stream, "tail_job_logs", fake_tail)
    monkeypatch.setattr(
        "server.services.agent_service.get_agent_service", lambda: _service()
    )
    res = await tasks_logs.get_task_logs(TASK, _access={}, tail=10)
    assert res["total"] == 1
    assert res["logs"][0]["content"] == "building"
