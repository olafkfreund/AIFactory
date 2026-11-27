"""Expire unanswered questions and retry durable answers in source specs."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

from server.project_registry import load_projects
from server.routes.task_service import get_spec_dirs
from server.specpath import spec_dir_for

from . import question_answers

logger = logging.getLogger(__name__)
INTERVAL = 60
MAX_RETRY_DELAY = 15 * 60


def _notify_expired() -> None:
    logger.debug("A question expired")


def _source_records(project_path: Path, spec_dir: Path) -> list[dict[str, Any]]:
    expected = spec_dir_for(project_path.resolve(), spec_dir.name)
    if spec_dir.resolve() != expected:
        raise ValueError("invalid source spec directory")
    return list(question_answers.list_records(spec_dir))


def _pending(record: dict[str, Any]) -> bool:
    return record["status"] == "answered" and record.get("delivery") in (
        "answered",
        "inboxed",
    )


def _can_retry(spec_dir: Path, qid: str) -> bool:
    record = question_answers.read_record(spec_dir, qid)
    return _pending(record) and (
        "_launch_claimed_at" in record
        or question_answers.review_reason(spec_dir) == "needs_input"
    )


class QuestionChecker:
    def __init__(self) -> None:
        self._retries: dict[tuple[str, str], tuple[float, int]] = {}

    async def _retry(self, spec_dir: Path, task_id: str, qid: str) -> None:
        key = task_id, qid
        due, delay = self._retries.get(key, (float("-inf"), INTERVAL))
        if monotonic() < due or not await asyncio.to_thread(_can_retry, spec_dir, qid):
            return
        self._retries[key] = (monotonic() + delay, min(delay * 2, MAX_RETRY_DELAY))
        record = await question_answers.advance_delivery(spec_dir, task_id, qid)
        if record.get("delivery") == "launched":
            self._retries.pop(key, None)

    async def _check_spec(
        self, project_path: Path, spec_dir: Path, task_id: str, now: datetime
    ) -> None:
        records = await asyncio.to_thread(_source_records, project_path, spec_dir)
        pending = {record["id"] for record in records if _pending(record)}
        for key in tuple(self._retries):
            if key[0] == task_id and key[1] not in pending:
                del self._retries[key]
        for record in records:
            qid = record["id"]
            try:
                if qid in pending:
                    await self._retry(spec_dir, task_id, qid)
                elif record["status"] == "open" and await asyncio.to_thread(
                    question_answers.expire_open, spec_dir, qid, now
                ):
                    _notify_expired()
            except Exception:  # noqa: BLE001 - isolate question and notice failures
                logger.warning("Question checker could not process a question")

    async def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        projects = await asyncio.to_thread(load_projects)
        active_tasks: set[str] = set()
        for project_id, project in projects.items():
            try:
                project_path = Path(project["path"])
                spec_dirs = await asyncio.to_thread(get_spec_dirs, project_path)
            except Exception:  # noqa: BLE001 - one project must not hide the rest
                logger.warning("Question checker could not scan a project")
                active_tasks.update(
                    task_id
                    for task_id, _ in self._retries
                    if task_id.partition(":")[0] == project_id
                )
                continue
            for spec_dir in spec_dirs:
                task_id = f"{project_id}:{spec_dir.name}"
                active_tasks.add(task_id)
                try:
                    await self._check_spec(project_path, spec_dir, task_id, now)
                except Exception:  # noqa: BLE001 - one spec must not hide the rest
                    logger.warning("Question checker could not scan a spec")
        for key in tuple(self._retries):
            if key[0] not in active_tasks:
                del self._retries[key]


async def run(*, stop: asyncio.Event) -> None:
    checker = QuestionChecker()
    while not stop.is_set():
        try:
            await checker.tick()
        except Exception:  # noqa: BLE001 - a failed tick must not stop the checker
            logger.warning("Question checker tick failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=INTERVAL)
        except TimeoutError:
            continue
