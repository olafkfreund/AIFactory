"""Source question expiry, retry, and checker lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call
from uuid import uuid4

import pytest
from agents.questions import ASKED_AT_FORMAT, update_question, write_question
from fastapi import FastAPI
from server.routes.questions import serialize_question
from server.services import project_settings_store, question_answers, question_checker
from server.services.task_control import write_control

NOW = datetime(2026, 11, 26, tzinfo=UTC)
TASK_ID = "p:001-task"
IDENTITY = question_answers.AnswerIdentity("person", "portal", "session")


@pytest.fixture
def spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(
        question_checker, "load_projects", lambda: {"p": {"path": str(tmp_path)}}
    )
    return tmp_path / ".aifactory" / "specs" / "001-task"


def _question(spec: Path, *, age: int = 60, approval: bool = False) -> str:
    record = write_question(
        spec,
        question="Which timezone?",
        run_id=uuid4().hex,
        asked_by_model="test",
        needs_approval=approval,
    )
    qid = str(record["id"])
    update_question(
        spec,
        qid,
        expected_status="open",
        changes={"asked_at": (NOW - timedelta(minutes=age)).strftime(ASKED_AT_FORMAT)},
    )
    return qid


def _answer(spec: Path, qid: str) -> None:
    question_answers.record_answer(spec, qid, {"answer": "UTC"}, IDENTITY)


async def test_expiry_is_private_one_shot_and_still_answerable(
    spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overdue, fresh = _question(spec), _question(spec, age=30)
    answered, rejected = _question(spec), _question(spec, approval=True)
    _answer(spec, answered)
    question_answers.record_answer(
        spec, rejected, {"decision": "reject"}, IDENTITY, token_digest=uuid4().hex
    )
    unchanged = {
        qid: question_answers.read_record(spec, qid)
        for qid in (fresh, answered, rejected)
    }

    observed = []
    notice = Mock(
        side_effect=lambda: observed.append(question_answers.read_record(spec, overdue))
    )
    monkeypatch.setattr(question_checker, "_notify_expired", notice)
    for _ in range(3):
        await question_checker.QuestionChecker().tick(now=NOW)
    notice.assert_called_once_with()
    assert observed[0]["status"] == "expired"
    assert observed[0]["_expiry_notified_at"] == NOW.strftime(ASKED_AT_FORMAT)
    assert {
        qid: question_answers.read_record(spec, qid) for qid in unchanged
    } == unchanged
    expired = next(
        record
        for record in question_answers.list_records(spec)
        if record["id"] == overdue
    )
    public = serialize_question(TASK_ID, expired)
    assert public["status"] == "expired"
    assert "_expiry_notified_at" not in public

    _answer(spec, overdue)
    assert question_answers.read_record(spec, overdue)["answer"] == "UTC"
    assert question_answers.read_record(spec, overdue)["status"] == "answered"


async def test_expiry_cas_keeps_an_answer_after_scan(
    spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qid = _question(spec)
    expire = question_answers.expire_open

    def answer_first(path: Path, question_id: str, now: datetime) -> bool:
        _answer(path, question_id)
        expired: bool = expire(path, question_id, now)
        return expired

    monkeypatch.setattr(question_answers, "expire_open", answer_first)
    notice = Mock()
    monkeypatch.setattr(question_checker, "_notify_expired", notice)
    await question_checker.QuestionChecker().tick(now=NOW)
    record = question_answers.read_record(spec, qid)
    assert record["status"] == "answered"
    assert "_expiry_notified_at" not in record
    notice.assert_not_called()


async def test_notice_failure_does_not_prevent_other_expiries(
    spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qids = [_question(spec), _question(spec)]
    notice = Mock(side_effect=[RuntimeError("notice failed"), None])
    monkeypatch.setattr(question_checker, "_notify_expired", notice)
    scan = question_checker.QuestionChecker()
    await scan.tick(now=NOW)
    await scan.tick(now=NOW)

    assert notice.call_count == len(qids)
    assert all(
        question_answers.read_record(spec, qid)["status"] == "expired" for qid in qids
    )


async def test_retries_only_pending_paused_answers_with_exponential_backoff(
    spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending, launched = _question(spec), _question(spec)
    _answer(spec, pending)
    _answer(spec, launched)
    update_question(
        spec, launched, expected_status="answered", changes={"delivery": "launched"}
    )
    rejected = _question(spec, approval=True)
    question_answers.record_answer(
        spec, rejected, {"decision": "reject"}, IDENTITY, token_digest=uuid4().hex
    )
    clock = Mock(return_value=0.0)
    monkeypatch.setattr(question_checker, "monotonic", clock)
    advance = AsyncMock(
        side_effect=[{"delivery": "inboxed"}, OSError(), {"delivery": "launched"}]
    )
    monkeypatch.setattr(question_answers, "advance_delivery", advance)
    scan = question_checker.QuestionChecker()

    await scan.tick(now=NOW)
    advance.assert_not_awaited()
    write_control(spec, review_reason="needs_input")
    for seconds, count in [(0, 1), (59, 1), (60, 2), (179, 2), (180, 3)]:
        clock.return_value = seconds
        await scan.tick(now=NOW)
        assert advance.await_count == count
    assert advance.await_args_list == [call(spec, TASK_ID, pending)] * 3
    assert not scan._retries


async def test_retries_only_claimed_answers_after_task_resumes(
    spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claimed, unclaimed = _question(spec), _question(spec)
    _answer(spec, claimed)
    _answer(spec, unclaimed)
    update_question(
        spec,
        claimed,
        expected_status="answered",
        changes={"delivery": "inboxed", "_launch_claimed_at": NOW.timestamp()},
    )
    write_control(spec, status="in_progress", clear_review_reason=True)
    advance = AsyncMock(return_value={"delivery": "launched"})
    monkeypatch.setattr(question_answers, "advance_delivery", advance)

    await question_checker.QuestionChecker().tick(now=NOW)

    advance.assert_awaited_once_with(spec, TASK_ID, claimed)


async def test_backoff_resets_when_question_disappears(
    spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qid = _question(spec)
    _answer(spec, qid)
    write_control(spec, review_reason="needs_input")
    advance = AsyncMock(return_value={"delivery": "answered"})
    monkeypatch.setattr(question_answers, "advance_delivery", advance)
    monkeypatch.setattr(question_checker, "monotonic", lambda: 0.0)
    scan = question_checker.QuestionChecker()
    path = spec / "questions" / f"{qid}.json"
    content = path.read_bytes()
    await scan.tick(now=NOW)
    path.unlink()
    await scan.tick(now=NOW)
    path.write_bytes(content)
    await scan.tick(now=NOW)

    assert advance.await_args_list == [call(spec, TASK_ID, qid)] * 2


async def test_scan_continues_after_failed_project_and_spec(
    spec: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qid = _question(spec)
    (spec.parent / "000-broken" / "questions").mkdir(parents=True)
    records = question_checker._source_records

    def scan_project(project: Path, path: Path) -> list[dict[str, object]]:
        if path.name == "000-broken":
            raise OSError
        source_records: list[dict[str, object]] = records(project, path)
        return source_records

    monkeypatch.setattr(
        question_checker,
        "load_projects",
        lambda: {"broken": {}, "p": {"path": str(tmp_path)}},
    )
    monkeypatch.setattr(question_checker, "_source_records", scan_project)
    await question_checker.QuestionChecker().tick(now=NOW)
    assert question_answers.read_record(spec, qid)["status"] == "expired"


async def test_scan_uses_source_specs_only(spec: Path, tmp_path: Path) -> None:
    worktree_spec = tmp_path / ".aifactory/worktrees/tasks/1/.aifactory/specs/001-task"
    qid = _question(worktree_spec)
    spec.parent.mkdir(parents=True)
    spec.symlink_to(worktree_spec, target_is_directory=True)
    await question_checker.QuestionChecker().tick(now=NOW)
    assert question_answers.read_record(worktree_spec, qid)["status"] == "open"


async def test_loop_survives_failed_tick_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    tick = AsyncMock(side_effect=[OSError(), None])

    async def immediate_wait(
        waiter: Coroutine[object, object, bool],
        timeout: float,  # noqa: ASYNC109
    ) -> None:
        assert timeout == 60
        if tick.await_count == 1:
            waiter.close()
            raise TimeoutError
        stop.set()
        await waiter

    monkeypatch.setattr(question_checker.QuestionChecker, "tick", tick)
    monkeypatch.setattr(asyncio, "wait_for", immediate_wait)
    await question_checker.run(stop=stop)
    assert tick.await_count == 2


@pytest.fixture
def isolated_lifespan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from server import main  # noqa: PLC0415

    settings = SimpleNamespace(
        PROJECTS_DATA_DIR=tmp_path, BACKEND_PATH=tmp_path / "absent", REDIS_URL=None
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    for name in (
        "server.main._make_non_dumpable",
        "server.main.warn_if_auth_disabled",
        "server.main.warn_if_multi_replica",
        "server.main.init_skills_service",
        "server.utils.subprocess_env.sweep_github_dirs",
        "server.services.github_app.start",
        "server.services.github_app.warn_legacy_web_login",
        "server.services.github_app.load_web_login",
        "server.observability.tracing.init_tracing",
        "server.services.build_backend.log_backend_config",
    ):
        monkeypatch.setattr(name, Mock())
    for name in (
        "services.github_app.configured",
        "services.outbox.outbox_enabled",
        "services.intake_poller.poller_enabled",
        "services.build_backend.kubejob_enabled",
        "services.job_state_store.store_enabled",
        "services.stale_reaper.reaper_enabled",
        "services.merger.merger_sweep_enabled",
    ):
        monkeypatch.setattr(f"server.{name}", lambda: False)
    monkeypatch.setattr(main, "init_db", AsyncMock())
    reconciler = SimpleNamespace(reconcile_on_startup=AsyncMock())
    monkeypatch.setattr(
        "server.services.agent_service.get_agent_service", lambda: reconciler
    )
    monkeypatch.setenv(project_settings_store.ENV, "{}")


@pytest.mark.usefixtures("isolated_lifespan")
async def test_lifespan_starts_checker_without_merger_and_cancels_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.main import lifespan  # noqa: PLC0415 - startup test only

    app = FastAPI()
    app.state.investigations_on = False
    started = asyncio.Event()
    stopped = asyncio.Event()
    waiting: list[asyncio.Task[None]] = []

    async def run(*, stop: asyncio.Event) -> None:
        started.set()
        await stop.wait()
        stopped.set()
        await asyncio.Future()

    async def expire_wait(
        task: asyncio.Task[None],
        timeout: float,  # noqa: ASYNC109
    ) -> None:
        assert timeout == 5.0
        await stopped.wait()
        waiting.append(task)
        raise TimeoutError

    monkeypatch.setattr(question_checker, "run", run)
    monkeypatch.setattr(asyncio, "wait_for", expire_wait)
    async with lifespan(app):
        await started.wait()
        assert app.state.merger_task is None
        assert not stopped.is_set()
    assert stopped.is_set()
    with pytest.raises(asyncio.CancelledError):
        await waiting[0]
