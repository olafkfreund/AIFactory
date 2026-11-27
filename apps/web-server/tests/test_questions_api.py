"""Question API contract, approval trust boundary, and durable delivery."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol
from unittest.mock import AsyncMock, MagicMock

import pytest
from agents.inbox import drain_unread, format_for_prompt
from agents.questions import (
    ASKED_AT_FORMAT,
    read_question,
    update_question,
    write_question,
)
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from server import auth, project_registry
from server.database.engine import get_db
from server.mcp_remote import auth as key_auth
from server.routes import execution, project_authz, questions, task_service
from server.services import agent_service, inbox_service, question_answers, task_control

CONTRACT = json.loads(
    (
        Path(__file__).resolve().parents[3] / "tests/fixtures/questions_api.json"
    ).read_text()
)
TASK_ID = "p:042-export-csv"
URL = f"/api/tasks/{TASK_ID}/questions"
BEARER = {"Authorization": "Bearer session-a"}
COOKIE = {"Cookie": "access_token=session-a"}
REVIEW = {"reason": "needs_input", "approved": True, "operator_note": "retain"}


class Response(Protocol):
    @property
    def status_code(self) -> int: ...

    def json(self) -> Any: ...


@dataclass
class QuestionApp:
    client: TestClient
    spec: Path
    start: AsyncMock

    def ask(self, *, run_id: str = "a" * 32, **fields: Any) -> str:
        record = write_question(
            self.spec,
            question="Which timezone?",
            run_id=run_id,
            asked_by_model="claude-test",
            **fields,
        )
        return str(record["id"])

    def answer(
        self, qid: str, body: dict[str, Any], headers: dict[str, str] | None = None
    ) -> Response:
        return self.client.post(
            f"{URL}/{qid}/answer", json=body, headers=headers or BEARER
        )

    def token(self, qid: str) -> str:
        response = self.client.get(f"{URL}/{qid}/confirm-token", headers=COOKIE)
        assert response.status_code == CONTRACT["confirm_token"]["response"]["status"]
        assert set(response.json()) == set(
            CONTRACT["confirm_token"]["response"]["body"]
        )
        assert response.headers["Cache-Control"] == "no-store"
        token = response.json()["confirm_token"]
        assert re.fullmatch(r"v1\.[0-9]+\.[a-zA-Z0-9_-]{43}", token)
        return str(token)


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> QuestionApp:
    spec = tmp_path / ".aifactory" / "specs" / "042-export-csv"
    spec.mkdir(parents=True)
    task_control.write_control(spec, status="human_review", review_reason="needs_input")
    (spec / "review_state.json").write_text(json.dumps(REVIEW))
    projects = {"p": {"path": str(tmp_path), "org_id": "org"}}
    monkeypatch.setattr(task_service, "load_projects", lambda: projects)
    monkeypatch.setattr(
        project_registry,
        "aload_projects",
        AsyncMock(return_value=projects),
    )
    monkeypatch.setattr(project_authz, "_auth_disabled", lambda: False)
    monkeypatch.setattr(project_authz, "_audit_authz_denial", AsyncMock())

    async def role(principal: dict[str, object], *_args: object) -> tuple[str, None]:
        return ("viewer" if principal["id"] == "viewer" else "member"), None

    monkeypatch.setattr(project_authz, "_effective_role", role)
    settings = SimpleNamespace(DISABLE_AUTH=False, JWT_SECRET=secrets.token_hex(32))
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.setattr(questions, "get_settings", lambda: settings)
    monkeypatch.setattr(auth, "_is_legacy_api_token", lambda value: value == "legacy")
    monkeypatch.setattr(auth, "_warn_legacy_api_token_once", lambda: None)
    monkeypatch.setattr(
        auth,
        "_try_decode_jwt",
        lambda token: {
            "session-a": {"sub": "alice"},
            "session-b": {"sub": "alice"},
            "session-c": {"sub": "bob"},
            "viewer": {"sub": "viewer"},
        }.get(token),
    )

    async def key(header: str) -> SimpleNamespace:
        owner = "alice" if header == "Bearer acw_personal" else None
        return SimpleNamespace(
            user_id=owner, key_id="personal", org_id="org", scopes=["api"]
        )

    monkeypatch.setattr(key_auth, "authenticate_rest", key)
    start = AsyncMock(
        return_value={"success": True, "message": "Task execution started"}
    )
    monkeypatch.setattr(execution, "start_task", start)
    monkeypatch.setattr(
        agent_service,
        "get_agent_service",
        lambda: SimpleNamespace(
            is_running=lambda _task_id: False, _task_queue=[], _store_enabled=False
        ),
    )

    api = FastAPI()
    api.add_middleware(auth.TokenAuthMiddleware)
    api.include_router(questions.router, prefix="/api/tasks")
    db = MagicMock()
    db.in_transaction.return_value = False
    db.rollback = AsyncMock()
    api.dependency_overrides[get_db] = lambda: db
    return QuestionApp(TestClient(api), spec, start)


def _error(response: Response, name: str) -> None:
    expected = CONTRACT["errors"][name]
    assert response.status_code == expected["status"]
    assert response.json() == expected["body"]


def test_list_contract_and_empty(app: QuestionApp) -> None:
    empty = {**CONTRACT["list_empty"]["response"]["body"], "task_id": TASK_ID}
    response = app.client.get(URL, headers=BEARER)
    assert response.status_code == 200
    assert response.json() == empty

    qid = app.ask(choices=["UTC", "Local"])
    path = app.spec / "questions" / "q-000000000000.json"
    path.write_text("{invalid")
    response = app.client.get(URL, headers=BEARER)
    result = response.json()
    expected = CONTRACT["list"]["response"]["body"]
    assert response.status_code == 200
    assert result.keys() == expected.keys()
    assert result["review_reason"] == "needs_input"
    question = result["questions"][0]
    assert len(result["questions"]) == 1
    assert question.keys() == expected["questions"][0].keys()
    assert question["id"] == qid
    assert question["task_id"] == TASK_ID
    expiry = datetime.strptime(question["expires_at"], ASKED_AT_FORMAT).replace(
        tzinfo=UTC
    )
    asked = datetime.strptime(question["asked_at"], ASKED_AT_FORMAT).replace(tzinfo=UTC)
    assert (expiry - asked).total_seconds() == 1800

    task_control.write_control(app.spec, clear_review_reason=True)
    assert app.client.get(URL, headers=BEARER).json() == empty
    task_control.control_file_path(app.spec).unlink()
    (app.spec / "implementation_plan.json").write_text('{"reviewReason":"needs_input"}')
    assert app.client.get(URL, headers=BEARER).json() == empty
    assert (
        app.client.get("/api/tasks/p:missing/questions", headers=BEARER).status_code
        == 404
    )
    assert (
        app.client.get(
            "/api/tasks/missing:042-export-csv/questions", headers=BEARER
        ).status_code
        == 404
    )


def test_traversal_task_id_leaves_question_files_untouched(app: QuestionApp) -> None:
    target = app.spec.parents[1]
    question = write_question(
        target,
        question="Outside the specs directory?",
        run_id="a" * 32,
        asked_by_model="test",
    )
    before = {path: path.read_bytes() for path in target.rglob("*") if path.is_file()}
    url = "/api/tasks/p:../questions"
    for response in (
        app.client.get(url, headers=BEARER),
        app.client.post(
            f"{url}/{question['id']}/answer", json={"answer": "UTC"}, headers=BEARER
        ),
    ):
        assert response.status_code == 404
        assert response.json() == {"detail": "Task spec not found"}
    after = {path: path.read_bytes() for path in target.rglob("*") if path.is_file()}
    assert after == before
    app.start.assert_not_awaited()


def test_factual_exact_choice_and_context_audit(app: QuestionApp) -> None:
    qid = app.ask(choices=["UTC", "Local"])
    for body in (
        {"answer": "utc"},
        {"answer": "UTC", "decision": None},
        {"answer": ""},
    ):
        _error(app.answer(qid, body), "invalid_answer")
    response = app.answer(
        qid,
        {
            "answer": "UTC",
            "answered_by": "spoofed",
            "channel": "cfactory",
            "auth": "key:spoofed",
            "answered_at": "spoofed",
            "needs_approval": True,
        },
    )
    fixture = CONTRACT["answer_factual"]["response"]
    assert response.status_code == fixture["status"]
    result = response.json()
    assert result.keys() == fixture["body"].keys()
    question = result["question"]
    assert question.keys() == fixture["body"]["question"].keys()
    assert question["answer"] == "UTC"
    assert question["answered_by"] == "alice"
    assert question["channel"] == "portal"
    assert question["auth"] == "session"
    assert question["needs_approval"] is False
    assert re.fullmatch(r"a-[0-9a-f]{12}", question["answer_id"])
    assert question["answered_at"].endswith("Z")
    _error(app.answer(qid, {"answer": "UTC"}), "already_answered")


def test_expiry_race_and_personal_key(
    app: QuestionApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    qid = app.ask()
    commit_answer = question_answers.record_answer

    def expire_before_write(*args: Any, **kwargs: Any) -> dict[str, Any]:
        assert update_question(
            app.spec, qid, expected_status="open", changes={"status": "expired"}
        )
        result: dict[str, Any] = commit_answer(*args, **kwargs)
        return result

    monkeypatch.setattr(question_answers, "record_answer", expire_before_write)
    answer = "  use this\nexactly  "
    response = app.answer(
        qid, {"answer": answer}, {"Authorization": "Bearer acw_personal"}
    )
    assert response.status_code == 200
    question = response.json()["question"]
    assert (question["answered_by"], question["channel"], question["auth"]) == (
        "alice",
        "mcp",
        "key:personal",
    )
    assert question["answer"] == answer
    (message,) = inbox_service.list_messages(app.spec)
    assert answer in message["text"]
    assert message["qid"] == qid
    assert message["answer_id"] == question["answer_id"]


def test_invalid_or_foreign_question(app: QuestionApp) -> None:
    other = app.spec.with_name("other")
    foreign = write_question(
        other, question="Foreign?", run_id="a" * 32, asked_by_model="test"
    )
    for qid in ("bad", "q-000000000001", foreign["id"]):
        _error(app.answer(qid, {"answer": "UTC"}), "question_not_found")
        _error(
            app.client.get(f"{URL}/{qid}/confirm-token", headers=COOKIE),
            "question_not_found",
        )


@pytest.mark.parametrize(
    ("headers", "error"),
    [
        ({"Authorization": "Bearer legacy"}, "legacy_token"),
        ({"Authorization": "Bearer acw_ownerless"}, "legacy_token"),
        ({"Authorization": "Bearer viewer"}, "not_project_member"),
    ],
)
def test_identity_and_role_on_every_route(
    app: QuestionApp, headers: dict[str, str], error: str
) -> None:
    for approval in (False, True):
        qid = app.ask(needs_approval=approval)
        _error(app.client.get(URL, headers=headers), error)
        _error(app.answer(qid, {"answer": "UTC"}, headers), error)
        _error(app.client.get(f"{URL}/{qid}/confirm-token", headers=headers), error)


@pytest.mark.parametrize(
    "headers",
    [
        BEARER,
        {**COOKIE, **BEARER},
        {**COOKIE, "Authorization": ""},
        {"Authorization": "Bearer acw_personal"},
    ],
)
def test_approval_rejects_non_cookie_transport(
    app: QuestionApp, headers: dict[str, str]
) -> None:
    qid = app.ask(needs_approval=True)
    token = app.token(qid)
    _error(
        app.answer(qid, {"decision": "approve", "confirm_token": token}, headers),
        "approval_not_interactive",
    )
    _error(
        app.client.get(f"{URL}/{qid}/confirm-token", headers=headers),
        "approval_not_interactive",
    )


def test_dev_principal_cannot_approve(
    app: QuestionApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    qid = app.ask(needs_approval=True)
    token = app.token(qid)
    monkeypatch.setattr(
        auth, "get_settings", lambda: SimpleNamespace(DISABLE_AUTH=True)
    )
    _error(
        app.answer(qid, {"decision": "approve", "confirm_token": token}, COOKIE),
        "approval_not_interactive",
    )
    _error(
        app.client.get(f"{URL}/{qid}/confirm-token", headers=COOKIE),
        "approval_not_interactive",
    )


def test_approval_bound_and_single_use(
    app: QuestionApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = SimpleNamespace(now=1_800_000_000)
    monkeypatch.setattr(questions, "time", SimpleNamespace(time=lambda: clock.now))
    qid = app.ask(needs_approval=True)
    other = app.ask(needs_approval=True)
    token = app.token(qid)
    body = {"decision": "approve", "confirm_token": token, "needs_approval": False}
    for target, credentials in (
        (other, COOKIE),
        (qid, {"Cookie": "access_token=session-b"}),
        (qid, {"Cookie": "access_token=session-c"}),
    ):
        _error(app.answer(target, body, credentials), "approval_not_interactive")
    _error(app.answer(qid, {"decision": "approve"}, COOKIE), "approval_not_interactive")
    clock.now += 300
    _error(app.answer(qid, body, COOKIE), "approval_not_interactive")

    body["confirm_token"] = app.token(qid)
    _error(app.answer(qid, {**body, "answer": "UTC"}, COOKIE), "invalid_answer")
    _error(app.answer(qid, {**body, "note": "x" * 2001}, COOKIE), "invalid_answer")
    response = app.answer(qid, {**body, "note": "go ahead"}, COOKIE)
    expected = CONTRACT["answer_approval_approve"]["response"]
    assert response.status_code == expected["status"]
    result = response.json()["question"]
    assert result.keys() == expected["body"]["question"].keys()
    assert result["decision"] == "approve"
    assert result["note"] == "go ahead"
    assert result["needs_approval"] is True
    assert result["delivery"] == "inboxed"
    assert app.start.await_count == 0
    _error(app.answer(qid, body, COOKIE), "approval_not_interactive")
    _error(
        app.client.get(f"{URL}/{qid}/confirm-token", headers=COOKIE), "already_answered"
    )


def test_confirm_token_refuses_factual_question(app: QuestionApp) -> None:
    _error(
        app.client.get(f"{URL}/{app.ask()}/confirm-token", headers=COOKIE),
        "approval_not_interactive",
    )


def test_reject_keeps_paused(app: QuestionApp) -> None:
    qid = app.ask(needs_approval=True)
    response = app.answer(
        qid,
        {"decision": "reject", "note": "Keep it", "confirm_token": app.token(qid)},
        COOKIE,
    )
    expected = CONTRACT["answer_approval_reject"]["response"]
    assert response.status_code == expected["status"]
    result = response.json()["question"]
    assert result.keys() == expected["body"]["question"].keys()
    assert result["status"] == "rejected"
    assert result["delivery"] is None
    assert (
        task_control.read_control(app.spec)["reviewReason"]
        == expected["task_review_reason_after"]
    )
    assert (
        json.loads((app.spec / "review_state.json").read_text())["reason"]
        == "approval_rejected"
    )
    assert inbox_service.list_messages(app.spec) == []
    app.start.assert_not_awaited()


async def test_launch_failure_retry_and_no_replay(app: QuestionApp) -> None:
    qid = app.ask()
    app.start.side_effect = [HTTPException(503, "unavailable"), app.start.return_value]
    response = app.answer(qid, {"answer": "UTC"})
    assert response.status_code == 200
    assert response.json()["question"]["delivery"] == "inboxed"
    assert task_control.read_control(app.spec)["reviewReason"] == "needs_input"

    result = await question_answers.advance_delivery(app.spec, TASK_ID, qid)
    assert result["delivery"] == "launched"
    await question_answers.advance_delivery(app.spec, TASK_ID, qid)
    assert app.start.await_count == 2
    assert len(inbox_service.list_messages(app.spec)) == 1
    assert "reviewReason" not in task_control.read_control(app.spec)
    assert json.loads((app.spec / "review_state.json").read_text()) == {
        "approved": True,
        "operator_note": "retain",
    }


@pytest.mark.parametrize("answered", [False, True])
async def test_answer_delivery_rechecks_new_questions_before_admission(
    app: QuestionApp, monkeypatch: pytest.MonkeyPatch, answered: bool
) -> None:
    qid = app.ask()
    identity = question_answers.AnswerIdentity("alice", "portal", "session")
    question_answers.record_answer(app.spec, qid, {"answer": "UTC"}, identity)
    prepare = question_answers._prepare_delivery
    newer: list[str] = []

    def prepare_then_ask(spec_dir: Path, target: str) -> list[dict[str, Any]]:
        batch: list[dict[str, Any]] = prepare(spec_dir, target)
        assert [record["id"] for record in batch] == [qid]
        newer.append(app.ask())
        if answered:
            question_answers.record_answer(
                spec_dir, newer[0], {"answer": "UTC"}, identity
            )
        return batch

    with monkeypatch.context() as patch:
        patch.setattr(question_answers, "_prepare_delivery", prepare_then_ask)
        result = await question_answers.advance_delivery(app.spec, TASK_ID, qid)
    assert len(newer) == 1
    assert result["delivery"] == "inboxed"
    assert "_launch_claimed_at" not in result
    app.start.assert_not_awaited()

    if not answered:
        question_answers.record_answer(app.spec, newer[0], {"answer": "UTC"}, identity)
    result = await question_answers.advance_delivery(app.spec, TASK_ID, qid)
    assert result["delivery"] == "launched"
    assert read_question(app.spec, newer[0])["delivery"] == "launched"
    app.start.assert_awaited_once()


def test_local_resume_delivers_exact_answer_once(
    app: QuestionApp, tmp_path: Path
) -> None:
    qid = app.ask()
    answer = "  Use UTC, please! Keep 'Z'.  "
    response = app.answer(qid, {"answer": answer})
    assert response.status_code == 200
    question = response.json()["question"]
    assert question["delivery"] == "launched"
    app.start.assert_awaited_once()

    worktree_spec = tmp_path / "worktree" / ".aifactory" / "specs" / app.spec.name
    source_inbox = app.spec / "inbox"
    worktree_inbox = worktree_spec / "inbox"
    shutil.copytree(source_inbox, worktree_inbox)
    delivered = drain_unread(worktree_spec, source_spec_dir=app.spec)
    (message,) = delivered
    assert message["qid"] == qid
    assert message["answer_id"] == question["answer_id"]
    assert answer in format_for_prompt(delivered)
    for spec_dir in (app.spec, worktree_spec):
        (stored,) = inbox_service.list_messages(spec_dir)
        assert stored["read"] is True

    shutil.copytree(source_inbox, worktree_inbox, dirs_exist_ok=True)
    assert drain_unread(worktree_spec, source_spec_dir=app.spec) == []


@pytest.mark.parametrize("failure", [None, "list_messages", "mark_read"])
async def test_kubejob_next_answer_retires_earlier_answer(
    app: QuestionApp, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    first = app.ask()
    response = app.answer(first, {"answer": "First"})
    assert response.status_code == 200
    assert response.json()["question"]["delivery"] == "launched"
    app.start.assert_awaited_once()
    (first_message,) = inbox_service.list_messages(app.spec)
    assert first_message["read"] is False

    second = app.ask(run_id="b" * 32)
    task_control.write_control(
        app.spec, status="human_review", review_reason="needs_input"
    )
    app.start.reset_mock()

    def fail(*_args: object) -> None:
        raise OSError("source inbox unavailable")

    async def start(*_args: object) -> dict[str, object]:
        assert {m["qid"]: m["read"] for m in inbox_service.list_messages(app.spec)} == {
            first: True,
            second: False,
        }
        return {"success": True, "message": "Task execution started"}

    app.start.side_effect = start
    with monkeypatch.context() as patch:
        if failure is not None:
            patch.setattr(inbox_service, failure, fail)
        response = app.answer(second, {"answer": "Second"})
    assert response.status_code == 200
    if failure is not None:
        assert response.json()["question"]["delivery"] == "inboxed"
        assert "_launch_claimed_at" not in read_question(app.spec, second)
        app.start.assert_not_awaited()
        await question_answers.advance_delivery(app.spec, TASK_ID, second)
    assert read_question(app.spec, second)["delivery"] == "launched"
    app.start.assert_awaited_once()


def test_last_answer_launches_all_inboxed_questions(app: QuestionApp) -> None:
    first = app.ask()
    last = app.ask()
    assert (
        app.answer(first, {"answer": "First"}).json()["question"]["delivery"]
        == "inboxed"
    )
    app.start.assert_not_awaited()
    assert (
        app.answer(last, {"answer": "Last"}).json()["question"]["delivery"]
        == "launched"
    )
    assert read_question(app.spec, first)["delivery"] == "launched"
    assert {message["qid"] for message in inbox_service.list_messages(app.spec)} == {
        first,
        last,
    }
    app.start.assert_awaited_once()


async def test_recover_after_answer_and_inbox_crashes(
    app: QuestionApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    qid = app.ask()
    identity = question_answers.AnswerIdentity("alice", "mcp", "key:personal")
    record = question_answers.record_answer(app.spec, qid, {"answer": "UTC"}, identity)
    assert record["delivery"] == "answered"

    def crash(*_args: object) -> None:
        raise OSError("simulated crash after inbox append")

    with monkeypatch.context() as patch:
        patch.setattr(question_answers, "_save", crash)
        result = await question_answers.advance_delivery(app.spec, TASK_ID, qid)
        assert result["delivery"] == "answered"
    assert len(inbox_service.drain_unread(app.spec)) == 1
    result = await question_answers.advance_delivery(app.spec, TASK_ID, qid)
    assert result["delivery"] == "launched"
    assert len(inbox_service.list_messages(app.spec)) == 1
    app.start.assert_awaited_once()


async def test_active_launch_recovers_without_second_start(
    app: QuestionApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    qid = app.ask()
    attempt = app.spec / ".question_attempt"
    attempt.write_text("a" * 32)
    monkeypatch.setattr(
        agent_service,
        "get_agent_service",
        lambda: SimpleNamespace(
            is_running=lambda _task_id: True, _task_queue=[], _store_enabled=False
        ),
    )
    finish = question_answers._finish_delivery

    async def start(*_args: object) -> dict[str, object]:
        attempt.write_text("b" * 32)
        return {"success": True, "message": "Task execution started"}

    def crash(*_args: object) -> None:
        raise OSError("simulated crash at admission receipt")

    app.start.side_effect = start
    monkeypatch.setattr(question_answers, "_finish_delivery", crash)
    assert (
        app.answer(qid, {"answer": "UTC"}).json()["question"]["delivery"] == "inboxed"
    )
    newer = app.ask()
    (app.spec / "review_state.json").write_text(json.dumps({**REVIEW, "qid": newer}))
    monkeypatch.setattr(question_answers, "_finish_delivery", finish)
    result = await question_answers.advance_delivery(app.spec, TASK_ID, qid)
    assert result["delivery"] == "launched"
    assert read_question(app.spec, newer)["status"] == "open"
    assert task_control.read_control(app.spec)["reviewReason"] == "needs_input"
    app.start.assert_awaited_once()


async def test_stale_claim_retries_before_a_new_attempt(app: QuestionApp) -> None:
    qid = app.ask()
    identity = question_answers.AnswerIdentity("alice", "portal", "session")
    question_answers.record_answer(app.spec, qid, {"answer": "UTC"}, identity)
    assert update_question(
        app.spec,
        qid,
        expected_status="answered",
        changes={"_launch_claimed_at": 1, "_launch_previous_attempt": None},
    )
    assert (await question_answers.advance_delivery(app.spec, TASK_ID, qid))[
        "delivery"
    ] == "launched"
    app.start.assert_awaited_once()


def test_queued_admission_clears_only_pause_fields(app: QuestionApp) -> None:
    qid = app.ask()
    review = app.spec / "review_state.json"
    review.write_text(json.dumps({**REVIEW, "qid": qid}))

    async def queued(*_args: object) -> dict[str, object]:
        task_control.write_control(app.spec, status="queued", clear_review_reason=True)
        return {"success": True, "status": "queued"}

    app.start.side_effect = queued
    assert (
        app.answer(qid, {"answer": "UTC"}).json()["question"]["delivery"] == "launched"
    )
    assert task_control.read_control(app.spec)["status"] == "queued"
    assert json.loads(review.read_text()) == {
        "approved": True,
        "operator_note": "retain",
    }


def test_plan_review_refusal_keeps_question_pause(app: QuestionApp) -> None:
    qid = app.ask()

    async def refused(*_args: object) -> dict[str, object]:
        task_control.write_control(
            app.spec, status="human_review", review_reason="plan_review"
        )
        return {
            "success": True,
            "status": "human_review",
            "reviewReason": "plan_review",
        }

    app.start.side_effect = refused
    assert (
        app.answer(qid, {"answer": "UTC"}).json()["question"]["delivery"] == "inboxed"
    )
    assert task_control.read_control(app.spec)["reviewReason"] == "needs_input"


async def test_concurrent_advance_has_one_admission(app: QuestionApp) -> None:
    qid = app.ask()
    identity = question_answers.AnswerIdentity("alice", "portal", "session")
    question_answers.record_answer(app.spec, qid, {"answer": "UTC"}, identity)
    started, proceed = asyncio.Event(), asyncio.Event()

    async def start(*_args: object) -> dict[str, object]:
        started.set()
        await proceed.wait()
        return {"success": True, "message": "Task execution started"}

    app.start.side_effect = start
    first = asyncio.create_task(
        question_answers.advance_delivery(app.spec, TASK_ID, qid)
    )
    await started.wait()
    try:
        result = await question_answers.advance_delivery(app.spec, TASK_ID, qid)
        assert result["delivery"] == "inboxed"
    finally:
        proceed.set()
        await first
    assert first.result()["delivery"] == "launched"
    app.start.assert_awaited_once()
