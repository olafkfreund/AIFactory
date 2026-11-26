"""Source question routes.

Approval uses a transport check to prevent accidental bearer and API-key calls.
Anyone holding a user's access JWT can obtain a confirm token and approve by
sending the JWT as the access_token cookie with no Authorization header.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from agents.questions import ASKED_AT_FORMAT, valid_qid
from factory_common.logsafe import sanitize_log
from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import get_settings
from server.services import question_answers
from server.specpath import safe_spec_component

from .project_authz import ProjectAccess, TaskAccessChecker
from .task_service import _resolve_task

logger = logging.getLogger(__name__)
_CONFIRM_TTL_SECONDS = 300
_TOKEN_FORMAT = re.compile(r"v1\.([0-9]{1,12})\.[A-Za-z0-9_-]{43}")


def _personal_identity(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "user", None)
    if isinstance(user, dict) and not (
        user.get("is_legacy_shared_token") or user.get("scoped_service")
    ):
        user_id = user.get("id")
        if isinstance(user_id, str) and user_id.strip():
            return user
    raise HTTPException(403, "questions require a personal identity")


class QuestionAccessChecker(TaskAccessChecker):
    async def _authorize(
        self,
        project_id: str,
        request: Request,
        db: AsyncSession,
    ) -> ProjectAccess:
        try:
            return await super()._authorize(project_id, request, db)
        except HTTPException as exc:
            if exc.status_code == status.HTTP_403_FORBIDDEN:
                exc.detail = "forbidden"
            raise


_member_access = QuestionAccessChecker("member")
Access = Annotated[ProjectAccess, Depends(_member_access)]
router = APIRouter(dependencies=[Depends(_personal_identity)])


def require_interactive_session(request: Request) -> str:
    """Bind approval to cookie transport to block bearer and API-key callers.

    A holder of the user's access JWT can obtain a confirm token and approve
    by sending it as the access_token cookie with no Authorization header.
    """
    user = _personal_identity(request)
    cookie = request.cookies.get("access_token")
    if (
        not cookie
        or "authorization" in request.headers
        or getattr(request.state, "auth_transport", None) != "cookie"
        or user.get("api_key_id") is not None
        or user.get("is_service")
    ):
        raise HTTPException(403, question_answers.APPROVAL_DETAIL)
    return hashlib.sha256(cookie.encode()).hexdigest()


def _signed_token(exp: int, user_id: str, task_id: str, qid: str, binding: str) -> str:
    secret = get_settings().JWT_SECRET
    if not secret:
        raise HTTPException(403, question_answers.APPROVAL_DETAIL)
    key = hmac.digest(secret.encode(), b"question-confirm-v1", "sha256")
    payload = json.dumps(["v1", exp, user_id, task_id, qid, binding]).encode()
    signature = base64.urlsafe_b64encode(hmac.digest(key, payload, "sha256"))
    return f"v1.{exp}.{signature.decode().rstrip('=')}"


def _verified_digest(request: Request, task_id: str, qid: str, token: object) -> str:
    binding = require_interactive_session(request)
    if isinstance(token, str) and (match := _TOKEN_FORMAT.fullmatch(token)):
        exp = int(match[1])
        if exp > time.time() and hmac.compare_digest(
            token, _signed_token(exp, request.state.user["id"], task_id, qid, binding)
        ):
            return hashlib.sha256(token.encode()).hexdigest()
    raise HTTPException(403, question_answers.APPROVAL_DETAIL)


def serialize_question(task_id: str, record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "task_id": task_id,
        "status": record["status"],
        "question": record["question"],
        "choices": record["choices"],
        "needs_approval": record["needs_approval"],
        "context": record["context"],
        "default": record["default"],
        "timeout_minutes": record["timeout_minutes"],
        "asked_at": record["asked_at"],
        "expires_at": question_answers.expires_at(record),
        "asked_by_model": record["asked_by_model"],
        "answer": record.get("answer"),
        "decision": record.get("decision"),
        "note": record.get("note"),
        "answer_id": record.get("answer_id"),
        "answered_by": record.get("answered_by"),
        "channel": record.get("channel"),
        "auth": record.get("auth"),
        "answered_at": record.get("answered_at"),
        "delivery": record.get("delivery"),
    }


def _source_spec(task_id: str, qid: str | None = None) -> Path:
    if qid is not None and not valid_qid(qid):
        raise HTTPException(404, "question not found")
    try:
        spec_id = safe_spec_component(task_id.partition(":")[2])
    except ValueError:
        raise HTTPException(404, "Task spec not found") from None
    _, _, project_dir, spec_dir = _resolve_task(task_id)
    expected = project_dir.resolve() / ".aifactory" / "specs" / spec_id
    if not spec_dir.is_dir() or spec_dir.resolve() != expected:
        raise HTTPException(404, "Task spec not found")
    return spec_dir


@router.get("/{task_id}/questions")
async def list_questions(task_id: str, _access: Access) -> dict[str, Any]:
    spec_dir = _source_spec(task_id)
    reason = question_answers.review_reason(spec_dir)
    questions: list[dict[str, Any]] = []
    if reason:
        try:
            questions = [
                serialize_question(task_id, record)
                for record in question_answers.list_records(spec_dir)
            ]
        except (OSError, ValueError):
            logger.warning("Could not list questions for %s", sanitize_log(task_id))
    return {
        "task_id": task_id,
        "review_reason": reason if questions else None,
        "questions": questions,
    }


@router.get("/{task_id}/questions/{qid}/confirm-token")
async def get_confirm_token(
    task_id: str, qid: str, request: Request, response: Response, _access: Access
) -> dict[str, str]:
    spec_dir = _source_spec(task_id, qid)
    record = question_answers.read_record(spec_dir, qid)
    binding = require_interactive_session(request)
    if not record["needs_approval"]:
        raise HTTPException(403, question_answers.APPROVAL_DETAIL)
    if record["status"] not in question_answers.ANSWERABLE:
        raise HTTPException(409, "already answered")
    exp = int(time.time()) + _CONFIRM_TTL_SECONDS
    response.headers["Cache-Control"] = "no-store"
    return {
        "confirm_token": _signed_token(
            exp, request.state.user["id"], task_id, qid, binding
        ),
        "expires_at": datetime.fromtimestamp(exp, UTC).strftime(ASKED_AT_FORMAT),
    }


@router.post("/{task_id}/questions/{qid}/answer")
async def answer_question(
    task_id: str,
    qid: str,
    request: Request,
    access: Access,
    body: Annotated[Any, Body()] = None,
) -> dict[str, Any]:
    spec_dir = _source_spec(task_id, qid)
    record = question_answers.read_record(spec_dir, qid)
    payload = body if isinstance(body, dict) else {}
    digest = (
        _verified_digest(request, task_id, qid, payload.get("confirm_token"))
        if record["needs_approval"]
        else None
    )
    user = _personal_identity(request)
    key_id = user.get("api_key_id")
    identity = question_answers.AnswerIdentity(
        user_id=user["id"],
        channel="mcp" if key_id else "portal",
        auth=f"key:{key_id}" if key_id else "session",
    )
    record = question_answers.record_answer(
        spec_dir, qid, payload, identity, token_digest=digest
    )
    if record["status"] == "answered":
        record = await question_answers.advance_delivery(
            spec_dir, task_id, qid, raw_request=request, access=access
        )
    return {"question": serialize_question(task_id, record)}
