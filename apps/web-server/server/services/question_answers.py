"""Record answers and resume paused tasks from their source spec directory."""

from __future__ import annotations

import fcntl
import hmac
import json
import logging
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeGuard

from agents.questions import (
    ASKED_AT_FORMAT,
    QUESTION_FIELDS,
    RUN_ID_RE,
    _atomic_json,
    _lock,
    read_question,
    valid_qid,
    validate_question,
)
from factory_common.logsafe import sanitize_log
from fastapi import HTTPException, Request

from . import inbox_service, task_control

if TYPE_CHECKING:
    from collections.abc import Iterator

    from server.routes.project_authz import ProjectAccess

logger = logging.getLogger(__name__)
ANSWERABLE = ("open", "expired")
APPROVAL_DETAIL = "approval requires a person in the portal"
CLAIM_GRACE_SECONDS = 300
ANSWER_LIMIT = 4000
NOTE_LIMIT = 2000
ANSWER_ID = re.compile(r"a-[0-9a-f]{12}")
ANSWER_TEXT_FIELDS = (
    "answer",
    "decision",
    "note",
    "answer_id",
    "answered_by",
    "channel",
    "auth",
    "answered_at",
)


@dataclass(frozen=True)
class AnswerIdentity:
    user_id: str
    channel: Literal["portal", "mcp", "cfactory"]
    auth: str


def review_reason(spec_dir: Path) -> str | None:
    control = task_control.read_control(spec_dir)
    reason = control.get("reviewReason")
    if "_migratedFromPlan" not in control and reason in (
        "needs_input",
        "approval_rejected",
    ):
        return str(reason)
    return None


def read_record(spec_dir: Path, qid: str) -> dict[str, Any]:
    """Validate the stored question before using its approval flag or path."""
    if not valid_qid(qid):
        raise HTTPException(404, "question not found")
    directory = spec_dir / "questions"
    path = directory / f"{qid}.json"
    try:
        if directory.is_symlink() or path.is_symlink() or not path.is_file():
            raise ValueError("invalid question path")
        record = read_question(spec_dir, qid)
        if (
            not isinstance(record, dict)
            or record.get("id") != qid
            or record.get("status") not in (*ANSWERABLE, "answered", "rejected")
        ):
            raise ValueError("invalid question identity or status")
        core = {key: record[key] for key in QUESTION_FIELDS}
        validate_question({**core, "status": "open"})
        _validate_answer_record(record)
        expires_at(record)
        return record
    except (OSError, ValueError, KeyError, OverflowError, RecursionError):
        logger.debug("Question unavailable at %s", sanitize_log(path))
        raise HTTPException(404, "question not found") from None


def _validate_answer_record(record: dict[str, Any]) -> None:
    deliveries = (
        ("answered", "inboxed", "launched")
        if record["status"] == "answered"
        else (None,)
    )
    if record.get("delivery") not in deliveries:
        raise ValueError("invalid delivery state")
    for key in ANSWER_TEXT_FIELDS:
        value = record.get(key)
        if value is not None:
            if not isinstance(value, str):
                raise ValueError("invalid answer field")
            value.encode("utf-8")
    for text in (
        record["question"],
        record["context"],
        record["asked_by_model"],
        record["default"] or "",
        *(record["choices"] or []),
    ):
        text.encode("utf-8")


def list_records(spec_dir: Path, *, strict: bool = False) -> Iterator[dict[str, Any]]:
    directory = spec_dir / "questions"
    if directory.is_symlink():
        raise ValueError("invalid questions directory")
    try:
        paths = sorted(directory.iterdir())
    except FileNotFoundError:
        return
    for path in paths:
        if path.suffix != ".json" or not valid_qid(path.stem):
            continue
        try:
            yield read_record(spec_dir, path.stem)
        except HTTPException:
            if strict:
                raise
            logger.warning("Skipping unreadable question record %s", sanitize_log(path))


def _review_state(spec_dir: Path, *, reason: str | None) -> None:
    review_file = spec_dir / "review_state.json"
    if review_file.is_symlink():
        raise ValueError("invalid source review state path")
    try:
        review = json.loads(review_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    if not isinstance(review, dict):
        return
    needs_input = review.get("reason") == "needs_input"
    if reason is None and (needs_input or "qid" in review):
        if needs_input:
            review.pop("reason")
        review.pop("qid", None)
    elif reason is not None and needs_input:
        review["reason"] = reason
    else:
        return
    _atomic_json(review_file, review)


def _answer_fields(
    record: dict[str, Any], body: dict[str, Any], token_digest: str | None
) -> dict[str, Any]:
    invalid = HTTPException(422, "answer must be one of the choices")
    if record["needs_approval"]:
        used_digest = record.get("_confirm_token_digest")
        if token_digest is None or (
            isinstance(used_digest, str)
            and hmac.compare_digest(used_digest, token_digest)
        ):
            raise HTTPException(403, APPROVAL_DETAIL)
        if record["status"] not in ANSWERABLE:
            raise HTTPException(409, "already answered")
        decision, note = body.get("decision"), body.get("note")
        if (
            "answer" in body
            or decision not in ("approve", "reject")
            or (note is not None and not _valid_text(note, NOTE_LIMIT))
        ):
            raise invalid
        return {
            "decision": decision,
            "note": note,
            "_confirm_token_digest": token_digest,
        }

    answer, choices = body.get("answer"), record["choices"]
    if (
        "decision" in body
        or not _valid_text(answer, ANSWER_LIMIT)
        or not answer.strip()
        or (choices is not None and answer not in choices)
    ):
        raise invalid
    return {"answer": answer}


def _valid_text(value: object, maximum: int) -> TypeGuard[str]:
    if not isinstance(value, str) or len(value) > maximum:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def record_answer(
    spec_dir: Path,
    qid: str,
    body: dict[str, Any],
    identity: AnswerIdentity,
    *,
    token_digest: str | None = None,
) -> dict[str, Any]:
    """Apply an answer once. REST verifies approval tokens before passing a digest."""
    if not identity.user_id.strip():
        raise HTTPException(403, "questions require a personal identity")
    read_record(spec_dir, qid)
    with _lock(spec_dir) as directory:
        record = read_record(spec_dir, qid)
        if not record["needs_approval"] and record["status"] not in ANSWERABLE:
            raise HTTPException(409, "already answered")
        fields = _answer_fields(record, body, token_digest)
        rejected = fields.get("decision") == "reject"
        record.update(
            fields,
            status="rejected" if rejected else "answered",
            delivery=None if rejected else "answered",
            answer_id=f"a-{uuid.uuid4().hex[:12]}",
            answered_by=identity.user_id,
            channel=identity.channel,
            auth=identity.auth,
            answered_at=datetime.now(UTC).strftime(ASKED_AT_FORMAT),
        )
        if rejected:
            task_control.write_control(
                spec_dir,
                status="human_review",
                review_reason="approval_rejected",
                updated_by=identity.user_id,
            )
            _review_state(spec_dir, reason="approval_rejected")
        _atomic_json(directory / f"{qid}.json", record)
        return record


def expires_at(record: dict[str, Any]) -> str:
    asked = datetime.strptime(record["asked_at"], ASKED_AT_FORMAT).replace(tzinfo=UTC)
    return (asked + timedelta(minutes=record["timeout_minutes"])).strftime(
        ASKED_AT_FORMAT
    )


def _save(directory: Path, record: dict[str, Any]) -> None:
    _atomic_json(directory / f"{record['id']}.json", record)


def _inbox(directory: Path, record: dict[str, Any]) -> None:
    answer_id = record["answer_id"]
    if not isinstance(answer_id, str) or ANSWER_ID.fullmatch(answer_id) is None:
        raise ValueError("invalid answer id")
    inbox_path = inbox_service.get_inbox_path(directory.parent)
    if inbox_path.parent.is_symlink() or inbox_path.is_symlink():
        raise ValueError("invalid source inbox path")
    if record["needs_approval"]:
        reply = f"Decision: {record['decision']}\nNote: {record.get('note') or ''}"
    else:
        reply = f"Answer: {record['answer']}"
    inbox_service.enqueue(
        directory.parent,
        text=f"Question {record['id']}: {record['question']}\n\n{reply}\n\nEnd of answer.",
        sender=record["answered_by"],
        question_answer=(record["id"], answer_id),
    )
    record["delivery"] = "inboxed"
    _save(directory, record)


@contextmanager
def _launch_lock(spec_dir: Path) -> Iterator[bool]:
    # Keep the lease exclusive across async admission without blocking another request.
    with (spec_dir / "questions" / ".launch.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def _attempt(spec_dir: Path) -> str | None:
    path = spec_dir / ".question_attempt"
    if path.is_symlink():
        raise ValueError("invalid question attempt path")
    try:
        attempt = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    if not RUN_ID_RE.fullmatch(attempt):
        raise ValueError("invalid question attempt")
    return attempt


async def _admitted(task_id: str, spec_dir: Path, claim: dict[str, Any]) -> bool:
    from server.services.agent_service import get_agent_service  # noqa: PLC0415

    agent = get_agent_service()
    attempt = _attempt(spec_dir)
    changed = attempt != claim.get("_launch_previous_attempt")
    if changed and agent.is_running(task_id):
        return True
    if any(queued.task_id == task_id for queued in agent._task_queue):
        return True
    if agent._store_enabled:
        state = await agent._store().get_state(task_id)
        if (
            state
            and state["updated_at"].timestamp() >= claim["_launch_claimed_at"]
            and (
                state["lifecycle_state"] == "queued"
                or (changed and state["worker_ref"].get("kind") == "k8s-job")
            )
        ):
            return True
    return changed and any(
        message.get("answer_id") == claim["answer_id"] and message.get("read") is True
        for message in inbox_service.list_messages(spec_dir)
    )


async def _start(
    task_id: str,
    identity: str,
    raw_request: Request | None,
    access: ProjectAccess | None,
) -> dict[str, Any]:
    from server.routes import execution  # noqa: PLC0415 - use the live start handler

    if raw_request is None:
        raw_request = Request(
            {"type": "http", "headers": [], "state": {"user": {"id": identity}}}
        )
    result = await execution.start_task(
        task_id, execution.StartTaskRequest(), raw_request, access
    )
    return result if isinstance(result, dict) else {}


def _prepare_delivery(spec_dir: Path, qid: str) -> list[dict[str, Any]]:
    with _lock(spec_dir) as directory:
        records = list(list_records(spec_dir, strict=True))
        target = read_record(spec_dir, qid)
        if target["status"] != "answered" or target.get("delivery") == "launched":
            return []
        for record in records:
            if record["status"] == "answered" and record.get("delivery") == "answered":
                _inbox(directory, record)
        batch = [
            record
            for record in records
            if record["status"] == "answered" and record.get("delivery") == "inboxed"
        ]
        claim = next(
            (record for record in batch if "_launch_claimed_at" in record), None
        )
        if claim is not None:
            answer_ids = claim.get("_launch_answer_ids", [claim["answer_id"]])
            return [record for record in batch if record["answer_id"] in answer_ids]
        return [] if any(_blocks_launch(record) for record in records) else batch


def _blocks_launch(record: dict[str, Any]) -> bool:
    return record["status"] in ANSWERABLE or (
        record["status"] == "rejected" and not record.get("closed_at")
    )


def _finish_delivery(
    spec_dir: Path, claim: dict[str, Any], batch: list[dict[str, Any]]
) -> None:
    with _lock(spec_dir) as directory:
        claim["_launch_admitted_at"] = datetime.now(UTC).strftime(ASKED_AT_FORMAT)
        attempt = _attempt(spec_dir)
        if attempt != claim["_launch_previous_attempt"]:
            claim["_launch_attempt_id"] = attempt
        _save(directory, claim)
        current = task_control.read_control(spec_dir)
        batch_ids = {record["id"] for record in batch}
        if not any(
            _blocks_launch(record)
            or (
                record["id"] not in batch_ids
                and record.get("delivery") in ("answered", "inboxed")
            )
            for record in list_records(spec_dir, strict=True)
        ):
            _review_state(spec_dir, reason=None)
            if review_reason(spec_dir) == "needs_input":
                task_control.write_control(
                    spec_dir,
                    status="in_progress"
                    if current.get("status") == "human_review"
                    else None,
                    clear_review_reason=True,
                    updated_by=claim["answered_by"],
                )
        # Keep the admission receipt retryable until the rest of its batch is written.
        for record in [*(r for r in batch if r is not claim), claim]:
            record["delivery"] = "launched"
            record["_launch_answer_id"] = claim["answer_id"]
            _save(directory, record)


def _retire_launched_answers(spec_dir: Path, records: list[dict[str, Any]]) -> None:
    inbox_path = inbox_service.get_inbox_path(spec_dir)
    if (
        inbox_path.parent.is_symlink()
        or inbox_path.is_symlink()
        or not inbox_path.is_file()
    ):
        raise ValueError("invalid source inbox path")
    messages = inbox_service.list_messages(spec_dir)
    if not messages:
        raise ValueError("missing source inbox messages")
    # A later confirmed pause means the runner already drained the earlier answers.
    launched = {
        record["answer_id"]
        for record in records
        if record.get("delivery") == "launched"
    }
    if any(not isinstance(answer_id, str) for answer_id in launched):
        raise ValueError("invalid launched answer id")
    inbox_service.mark_read(
        spec_dir,
        [
            message["messageId"]
            for message in messages
            if message.get("answer_id") in launched and message.get("read") is not True
        ],
    )


async def _admit_delivery(
    spec_dir: Path,
    task_id: str,
    batch: list[dict[str, Any]],
    raw_request: Request | None,
    access: ProjectAccess | None,
) -> None:
    claim = next((r for r in batch if "_launch_claimed_at" in r), batch[0])
    if "_launch_admitted_at" in claim:
        _finish_delivery(spec_dir, claim, batch)
        return
    previous_claim = claim.get("_launch_claimed_at")
    if previous_claim is not None:
        if await _admitted(task_id, spec_dir, claim):
            _finish_delivery(spec_dir, claim, batch)
            return
        if (
            time.time() - previous_claim < CLAIM_GRACE_SECONDS
            or _attempt(spec_dir) != claim["_launch_previous_attempt"]
        ):
            # The attempt is stamped before spawn. Wait for proof before replaying it.
            return
    with _lock(spec_dir) as directory:
        if review_reason(spec_dir) != "needs_input":
            return
        records = list(list_records(spec_dir, strict=True))
        batch_ids = {record["id"]: record["answer_id"] for record in batch}
        fresh_batch = {
            record["id"]: record
            for record in records
            if record["id"] in batch_ids
            and record["status"] == "answered"
            and record.get("delivery") == "inboxed"
        }
        current_answers = {
            qid: record["answer_id"] for qid, record in fresh_batch.items()
        }
        if current_answers != batch_ids or any(
            _blocks_launch(record)
            or (
                record["id"] not in batch_ids
                and record["status"] == "answered"
                and record.get("delivery") == "answered"
            )
            for record in records
        ):
            return
        claim = fresh_batch[claim["id"]]
        batch = [fresh_batch[qid] for qid in batch_ids]
        _retire_launched_answers(spec_dir, records)
        claim["_launch_claimed_at"] = time.time()
        claim["_launch_previous_attempt"] = _attempt(spec_dir)
        claim["_launch_answer_ids"] = [record["answer_id"] for record in batch]
        _save(directory, claim)
    try:
        result = await _start(task_id, claim["answered_by"], raw_request, access)
    except Exception:  # noqa: BLE001 - durable answers survive every start failure
        logger.warning("Question resume failed for %s", sanitize_log(task_id))
        result = {}
    if result.get("success") and (
        result.get("status") == "queued"
        or result.get("message") == "Task execution started"
    ):
        _finish_delivery(spec_dir, claim, batch)
    elif _attempt(spec_dir) == claim["_launch_previous_attempt"]:
        with _lock(spec_dir) as directory:
            if result.get("reviewReason") == "plan_review":
                _restore_pause(spec_dir)
            claim.pop("_launch_claimed_at")
            claim.pop("_launch_previous_attempt")
            claim.pop("_launch_answer_ids")
            _save(directory, claim)


def _restore_pause(spec_dir: Path) -> None:
    control = task_control.read_control(spec_dir)
    if (
        control.get("reviewReason") == "plan_review"
        and control.get("updatedBy") == "web_server"
    ):
        task_control.write_control(
            spec_dir, status="human_review", review_reason="needs_input"
        )


async def advance_delivery(
    spec_dir: Path,
    task_id: str,
    qid: str,
    *,
    raw_request: Request | None = None,
    access: ProjectAccess | None = None,
) -> dict[str, Any]:
    """Advance a durable answer from REST or from the retry checker."""
    target = read_record(spec_dir, qid)
    if target.get("delivery") not in ("answered", "inboxed"):
        return target
    try:
        with _launch_lock(spec_dir) as acquired:
            if acquired:
                batch = _prepare_delivery(spec_dir, qid)
                if batch:
                    await _admit_delivery(spec_dir, task_id, batch, raw_request, access)
    except Exception:  # noqa: BLE001 - leave the last durable state for retry
        logger.warning("Question delivery deferred for %s", sanitize_log(qid))
    return read_record(spec_dir, qid)
