"""Question notices run off-thread and never carry task data."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread, get_ident
from types import SimpleNamespace
from unittest.mock import Mock, call

import httpx
import pytest
from agents.questions import read_question, write_question
from server.services import bus_notify, question_checker, question_pause

TASK_ID = "project:001-task"


@pytest.fixture
def threads(monkeypatch: pytest.MonkeyPatch) -> list[Thread]:
    started: list[Thread] = []

    def make_thread(*, target: Callable[[], None], daemon: bool) -> Thread:
        thread = Thread(target=target, daemon=daemon)
        started.append(thread)
        return thread

    monkeypatch.setattr(
        question_pause, "threading", SimpleNamespace(Thread=make_thread)
    )
    return started


@pytest.fixture
def spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(
        question_checker, "load_projects", lambda: {"project": {"path": str(tmp_path)}}
    )
    return tmp_path / ".aifactory" / "specs" / "001-task"


def _question(spec: Path) -> str:
    record = write_question(
        spec, question="Which timezone?", run_id="0" * 32, asked_by_model="test"
    )
    return str(record["id"])


def test_open_returns_while_notice_is_blocked(
    monkeypatch: pytest.MonkeyPatch, threads: list[Thread]
) -> None:
    entered, release = Event(), Event()

    def block() -> None:
        entered.set()
        release.wait(timeout=2)

    notice = Mock(side_effect=block)
    monkeypatch.setattr(bus_notify, "notify_question_waiting", notice)

    try:
        question_pause.on_question_open(TASK_ID)
        assert entered.wait(timeout=1)
        assert len(threads) == 1
        assert threads[0].daemon and threads[0].is_alive()
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=1)
    notice.assert_called_once_with()


def test_open_isolates_raising_notifier(
    monkeypatch: pytest.MonkeyPatch,
    threads: list[Thread],
    caplog: pytest.LogCaptureFixture,
) -> None:
    notice = Mock(side_effect=RuntimeError("private detail"))
    monkeypatch.setattr(bus_notify, "notify_question_waiting", notice)

    question_pause.on_question_open(TASK_ID)
    threads[0].join(timeout=1)

    notice.assert_called_once_with()
    assert caplog.messages == ["Question bus notice failed (RuntimeError)"]


def test_open_isolates_thread_start_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    start = Mock(side_effect=RuntimeError("private detail"))
    thread = Mock(return_value=SimpleNamespace(start=start))
    monkeypatch.setattr(question_pause, "threading", SimpleNamespace(Thread=thread))
    notice = Mock()
    monkeypatch.setattr(bus_notify, "notify_question_waiting", notice)

    question_pause.on_question_open(TASK_ID)

    start.assert_called_once_with()
    notice.assert_not_called()
    assert caplog.messages == ["Question bus notice failed (RuntimeError)"]


@pytest.mark.parametrize("fails", [False, True])
async def test_tick_notifies_once_per_expiry_off_the_event_loop(
    spec: Path, monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    qids = [_question(spec) for _ in range(2 if fails else 1)]
    notice_threads: list[int] = []

    def notify() -> None:
        notice_threads.append(get_ident())
        if fails:
            raise RuntimeError("private detail")

    notice = Mock(side_effect=notify)
    monkeypatch.setattr(bus_notify, "notify_question_waiting", notice)
    checker = question_checker.QuestionChecker()
    now = datetime.max.replace(tzinfo=UTC)

    await checker.tick(now=now)
    await checker.tick(now=now)

    assert notice.call_args_list == [call()] * len(qids)
    assert get_ident() not in notice_threads
    assert all(read_question(spec, qid)["status"] == "expired" for qid in qids)


async def test_unconfigured_bus_makes_no_http_request_from_either_hook(
    spec: Path, monkeypatch: pytest.MonkeyPatch, threads: list[Thread]
) -> None:
    for key in (
        "AIFACTORY_BUS_HOMESERVER",
        "AIFACTORY_BUS_ROOM",
        "AIFACTORY_BUS_TOKEN_FILE",
        "AIFACTORY_PORTAL_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    put = Mock(side_effect=AssertionError("unexpected HTTP request"))
    monkeypatch.setattr(httpx, "put", put)
    notice = Mock(wraps=bus_notify.notify_question_waiting)
    monkeypatch.setattr(bus_notify, "notify_question_waiting", notice)
    _question(spec)

    question_pause.on_question_open(TASK_ID)
    await asyncio.to_thread(threads[0].join, 1)
    assert not threads[0].is_alive()
    notice.assert_called_once_with()

    await question_checker.QuestionChecker().tick(now=datetime.max.replace(tzinfo=UTC))

    assert notice.call_args_list == [call(), call()]
    put.assert_not_called()
