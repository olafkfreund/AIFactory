"""Project-authorized caller routes and run-token-authorized pod routes."""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from server.database.engine import get_db
from server.project_registry import aload_projects
from server.routes.project_authz import authorize_project_for_user
from server.services import project_settings_store
from server.services.agent_service import get_agent_service
from server.services.investigation_backend import (
    InvestigationBackend,
    InvestigationRefused,
)

router = APIRouter(prefix="/api/investigations", tags=["Investigations"])


class CreateInput(BaseModel):
    project_id: str
    sha: str
    request: str
    test_command: str | None = None
    limits: dict[str, int] | None = None


class LogInput(BaseModel):
    lines: list[str]
    tokens_used: int = 0
    turns_used: int = 0


class ReportInput(BaseModel):
    findings: str


def _backend(request: Request) -> InvestigationBackend:
    backend: InvestigationBackend = request.app.state.investigation_backend
    return backend


async def _project(
    request: Request, db: AsyncSession, project_id: str, role: str
) -> dict[str, Any]:
    projects = await aload_projects()
    project = projects.get(project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    await authorize_project_for_user(
        getattr(request.state, "user", None), project_id, db, role
    )
    return project


async def _run_project(
    request: Request, db: AsyncSession, run_id: str, role: str
) -> None:
    row = await _backend(request).store.get(run_id)
    if row is None:
        raise HTTPException(404, "Investigation not found")
    await _project(request, db, row.project_id, role)


def _refused(exc: InvestigationRefused) -> HTTPException:
    return HTTPException(404 if "not found" in str(exc) else 409, str(exc))


@router.post("")
async def create(
    body: CreateInput, request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> dict[str, str]:
    if os.environ.get("AIFACTORY_INVESTIGATIONS", "off") == "drain":
        raise HTTPException(503, "investigations are draining")
    project = await _project(request, db, body.project_id, "member")
    project_path = Path(project["path"])
    settings = await project_settings_store.get_settings(project_path)
    service = get_agent_service()
    credential_key = f"investigation:{uuid4()}"
    token, _, _ = service._resolve_claude_token_pooled(credential_key)
    try:
        result = await _backend(request).dispatch(
            project_id=body.project_id,
            repo_path=project_path,
            repo=project.get("repo") or project_path.name,
            sha=body.sha,
            request=body.request,
            claude_token=token or "",
            test_command=body.test_command,
            limits=body.limits,
            extra_patterns=settings.get("INVESTIGATE_SECRET_PATHS", ""),
        )
        request.app.state.investigation_credential_keys[result["id"]] = credential_key
        return result
    except InvestigationRefused as exc:
        service._release_task_credential(credential_key)
        raise _refused(exc) from exc
    except Exception:
        service._release_task_credential(credential_key)
        raise


@router.get("/{run_id}")
async def get(
    run_id: str, request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> dict[str, Any]:
    await _run_project(request, db, run_id, "viewer")
    return await _backend(request).get(run_id)


@router.get("/{run_id}/logs")
async def logs(
    run_id: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    after: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    await _run_project(request, db, run_id, "viewer")
    return await _backend(request).logs(run_id, after, limit)


@router.post("/{run_id}/lease")
async def lease(
    run_id: str, request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> dict[str, bool]:
    await _run_project(request, db, run_id, "member")
    try:
        await _backend(request).renew_lease(run_id)
    except InvestigationRefused as exc:
        raise _refused(exc) from exc
    return {"ok": True}


@router.post("/{run_id}/stop")
async def stop(
    run_id: str, request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> dict[str, Any]:
    await _run_project(request, db, run_id, "member")
    try:
        return await _backend(request).stop(run_id)
    except InvestigationRefused as exc:
        raise _refused(exc) from exc


async def _pod_auth(
    run_id: str,
    request: Request,
    x_run_token: Annotated[str | None, Header()] = None,
) -> None:
    if not x_run_token:
        raise HTTPException(401, "Run token required")
    row = await _backend(request).store.get(run_id)
    digest = hashlib.sha256(x_run_token.encode()).hexdigest()
    if row is None or row.pods_gone or not hmac.compare_digest(digest, row.token_hash):
        raise HTTPException(403, "Run token refused")


_PodAuth = Annotated[None, Depends(_pod_auth)]


@router.post("/{run_id}/pod/log")
async def pod_log(
    run_id: str, body: LogInput, request: Request, _auth: _PodAuth
) -> dict[str, bool]:
    try:
        await _backend(request).append_log(
            run_id, body.lines, tokens_used=body.tokens_used, turns_used=body.turns_used
        )
    except InvestigationRefused as exc:
        raise _refused(exc) from exc
    return {"ok": True}


@router.post("/{run_id}/pod/run-tests")
async def pod_run_tests(
    run_id: str, request: Request, _auth: _PodAuth
) -> dict[str, Any]:
    try:
        return await _backend(request).run_tests(run_id)
    except InvestigationRefused as exc:
        raise _refused(exc) from exc


@router.post("/{run_id}/pod/report")
async def pod_report(
    run_id: str, body: ReportInput, request: Request, _auth: _PodAuth
) -> dict[str, bool]:
    try:
        await _backend(request).put_report(run_id, body.findings)
    except InvestigationRefused as exc:
        raise _refused(exc) from exc
    return {"ok": True}


@router.get("/{run_id}/pod/lease")
async def pod_lease(run_id: str, request: Request, _auth: _PodAuth) -> dict[str, Any]:
    return await _backend(request).lease_status(run_id)


@router.get("/{run_id}/pod/config")
async def pod_config(run_id: str, request: Request, _auth: _PodAuth) -> dict[str, Any]:
    try:
        return await _backend(request).config(run_id)
    except InvestigationRefused as exc:
        raise _refused(exc) from exc
