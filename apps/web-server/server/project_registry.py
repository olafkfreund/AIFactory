"""The project registry: read/write access to the projects DB (#2157).

The rows live in ``project_store``; ``projects.json`` is only a one-off import
source and a rollback export. What follows is the original (#1317) rationale.

Extracted from ``server.routes.projects`` (#1317). These five helpers are the
single most widely-shared piece of state in the server -- routes, services,
``specpath`` and ``database.engine`` all need to answer "where does project X
live?" -- but they lived in a ROUTE module, so every one of those callers had
an edge pointing UP into the routing layer. ``routes.projects`` in turn imports
the sub-routers it mounts (``changelog``, ``context``, ``files``, ``git``,
``github``) and reads ``settings.load_app_settings``, which closed the loop:
one 62-module import cycle covering most of the package, and 102 of the repo's
126 ``py/cyclic-import`` alerts.

Every caller had already worked around it the same way -- a function-local
``from ..routes.projects import load_projects`` -- so the cycle never actually
fired at boot. That is precisely what made it dangerous: the repo's own lint
rules (PLC0415/TID252) push those imports back up to module level, and the
first person to accept that autofix would have turned a latent cycle into an
``ImportError`` at startup, in a path no unit test covers.

This module sits BELOW everything. It imports ``server.config`` and
``server.tenancy`` (both leaves) and nothing else from the package, so it can
be imported at module level from anywhere without creating an edge back.

``routes.projects`` still owns the HTTP surface, the Pydantic models, and
``project_to_response``/``analyze_project`` -- the things that genuinely belong
to a route module. Only the registry itself moved.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePath
from typing import Any

from fastapi import HTTPException

from . import project_store
from .config import get_settings


def get_projects_file() -> Path:
    """Get path to the projects data file."""
    settings = get_settings()
    return Path(settings.PROJECTS_DATA_DIR) / "projects.json"


def load_projects() -> dict[str, dict[str, Any]]:
    """Active projects from the registry DB, in the legacy ``projects.json`` shape."""
    return project_store.load_projects()


async def aload_projects() -> dict[str, dict[str, Any]]:
    """:func:`load_projects` without blocking the caller's event loop."""
    return await project_store.aload_projects()


def org_id_for_path(project_path: str | Path | None) -> str | None:
    if not project_path or not PurePath(project_path).is_absolute():
        return None
    path = PurePath(os.path.normpath(project_path))
    try:
        projects = load_projects()
    except (OSError, ValueError):
        return None
    if not isinstance(projects, dict):
        return None
    orgs: set[str | None] = set()
    for project in projects.values():
        entry = project.get("path") if isinstance(project, dict) else None
        if (
            isinstance(entry, str)
            and entry
            and PurePath(entry).is_absolute()
            and path.is_relative_to(PurePath(os.path.normpath(entry)))
        ):
            org = project.get("org_id")
            orgs.add(org if isinstance(org, str) else None)
    return next(iter(orgs)) if len(orgs) == 1 and None not in orgs else None


def overlaps_other_org(
    path: str | Path,
    org_id: str | None,
    projects: dict[str, Any],
    skip: str | None = None,
) -> bool:
    """True when ``path`` equals, holds or sits inside another org's project.

    Such an overlap makes ``org_id_for_path`` ambiguous, so neither org would
    get a GitHub token for it (#2038). Same-org overlap is harmless.
    """
    new = PurePath(os.path.normpath(path))
    for pid, project in projects.items():
        entry = project.get("path") if isinstance(project, dict) else None
        if pid == skip or not isinstance(entry, str) or not entry:
            continue
        other = PurePath(os.path.normpath(entry))
        if (new.is_relative_to(other) or other.is_relative_to(new)) and project.get(
            "org_id"
        ) != org_id:
            return True
    return False


def resolve_project_path(project_id: str) -> Path:
    """Resolve a project id to its filesystem path, or raise 404.

    Single source of truth for the ``load_projects() -> 404 -> Path(...)`` idiom
    that several route modules each re-implemented. The 404 detail is kept
    identical to those copies so callers behave exactly as before.
    """
    projects = load_projects()
    if project_id not in projects:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    return Path(projects[project_id]["path"])


def resolve_project_id(project_id: str) -> str | None:
    """Map a UUID, a project ``name``, or an ``owner/repo`` slug to the canonical
    project id — or ``None`` if nothing matches.

    External callers (notably PFactory's plan->AIFactory handoff, #321) address a
    project by the repo they planned against, not AIFactory's internal UUID. The
    project registry is keyed by UUID with ``name`` (repo) and ``path``
    (``…/owner-repo``), so match a non-UUID id against those. Exact-UUID first so
    the common path is unchanged.
    """
    projects = load_projects()
    if project_id in projects:
        return project_id
    repo = project_id.rsplit("/", 1)[-1]  # owner/repo -> repo
    path_suffix = project_id.replace("/", "-")  # owner/repo -> owner-repo
    for pid, meta in projects.items():
        name = meta.get("name", "")
        path = str(meta.get("path", ""))
        if name in (project_id, repo) or path.endswith(
            ("/" + path_suffix, "-" + path_suffix)
        ):
            return pid
    return None


def save_projects(projects: dict[str, dict[str, Any]]) -> None:
    """Upsert the entries changed since :func:`load_projects` returned them.

    Stamps ``org_id=DEFAULT_ORG_ID`` on any entry that lacks one so that
    programmatically-registered projects (cross-factory handoff, build
    dispatch, the agent ``project_create`` tool) are immediately visible in
    the org-scoped portal -- an unowned project would otherwise be admin-only
    (see ``project_authz``: ``org_id is None`` -> 403). Never removes a
    project: use ``project_store.archive_project``.
    """
    project_store.save_projects(projects)


async def asave_projects(projects: dict[str, dict[str, Any]]) -> None:
    await project_store.asave_projects(projects)
