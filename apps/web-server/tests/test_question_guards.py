"""Question pauses protect the existing task mutation routes."""

import json
from collections.abc import AsyncIterator
from contextlib import nullcontext
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient, Response
from review.state import ReviewState
from server.routes import (
    execution,
    plan_approval,
    project_authz,
    projects,
    task_service,
    tasks,
)
from server.services import (
    agent_kubejob,
    agent_service,
    audit_service,
    copilot_dispatch_service,
    task_control,
)
from server.services.agent_task_models import QueuedTask
from server.services.build_backend import GatedTaskRefusedError
from server.services.job_state_store import SpawnArgs
from server.services.question_guards import APPROVAL_PENDING, approval_blocked

TASK = "project:042-export-csv"
URL = f"/api/tasks/{TASK}"
CONTRACT = Path(__file__).resolve().parents[3] / "tests/fixtures/questions_api.json"


@pytest.fixture
def spec_dir(tmp_path: Path) -> Path:
    spec = tmp_path / ".aifactory" / "specs" / TASK.split(":")[1]
    (spec / "questions").mkdir(parents=True)
    (spec / "spec.md").write_text("# Export CSV", encoding="utf-8")
    (spec / "implementation_plan.json").write_text(
        '{"status":"human_review","phases":[]}', encoding="utf-8"
    )
    return spec


@pytest.fixture
def build_start(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    start = AsyncMock(return_value=SimpleNamespace(pid=1))
    service = SimpleNamespace(
        is_running_anywhere=AsyncMock(return_value=False),
        start_task_execution=start,
    )
    monkeypatch.setattr(execution, "get_agent_service", lambda: service)
    monkeypatch.setattr(execution, "emit_task_status", AsyncMock())
    return start


@pytest.fixture
async def client(
    spec_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    registry = {"project": {"path": str(spec_dir.parents[2])}}
    for module in (execution, tasks, task_service, projects, plan_approval):
        monkeypatch.setattr(module, "load_projects", lambda: registry)
    monkeypatch.setattr(audit_service, "audit_task_action", AsyncMock())
    monkeypatch.setattr(execution, "audit_task_action", AsyncMock())
    monkeypatch.setattr(plan_approval, "audit_task_action", AsyncMock())
    monkeypatch.setattr("server.websockets.events.emit_task_status", AsyncMock())
    monkeypatch.setattr(execution, "correction_kubejob_enabled", lambda: False)
    monkeypatch.setattr(execution, "inpod_agent_refusal", lambda: None)
    monkeypatch.setattr(
        execution,
        "get_settings",
        lambda: SimpleNamespace(REQUIRE_CORRECTION_TRIAGE=False),
    )
    app = FastAPI()
    app.include_router(execution.router, prefix="/api/tasks")
    app.include_router(tasks.router, prefix="/api/tasks")
    app.include_router(projects.router, prefix="/api/projects")
    for route in app.routes:
        if isinstance(route, APIRoute):
            for dep in route.dependant.dependencies:
                if isinstance(dep.call, project_authz.ProjectAccessChecker):
                    app.dependency_overrides[dep.call] = lambda: {}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as test_client:
        yield test_client


def _question(
    spec_dir: Path, status: str, *, approval: bool = True, number: int = 1
) -> Path:
    qid = f"q-{number:012x}"
    path = spec_dir / "questions" / f"{qid}.json"
    path.write_text(
        json.dumps({"id": qid, "status": status, "needs_approval": approval}),
        encoding="utf-8",
    )
    return path


def _assert_approval_pending(response: Response) -> None:
    error = json.loads(CONTRACT.read_text(encoding="utf-8"))["errors"][
        "approval_pending"
    ]
    assert response.status_code == error["status"]
    assert response.json() == error["body"]


@pytest.mark.parametrize("route", ["start", "recover"])
@pytest.mark.parametrize("question_status", ["open", "expired", "rejected", "corrupt"])
async def test_build_routes_hold_pending_approval(
    client: AsyncClient, spec_dir: Path, route: str, question_status: str
) -> None:
    question = _question(spec_dir, question_status)
    if question_status == "corrupt":
        question.write_text("{", encoding="utf-8")
    before = {file: file.read_bytes() for file in spec_dir.rglob("*") if file.is_file()}

    _assert_approval_pending(await client.post(f"{URL}/{route}", json={}))

    assert {
        file: file.read_bytes() for file in spec_dir.rglob("*") if file.is_file()
    } == before


@pytest.mark.parametrize("auto_restart", [True, False])
@pytest.mark.parametrize("question_status", ["open", "expired", "rejected"])
async def test_approve_plan_holds_pending_approval(
    client: AsyncClient,
    spec_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    auto_restart: bool,
    question_status: str,
) -> None:
    _question(spec_dir, question_status)
    task_control.write_control(
        spec_dir, status="human_review", review_reason="plan_review"
    )
    stop = AsyncMock()
    start = AsyncMock()
    service = SimpleNamespace(
        stop_stale_spec_creation=stop,
        is_running_anywhere=AsyncMock(return_value=False),
        start_task_execution=start,
    )
    monkeypatch.setattr(agent_service, "get_agent_service", lambda: service)
    before = {file: file.read_bytes() for file in spec_dir.rglob("*") if file.is_file()}
    body = {"auto_restart": auto_restart}

    _assert_approval_pending(await client.post(f"{URL}/approve-plan", json=body))
    # The stdio proxy also calls the handler directly.
    with pytest.raises(HTTPException) as refused:
        await plan_approval.approve_plan(
            TASK, plan_approval.ApprovePlanRequest(**body), {}
        )
    assert refused.value.status_code == 409
    assert refused.value.detail == APPROVAL_PENDING

    assert "planApproved" not in task_control.read_control(spec_dir)
    assert not ReviewState.load(spec_dir).approved
    stop.assert_not_awaited()
    start.assert_not_awaited()
    assert {
        file: file.read_bytes() for file in spec_dir.rglob("*") if file.is_file()
    } == before


@pytest.mark.parametrize("reason", ["needs_input", "injection_scan"])
async def test_approve_plan_refuses_other_review_reasons(
    client: AsyncClient, spec_dir: Path, reason: str
) -> None:
    task_control.write_control(spec_dir, status="human_review", review_reason=reason)
    response = await client.post(f"{URL}/approve-plan", json={"auto_restart": False})
    assert response.status_code == 409
    assert response.json() == {"detail": "task is not awaiting plan review"}
    assert "planApproved" not in task_control.read_control(spec_dir)
    assert not ReviewState.load(spec_dir).approved


async def test_approve_plan_rechecks_after_restart_preflight(
    client: AsyncClient, spec_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def publish_question(_task_id: str) -> bool:
        _question(spec_dir, "open")
        return False

    start = AsyncMock()
    service = SimpleNamespace(
        stop_stale_spec_creation=AsyncMock(),
        is_running_anywhere=publish_question,
        start_task_execution=start,
    )
    monkeypatch.setattr(agent_service, "get_agent_service", lambda: service)

    _assert_approval_pending(await client.post(f"{URL}/approve-plan", json={}))
    assert "planApproved" not in task_control.read_control(spec_dir)
    assert not ReviewState.load(spec_dir).approved
    start.assert_not_awaited()


@pytest.mark.parametrize("durable", [False, True])
async def test_admission_refuses_pending_approval(
    spec_dir: Path, monkeypatch: pytest.MonkeyPatch, durable: bool
) -> None:
    _question(spec_dir, "open")
    service = agent_service.AgentService()
    spawn = AsyncMock()
    admit = AsyncMock()
    monkeypatch.setattr(service, "_store_enabled", durable)
    monkeypatch.setattr(service, "_start_build_unit", spawn)
    monkeypatch.setattr(service, "_start_task_execution_durable", admit)

    with pytest.raises(GatedTaskRefusedError, match=APPROVAL_PENDING):
        await service.start_task_execution(TASK, spec_dir.parents[2], spec_dir.name)

    spawn.assert_not_awaited()
    admit.assert_not_awaited()
    assert not service.is_queued(TASK)


@pytest.mark.parametrize("question_status", [None, "open", "expired", "rejected"])
async def test_copilot_dispatch_requires_approval_clear(
    client: AsyncClient,
    spec_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    question_status: str | None,
) -> None:
    dispatch = Mock(return_value={"agent_handle": "copilot", "dispatched_at": "now"})
    monkeypatch.setattr(copilot_dispatch_service, "is_dispatch_enabled", lambda: True)
    monkeypatch.setattr(
        copilot_dispatch_service.CopilotDispatchService, "dispatch", dispatch
    )
    monkeypatch.setattr(execution, "_bound_github_repo", lambda _project: "owner/repo")
    monkeypatch.setattr(execution, "project_github_token", lambda _path: nullcontext())
    monkeypatch.setattr(execution, "emit_task_status", AsyncMock())
    monkeypatch.setattr(execution, "spawn", lambda coro: coro.close())
    if question_status is not None:
        _question(spec_dir, question_status)

    response = await client.post(
        f"{URL}/dispatch-to-copilot",
        json={"repo_full_name": "owner/repo", "issue_number": 42},
    )

    if question_status is None:
        assert response.status_code == 200
        assert response.json()["dispatched"] is True
        dispatch.assert_called_once_with("owner/repo", 42)
    else:
        _assert_approval_pending(response)
        dispatch.assert_not_called()
        assert not (spec_dir / "task_metadata.json").exists()


@pytest.mark.parametrize("durable", [False, True])
async def test_drain_skips_pending_approval_and_promotes_next(
    spec_dir: Path, monkeypatch: pytest.MonkeyPatch, durable: bool
) -> None:
    project_path = spec_dir.parents[2]
    next_task = "project:043-next"
    _question(spec_dir, "open")
    task_control.write_control(
        spec_dir, status="human_review", review_reason="needs_input"
    )
    service = agent_service.AgentService()
    spawn = AsyncMock()
    dispatch = AsyncMock()
    monkeypatch.setattr(service, "_store_enabled", durable)
    monkeypatch.setattr(service, "_kubejob_backend_enabled", lambda: durable)
    monkeypatch.setattr(service, "_concurrency_cap", lambda: 1)
    monkeypatch.setattr(service, "_spawn_task_execution", spawn)
    monkeypatch.setattr(service, "_dispatch_build_job", dispatch)
    monkeypatch.setattr(agent_kubejob, "backend_config_error", lambda _enabled: None)
    monkeypatch.setattr(agent_kubejob, "inpod_agent_refusal", lambda: None)

    blocked = SpawnArgs(project_path=str(project_path), spec_id=spec_dir.name)
    ready = SpawnArgs(project_path=str(project_path), spec_id="043-next")
    store = SimpleNamespace(
        drain=AsyncMock(return_value=[(TASK, blocked), (next_task, ready)]),
        mark_terminal=AsyncMock(),
    )
    if durable:
        monkeypatch.setattr(service, "_store", lambda: store)
    else:
        for task_id, args in ((TASK, blocked), (next_task, ready)):
            service._task_queue.append(
                QueuedTask(
                    task_id=task_id, **{**asdict(args), "project_path": project_path}
                )
            )

    await service._drain_queue()

    if durable:
        store.mark_terminal.assert_awaited_once_with(
            TASK, "failed", error="spawn failed on dequeue"
        )
        dispatch.assert_awaited_once()
        assert dispatch.await_args_list[0].kwargs["task_id"] == next_task
        spawn.assert_not_awaited()
    else:
        assert service.get_queued_tasks() == []
        spawn.assert_awaited_once()
        assert spawn.await_args_list[0].kwargs["task_id"] == next_task
        dispatch.assert_not_awaited()
    assert not service.is_running(TASK)
    assert task_control.read_control(spec_dir)["status"] == "human_review"


async def test_start_still_launches_with_only_factual_questions(
    client: AsyncClient, spec_dir: Path, build_start: AsyncMock
) -> None:
    _question(spec_dir, "open", approval=False)
    task_control.write_control(
        spec_dir, status="human_review", review_reason="needs_input"
    )

    assert (await client.post(f"{URL}/start", json={})).status_code == 200
    build_start.assert_awaited_once()


async def test_start_ignores_migrated_rejection(
    client: AsyncClient, spec_dir: Path, build_start: AsyncMock
) -> None:
    (spec_dir / "implementation_plan.json").write_text(
        '{"status":"human_review","phases":[],"reviewReason":"approval_rejected"}',
        encoding="utf-8",
    )
    assert (await client.post(f"{URL}/start", json={})).status_code == 200
    build_start.assert_awaited_once()


async def test_start_holds_persisted_rejection_without_question(
    client: AsyncClient, spec_dir: Path
) -> None:
    task_control.write_control(
        spec_dir, status="human_review", review_reason="approval_rejected"
    )
    _assert_approval_pending(await client.post(f"{URL}/start", json={}))


async def test_retry_closes_rejections_before_fixer(
    client: AsyncClient,
    spec_dir: Path,
    build_start: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rejected = [_question(spec_dir, "rejected", number=number) for number in (1, 2)]
    task_control.write_control(
        spec_dir, status="human_review", review_reason="approval_rejected"
    )
    review = spec_dir / "review_state.json"
    review.write_text(
        '{"reason":"approval_rejected","qid":"q-000000000001","approved":false}',
        encoding="utf-8",
    )

    async def fix(directory: Path) -> dict[str, bool]:
        assert directory == spec_dir
        assert not approval_blocked(directory)
        for question in rejected:
            record = json.loads(question.read_text(encoding="utf-8"))
            assert record["status"] == "rejected"
            assert record["closed_at"].endswith("Z")
            assert datetime.fromisoformat(record["closed_at"]).tzinfo == UTC
        assert "reviewReason" not in task_control.read_control(spec_dir)
        assert json.loads(review.read_text(encoding="utf-8")) == {"approved": False}
        return {"scheduled": True}

    monkeypatch.setattr(execution, "_default_fixer", fix)
    body = {"fix_request_md": "Keep the legacy table and update the export."}
    preview = await client.post(
        f"{URL}/apply-correction", json={**body, "confirm": False}
    )
    assert preview.status_code == 200
    assert not preview.json()["started"]
    assert not (spec_dir / "QA_FIX_REQUEST.md").exists()
    assert all(
        "closed_at" not in question.read_text(encoding="utf-8") for question in rejected
    )
    _assert_approval_pending(await client.post(f"{URL}/start", json={}))

    retry = await client.post(f"{URL}/apply-correction", json={**body, "confirm": True})
    assert retry.status_code == 200
    assert retry.json()["scheduled"] is True
    assert (await client.post(f"{URL}/start", json={})).status_code == 200
    build_start.assert_awaited_once()


@pytest.mark.parametrize("status", ["open", "expired"])
async def test_retry_cannot_bypass_pending_approval(
    client: AsyncClient, spec_dir: Path, status: str
) -> None:
    rejected = _question(spec_dir, "rejected")
    _question(spec_dir, status, number=2)
    task_control.write_control(spec_dir, review_reason="approval_rejected")

    _assert_approval_pending(
        await client.post(
            f"{URL}/apply-correction",
            json={"fix_request_md": "Use an alternative", "confirm": True},
        )
    )
    assert "closed_at" not in rejected.read_text(encoding="utf-8")
    assert task_control.read_control(spec_dir)["reviewReason"] == "approval_rejected"


async def test_paused_task_can_be_archived(client: AsyncClient, spec_dir: Path) -> None:
    _question(spec_dir, "rejected")
    task_control.write_control(
        spec_dir, status="human_review", review_reason="approval_rejected"
    )
    response = await client.post(
        "/api/projects/project/tasks/archive", json={"taskIds": [TASK]}
    )
    assert response.status_code == 200
    assert response.json()["archivedCount"] == 1


@pytest.mark.parametrize("route", [("PATCH", "/status"), ("PATCH", ""), ("PUT", "")])
@pytest.mark.parametrize("reason", ["needs_input", "approval_rejected"])
@pytest.mark.parametrize("column", ["done", "backlog"])
async def test_paused_task_cannot_move_columns(
    client: AsyncClient,
    spec_dir: Path,
    route: tuple[str, str],
    reason: str,
    column: str,
) -> None:
    task_control.write_control(spec_dir, status="human_review", review_reason=reason)
    method, suffix = route
    response = await client.request(method, URL + suffix, json={"status": column})
    assert response.status_code == 409
    assert response.json() == {"detail": f"task is paused: {reason}"}
