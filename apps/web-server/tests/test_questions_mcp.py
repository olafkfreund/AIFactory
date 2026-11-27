"""Question MCP tools against the scoped proxy."""

import json
from collections.abc import AsyncIterator
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from agents.questions import read_question, update_question, write_question
from fastapi import FastAPI
from server import project_registry
from server.database.engine import get_db
from server.mcp_remote import tools
from server.mcp_remote.auth import AuthenticatedKey
from server.mcp_stdio import auth as proxy_auth
from server.routes import project_authz, task_service
from server.services import question_answers, task_control
from starlette import status

proxy: Any = import_module("server.mcp_stdio.router")

CONTRACT = json.loads(
    (
        Path(__file__).resolve().parents[3] / "tests/fixtures/questions_api.json"
    ).read_text()
)
MCP = CONTRACT["mcp"]
PREFIX = "/api/mcp-stdio"
TASK_ID = "a:001-task"
PORTAL = "https://portal.example/p/alpha/tasks/a%3A001-task"


@pytest.fixture
async def question_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[
    tuple[
        httpx.AsyncClient,
        dict[str, AuthenticatedKey],
        Path,
        AsyncMock,
        AsyncMock,
    ]
]:
    projects = {
        pid: {"name": pid.upper(), "slug": slug, "org_id": pid, "path": tmp_path / pid}
        for pid, slug in (("a", "alpha"), ("b", "beta"), ("c", "gamma"))
    }
    monkeypatch.setenv("AIFACTORY_PORTAL_URL", "https://portal.example/")
    monkeypatch.setattr(proxy, "load_projects", lambda: projects)
    monkeypatch.setattr(task_service, "load_projects", lambda: projects)
    monkeypatch.setattr(
        project_registry, "aload_projects", AsyncMock(return_value=projects)
    )
    monkeypatch.setattr(project_authz, "_auth_disabled", lambda: False)
    monkeypatch.setattr(project_authz, "_audit_authz_denial", AsyncMock())

    async def team_grant(
        principal: dict[str, object], project: dict[str, object], _db: object
    ) -> tuple[str | None, None]:
        if principal["org_id"] != project["org_id"]:
            return None, None
        return ("viewer" if principal["id"] == "viewer" else "member"), None

    monkeypatch.setattr(project_authz, "_team_grant", team_grant)
    keys = {
        token: AuthenticatedKey(
            "test-key", frozenset(scopes), user, org_id=org, raw_token=token
        )
        for token, scopes, user, org in (
            ("acw_a", ("mcp:read", "mcp:write"), "alice", "a"),
            ("acw_b", ("mcp:read",), "bob", "b"),
            ("acw_viewer", ("mcp:write",), "viewer", "a"),
            ("acw_ownerless", ("mcp:read", "mcp:write"), None, "a"),
        )
    }
    legacy = "legacy"
    monkeypatch.setattr(
        proxy_auth, "_is_legacy_api_token", lambda token: token == legacy
    )
    monkeypatch.setattr(proxy_auth, "_warn_legacy_api_token_once", lambda: None)
    monkeypatch.setattr(
        "server.mcp_remote.auth.authenticate",
        AsyncMock(side_effect=lambda header: keys[header[7:]]),
    )
    audit = AsyncMock()
    monkeypatch.setattr(proxy, "log_audit_event_bg", audit)
    start = AsyncMock(
        return_value={"success": True, "message": "Task execution started"}
    )
    monkeypatch.setattr(question_answers, "_start", start)
    db = MagicMock()
    db.in_transaction.return_value = False
    db.rollback = AsyncMock()

    async def remote_db() -> AsyncIterator[MagicMock]:
        yield db

    monkeypatch.setattr(tools, "get_db", remote_db)
    api = FastAPI()
    api.include_router(proxy.router)
    api.dependency_overrides[get_db] = lambda: db
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://other-host.test"
    ) as client:

        async def internal(
            method: str, path: str, key: AuthenticatedKey, **kwargs: Any
        ) -> Any:
            response = await client.request(
                method, path, headers=_bearer(key.raw_token), **kwargs
            )
            response.raise_for_status()
            return response.json()

        monkeypatch.setattr(tools, "_call_internal", internal)
        yield client, keys, tmp_path, audit, start


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _spec(root: Path, task_id: str = TASK_ID) -> Path:
    project, _, spec = task_id.partition(":")
    return root / project / ".aifactory" / "specs" / spec


def _ask(
    spec: Path, *, approval: bool = False, status: str = "open", paused: bool = True
) -> str:
    if paused:
        task_control.write_control(
            spec, status="human_review", review_reason="needs_input"
        )
    record = write_question(
        spec,
        question="Which timezone?",
        choices=["UTC", "Local"],
        run_id="a" * 32,
        asked_by_model="test",
        needs_approval=approval,
        context="private context",
        default="UTC",
    )
    qid = str(record["id"])
    if status != "open":
        changes = {"status": status}
        if status == "answered":
            changes["delivery"] = "answered"
        update_question(spec, qid, expected_status="open", changes=changes)
    return qid


def _answer_path(qid: str) -> str:
    return f"{PREFIX}/tasks/{TASK_ID}/questions/{qid}/answer"


def _body(result: dict[str, Any]) -> Any:
    return json.loads(result["content"][0]["text"])


async def test_pending_scopes_fields_age_and_portal(question_proxy: Any) -> None:
    client, keys, root, _, _ = question_proxy
    spec = _spec(root)
    opened = _ask(spec)
    update_question(
        spec,
        opened,
        expected_status="open",
        changes={"asked_at": "2090-01-01T00:00:00Z"},
    )
    expired = _ask(spec, status="expired")
    _ask(spec, status="answered")
    _ask(_spec(root, "a:002-task"), status="rejected", approval=True)
    _ask(_spec(root, "a:003-task"), paused=False)
    foreign = _ask(_spec(root, "b:001-task"))
    response = await client.get(f"{PREFIX}/questions/pending", headers=_bearer("acw_a"))
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body.keys() == MCP["questions_pending"]["result"].keys()
    assert {record["id"] for record in body["questions"]} == {opened, expired}
    for record in body["questions"]:
        assert (
            record.keys() == MCP["questions_pending"]["result"]["questions"][0].keys()
        )
        assert record["task_id"] == TASK_ID
        assert record["portal_url"] == PORTAL
        assert record["project"] == "A"
        assert record["status"] == ("open" if record["id"] == opened else "expired")
        assert isinstance(record["age_seconds"], int) and record["age_seconds"] >= 0
        if record["id"] == opened:
            assert record["age_seconds"] == 0
    remote = await tools.dispatch_tool_call(
        "aifactory.questions_pending", {}, keys["acw_a"]
    )
    assert {record["id"] for record in _body(remote)["questions"]} == {opened, expired}
    remote = await tools.dispatch_tool_call(
        "aifactory.questions_pending", {}, keys["acw_b"]
    )
    assert [record["id"] for record in _body(remote)["questions"]] == [foreign]


async def test_unreadable_project_is_skipped(
    question_proxy: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, root, _, _ = question_proxy
    _ask(_spec(root))

    def denied(_path: Path) -> list[Path]:
        raise PermissionError

    monkeypatch.setattr(proxy, "get_spec_dirs", denied)
    response = await client.get(f"{PREFIX}/questions/pending", headers=_bearer("acw_a"))
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"questions": []}


async def test_pending_without_portal_url_uses_relative_paths(
    question_proxy: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, root, _, _ = question_proxy
    monkeypatch.delenv("AIFACTORY_PORTAL_URL")
    _ask(_spec(root))
    host = "request-host.example"
    response = await client.get(
        f"{PREFIX}/questions/pending", headers={**_bearer("acw_a"), "Host": host}
    )
    assert response.status_code == status.HTTP_200_OK
    [record] = response.json()["questions"]
    assert record["portal_url"].startswith("/p/")
    assert host not in record["portal_url"]


async def test_answer_reuses_rest_validation_and_delivery(question_proxy: Any) -> None:
    client, keys, root, audit, start = question_proxy
    spec = _spec(root)
    qid = _ask(spec)
    foreign = _ask(_spec(root, "a:002-task"))
    headers = _bearer("acw_a")
    for candidate, answer, error in (
        (qid, "Wrong", "invalid_answer"),
        (foreign, "UTC", "question_not_found"),
        ("bad-id", "UTC", "question_not_found"),
    ):
        response = await client.post(
            _answer_path(candidate), headers=headers, json={"answer": answer}
        )
        assert response.status_code == CONTRACT["errors"][error]["status"]
        assert response.json() == CONTRACT["errors"][error]["body"]
    response = await client.post(
        _answer_path(qid), headers=_bearer("acw_viewer"), json={"answer": "UTC"}
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json() == CONTRACT["errors"]["not_project_member"]["body"]
    assert read_question(spec, qid)["status"] == "open"
    audit.assert_not_awaited()
    result = await tools.dispatch_tool_call(
        "aifactory.answer_question",
        {"task_id": TASK_ID, "qid": qid, "answer": "UTC"},
        keys["acw_a"],
    )
    body = _body(result)
    assert body.keys() == MCP["answer_question"]["result"].keys()
    record = body["question"]
    assert record.keys() == CONTRACT["list"]["response"]["body"]["questions"][0].keys()
    assert MCP["answer_question"]["result"]["question"].keys() <= record.keys()
    expected = {
        "answer": "UTC",
        "channel": "mcp",
        "auth": "key:test-key",
        "answered_by": "alice",
        "delivery": "launched",
    }
    stored = read_question(spec, qid)
    assert {field: record[field] for field in expected} == expected
    assert {field: stored[field] for field in expected} == expected
    start.assert_awaited_once()
    audit.assert_awaited_once()
    assert audit.call_args.kwargs["action"] == "mcp.task.answer_question"
    assert audit.call_args.kwargs["details"] == {"mcp_key_id": "test-key"}
    response = await client.post(
        _answer_path(qid), headers=headers, json={"answer": "UTC"}
    )
    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json() == CONTRACT["errors"]["already_answered"]["body"]


async def test_approval_refusal_has_link_and_leaves_record_unchanged(
    question_proxy: Any,
) -> None:
    client, keys, root, audit, _ = question_proxy
    spec = _spec(root)
    qid = _ask(spec, approval=True)
    path = spec / "questions" / f"{qid}.json"
    before = path.read_bytes()
    response = await client.post(
        _answer_path(qid),
        headers=_bearer("acw_a"),
        json={
            "decision": "approve",
            "confirm_token": "untrusted",
            "answered_by": "spoof",
        },
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json() == {
        **CONTRACT["errors"]["approval_not_interactive"]["body"],
        "portal_url": PORTAL,
    }
    result = await tools.dispatch_tool_call(
        "aifactory.answer_question",
        {"task_id": TASK_ID, "qid": qid, "answer": "UTC"},
        keys["acw_a"],
    )
    assert result["isError"] is True
    assert _body(result) == {
        **MCP["answer_question"]["refusal_approval"],
        "portal_url": PORTAL,
    }
    assert path.read_bytes() == before
    audit.assert_not_awaited()


@pytest.mark.parametrize("token", ["legacy", "acw_ownerless"])
async def test_personal_identity_required_on_both_routes(
    question_proxy: Any, token: str
) -> None:
    client, _, root, audit, _ = question_proxy
    spec = _spec(root)
    qid = _ask(spec)
    headers = _bearer(token)
    pending = await client.get(f"{PREFIX}/questions/pending", headers=headers)
    answered = await client.post(
        _answer_path(qid), headers=headers, json={"answer": "UTC"}
    )
    for response in (pending, answered):
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json() == CONTRACT["errors"]["legacy_token"]["body"]
    assert read_question(spec, qid)["status"] == "open"
    audit.assert_not_awaited()


@pytest.mark.parametrize(
    "tool", ["aifactory.questions_pending", "aifactory.answer_question"]
)
async def test_remote_refuses_ownerless_key_before_authorization(
    question_proxy: Any, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    _, keys, _, _, _ = question_proxy
    loopback = AsyncMock()
    authorize_call = AsyncMock()
    authorize_project = AsyncMock()
    monkeypatch.setattr(tools, "_call_internal", loopback)
    monkeypatch.setattr(tools, "_authorize_call", authorize_call)
    monkeypatch.setattr(tools, "authorize_project_for_user", authorize_project)
    args = {}
    if tool == "aifactory.answer_question":
        args = {**MCP["answer_question"]["arguments"], "task_id": TASK_ID}
    result = await tools.dispatch_tool_call(tool, args, keys["acw_ownerless"])
    assert result["isError"] is True
    assert _body(result) == MCP["answer_question"]["refusal_ownerless_or_legacy_key"]
    loopback.assert_not_awaited()
    authorize_call.assert_not_awaited()
    authorize_project.assert_not_awaited()


async def test_remote_answer_requires_project_membership(
    question_proxy: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, keys, root, _, _ = question_proxy
    spec = _spec(root)
    qid = _ask(spec)
    path = spec / "questions" / f"{qid}.json"
    before = path.read_bytes()
    loopback = AsyncMock()
    authorize_project = AsyncMock(wraps=project_authz.authorize_project_for_user)
    monkeypatch.setattr(tools, "_call_internal", loopback)
    monkeypatch.setattr(tools, "authorize_project_for_user", authorize_project)
    result = await tools.dispatch_tool_call(
        "aifactory.answer_question",
        {"task_id": TASK_ID, "qid": qid, "answer": "UTC"},
        keys["acw_viewer"],
    )
    assert result["isError"] is True
    assert (
        result["content"][0]["text"]
        == "Error: aifactory.answer_question denied: Forbidden"
    )
    authorize_project.assert_awaited_once()
    assert authorize_project.call_args.args[1] == "a"
    assert authorize_project.call_args.args[3] == "member"
    loopback.assert_not_awaited()
    assert path.read_bytes() == before


async def test_read_scope_cannot_answer(question_proxy: Any) -> None:
    client, keys, root, _, _ = question_proxy
    qid = _ask(_spec(root, "b:001-task"))
    task_id = "b:001-task"
    response = await client.post(
        f"{PREFIX}/tasks/{task_id}/questions/{qid}/answer",
        headers=_bearer("acw_b"),
        json={"answer": "UTC"},
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    result = await tools.dispatch_tool_call(
        "aifactory.answer_question",
        {"task_id": task_id, "qid": qid, "answer": "UTC"},
        keys["acw_b"],
    )
    assert result["isError"] is True
    assert read_question(_spec(root, task_id), qid)["status"] == "open"


async def test_remote_no_task_allowlist_and_hostile_qids(
    question_proxy: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, keys, _, _, _ = question_proxy
    loopback = AsyncMock()
    monkeypatch.setattr(tools, "_call_internal", loopback)
    key = AuthenticatedKey(
        "test-key",
        frozenset({"mcp:read", "mcp:write", "task:merge"}),
        "alice",
        org_id="a",
    )
    for definition in tools.get_tool_definitions():
        name = definition["name"]
        if name in {"aifactory.list_projects", "aifactory.questions_pending"}:
            continue
        result = await tools.dispatch_tool_call(name, {}, key)
        arg = "project_id" if name == "aifactory.list_tasks" else "task_id"
        assert result["isError"] is True
        assert (
            result["content"][0]["text"] == f"Error: missing required argument: {arg}"
        )
    for qid in ("../else", "q-..", "q-hello?x", "q-%2f"):
        result = await tools.dispatch_tool_call(
            "aifactory.answer_question",
            {"task_id": TASK_ID, "qid": qid, "answer": "UTC"},
            keys["acw_a"],
        )
        assert result["isError"] is True
    loopback.assert_not_awaited()
