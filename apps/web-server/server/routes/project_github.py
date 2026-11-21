"""Router dependency: a project route gets its org's GitHub token (#1700)."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from fastapi import Request

from server.database.engine import get_db
from server.services import github_app


async def project_github_token(request: Request) -> AsyncIterator[None]:
    """Bind the token of the org that owns the request's project; no-op without the map.

    The caller's `projectId` (path or query) selects the org only after the
    project access check, which refuses (403/404) like any project route. A
    body-only route reads `projectId` from the JSON body. A request without a
    project gets no token (a body `projectPath` never picks an org). An async
    generator on purpose: the set and the reset must run in the handler's
    context. The org is pinned: path-based wiring inside the handler cannot swap it.
    """
    from server.project_registry import load_projects  # noqa: PLC0415
    from server.routes.project_authz import require_project_access  # noqa: PLC0415

    org: object = None
    pid = request.path_params.get("projectId") or request.query_params.get("projectId")
    if not pid and request.method not in ("GET", "HEAD", "OPTIONS"):
        # Body-only routes (`/persist-token`) name the project in the JSON body (#2053).
        # The body is cached on the request, so the handler still reads it.
        with contextlib.suppress(ValueError, UnicodeDecodeError):
            body = await request.json()
            if isinstance(body, dict) and isinstance(body.get("projectId"), str):
                pid = body["projectId"]
    if pid:
        async with contextlib.asynccontextmanager(get_db)() as db:
            # A write (merge, release, import) needs the member role.
            role = (
                "viewer" if request.method in ("GET", "HEAD", "OPTIONS") else "member"
            )
            await require_project_access(role)._authorize(pid, request, db)
        org = (load_projects().get(pid) or {}).get("org_id")
    if not github_app.multi_installation():
        yield
        return
    org_id = org if isinstance(org, str) else None
    await github_app.warm_org_token(org_id)
    with github_app.org_github_token(org_id, pinned=True):
        yield
