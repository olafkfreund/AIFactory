"""The project registry: read/write access to ``projects.json``.

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

import json
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from .config import get_settings
from .tenancy import DEFAULT_ORG_ID


def get_projects_file() -> Path:
    """Get path to the projects data file."""
    settings = get_settings()
    return Path(settings.PROJECTS_DATA_DIR) / "projects.json"


def load_projects() -> dict[str, dict[str, Any]]:
    """Load projects from disk."""
    projects_file = get_projects_file()
    if projects_file.exists():
        loaded: dict[str, dict[str, Any]] = json.loads(projects_file.read_text())
        return loaded
    return {}


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
    """Save projects to disk.

    Stamps ``org_id=DEFAULT_ORG_ID`` on any entry that lacks one so that
    programmatically-registered projects (cross-factory handoff, build
    dispatch, the agent ``project_create`` tool) are immediately visible in
    the org-scoped portal — not just after the next startup backfill
    (``database.engine._backfill_project_orgs``). The portal create path sets
    ``org_id`` explicitly before saving, so this never overrides a real value;
    it only defaults the unowned case (single-tenant deployments own the
    "default" org). An unowned project would otherwise be admin-only / hidden
    (see ``project_authz`` — ``org_id is None`` → 403).
    """
    for proj in projects.values():
        if isinstance(proj, dict) and not proj.get("org_id"):
            proj["org_id"] = DEFAULT_ORG_ID
    projects_file = get_projects_file()
    projects_file.parent.mkdir(parents=True, exist_ok=True)
    projects_file.write_text(json.dumps(projects, indent=2))
