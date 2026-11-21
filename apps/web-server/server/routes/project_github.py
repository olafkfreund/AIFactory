"""Router dependency: a project route gets its org's GitHub token (#1700)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from fastapi import Request

from server.database.engine import get_db
from server.project_registry import load_projects
from server.routes.project_authz import (
    authorize_host_path,
    require_project_access,
    resolve_owner_org_id,
)
from server.services import github_app
from server.specpath import browse_roots, within_roots


async def project_github_token(request: Request) -> AsyncIterator[None]:
    """Bind the token of the org that owns the request's project; no-op without the map.

    The caller's `projectId` (path or query) selects the org only after the
    project access check, which refuses (403/404) like any project route. A
    body-only route reads `projectId` from the JSON body. A request without a
    project gets no token (a body `projectPath` never picks an org). An async
    generator on purpose: the set and the reset must run in the handler's
    context. The org is pinned: path-based wiring inside the handler cannot swap it.
    """
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
    elif request.url.path in {
        "/api/github/repos",
        "/api/github/branches",
        "/api/github/detect-repo",
        "/api/github/remote",
        "/api/github/fork-info",
    }:
        repo = request.query_params.get("repo")
        if repo:
            from server.routes.github import _get_repo_full_name  # noqa: PLC0415

            for candidate, project in load_projects().items():
                if (
                    project.get("path")
                    and repo.casefold()
                    == (
                        await asyncio.to_thread(
                            _get_repo_full_name, str(project["path"])
                        )
                    ).casefold()
                ):
                    async with contextlib.asynccontextmanager(get_db)() as db:
                        await require_project_access("viewer")._authorize(
                            candidate, request, db
                        )
                    org = project.get("org_id")
                    break
        body = {}
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            with contextlib.suppress(ValueError, UnicodeDecodeError):
                body = await request.json()
        path = (
            request.query_params.get("path")
            or request.query_params.get("project_path")
            or (body.get("projectPath") if isinstance(body, dict) else None)
        )
        if org is None:
            async with contextlib.asynccontextmanager(get_db)() as db:
                org = (
                    await authorize_host_path(
                        request,
                        db,
                        within_roots(path, browse_roots(), "browsable directories"),
                        "viewer" if request.method == "GET" else "member",
                    )
                    if path
                    else await resolve_owner_org_id(request, db)
                )
    if not github_app.multi_installation():
        yield
        return
    org_id = org if isinstance(org, str) else None
    await github_app.warm_org_token(org_id)
    with github_app.org_github_token(org_id, pinned=True):
        yield
