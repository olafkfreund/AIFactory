"""Auto-Fix REST routes — backs the ``useAutoFix`` hook on the frontend.

The frontend currently calls ``window.API.github.*`` (the old Electron
IPC shim); the api-adapter at ``apps/frontend-web/src/lib/api-adapter.ts``
is being rewritten to call THESE endpoints instead.

Contract is defined by the AutoFixConfig + AutoFixQueueItem types at
``apps/frontend-web/src/shared/types/github-api.ts``.

Endpoints:
  - GET  /api/projects/{project_id}/auto-fix/config       — load config
  - PUT  /api/projects/{project_id}/auto-fix/config       — save config
  - GET  /api/projects/{project_id}/auto-fix/queue        — list queue items
  - POST /api/projects/{project_id}/auto-fix/check-new    — manual poll
  - POST /api/projects/{project_id}/auto-fix/{N}/start    — single-issue start

Returns are RAW dicts (not wrapped in ``{success, data}``) — the
frontend's api-adapter expects the schema directly per the existing
hook in ``useAutoFix.ts``.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from factory_common.logsafe import sanitize_log
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from server.error_ref import client_error

from ..services import auto_fix_service
from .project_authz import require_project_access

logger = logging.getLogger(__name__)

router = APIRouter()

Viewer = Annotated[dict, Depends(require_project_access("viewer"))]
Member = Annotated[dict, Depends(require_project_access("member"))]
Admin = Annotated[dict, Depends(require_project_access("admin"))]

# GET never returns the stored bot token; PUT treats this value (or an omitted
# field) as "keep the stored token" so a round-tripped config does not wipe it.
TOKEN_MASK = "********"


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class AutoFixConfigPayload(BaseModel):
    """Mirrors AutoFixConfig at apps/frontend-web/src/shared/types/github-api.ts:21"""

    enabled: bool = False
    labels: list[str] = Field(default_factory=list)
    requireHumanApproval: bool = False
    botToken: str | None = Field(default=None, repr=False)
    model: str = "sonnet"
    thinkingLevel: str = "none"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/{project_id}/auto-fix/config")
async def get_auto_fix_config(
    project_id: str, _access: Member
) -> dict[str, Any] | None:
    """Return AutoFixConfig for the project, or null if no project."""
    cfg = auto_fix_service.get_config(project_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail="Project not found")
    # Strip the queue from the config response (queue has its own endpoint)
    out = {k: v for k, v in cfg.items() if k != "queue"}
    if out.get("botToken"):
        out["botToken"] = TOKEN_MASK
    return out


@router.put("/{project_id}/auto-fix/config")
async def save_auto_fix_config(
    project_id: str,
    payload: AutoFixConfigPayload,
    _access: Admin,
) -> dict[str, bool]:
    """Save AutoFixConfig. Returns ``{success: bool}``."""
    config = payload.model_dump()
    if config["botToken"] in (None, TOKEN_MASK):
        config["botToken"] = (auto_fix_service.get_config(project_id) or {}).get(
            "botToken", ""
        )
    ok = auto_fix_service.save_config(project_id, config)
    if not ok:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"success": True}


@router.get("/{project_id}/auto-fix/queue")
async def get_auto_fix_queue(project_id: str, _access: Viewer) -> list[dict[str, Any]]:
    """Return the AutoFix queue items."""
    return auto_fix_service.get_queue(project_id)


@router.post("/{project_id}/auto-fix/check-new")
async def check_new_issues(project_id: str, _access: Member) -> dict[str, Any]:
    """Manually trigger a poll: find new issues + start each one.

    This is what the frontend's "Poll now" / refresh button calls,
    and also what the 5-min auto-poll loop in ``useAutoFix.ts`` calls
    when the toggle is enabled.

    The auto-poll in the frontend is per-project + only runs while the
    user has the GitHub Issues view open — there's intentionally no
    server-side cron.  This endpoint is the polling primitive both
    invocations share.
    """
    try:
        result = await auto_fix_service.check_new_and_start_all(project_id)
    except ValueError as e:
        # auto_fix_service raises InputRejectedError for "project not found",
        # returned verbatim; any other ValueError gets a reference.
        raise HTTPException(
            status_code=404, detail=client_error(logger, "Project not found", e)
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=client_error(logger, "Issue check failed", e)
        ) from e
    return result


@router.post("/{project_id}/auto-fix/{issueNumber}/start")
async def start_auto_fix_one(
    project_id: str,
    issueNumber: int,
    _access: Member,
) -> dict[str, Any]:
    """Import + start the agent on a single issue."""
    try:
        return await auto_fix_service.start_auto_fix(project_id, issueNumber)
    except ValueError as e:
        raise HTTPException(
            status_code=404, detail=client_error(logger, "Project not found", e)
        ) from e
    except Exception as e:
        logger.exception(
            "[auto_fix] start_auto_fix failed project=%s issue=%s",
            sanitize_log(project_id),
            sanitize_log(issueNumber),
        )
        raise HTTPException(
            status_code=500, detail=client_error(logger, "Auto-fix start failed", e)
        ) from e
