"""Who may use a terminal session (#3740).

A session is bound to its creating user and project. The service principal
(legacy token, auth-disabled dev user) keeps full access; everyone else needs
to own the session and still hold its project. Every check fails closed.
"""

from pathlib import Path, PurePath
from typing import Any

from server import project_registry
from server.config import get_settings
from server.database import engine
from server.routes.project_authz import authorize_project_for_user


async def project_ok(
    user: dict[str, Any] | None, project_id: str | None, role: str = "viewer"
) -> bool:
    """Strict project authorization; denial or lookup failure is False."""
    try:
        async with engine.async_session_factory() as db:
            await authorize_project_for_user(user, project_id, db, role)
    except Exception:  # noqa: BLE001 - denial or lookup failure fails closed
        return False
    return True


async def may_use(
    user: dict[str, Any] | None, session: Any, role: str = "viewer"
) -> bool:
    if (
        user
        and user.get("id") == "default"
        and user.get("is_service")
        and get_settings().DISABLE_AUTH
    ) or (
        user
        and user.get("is_legacy_shared_token")
        and get_settings().LEGACY_SHARED_TOKEN_FULL_ACCESS
    ):
        return True
    if not isinstance(user, dict) or (
        not user.get("id") and not user.get("scoped_service")
    ):
        return False
    if session.owner_id is not None and user.get("id") != session.owner_id:
        return False
    if session.owner_id is None:
        role = "admin"
    # Old project-less sessions are readable by their owner, but cannot open a shell.
    return (
        role == "viewer"
        if session.project_id is None
        else await project_ok(user, session.project_id, role)
    )


def _roots(project: object) -> list[PurePath]:
    """Normalised and resolved forms of a registered project's path, from trusted data."""
    # A repo-only project stores "" (no clone); Path("") would be the CWD.
    if not isinstance(project, dict) or not project.get("path"):
        return []
    raw = str(project["path"])
    try:
        return [Path(raw).resolve()]
    except (OSError, RuntimeError, ValueError):
        return []


def _inside(target: PurePath, root: PurePath) -> bool:
    return target == root or root in target.parents


def project_id_for_path(path: str | None) -> str | None:
    """Registered project id for a project id or a path inside a project."""
    if not path:
        return None
    projects = project_registry.load_projects()
    if path in projects:
        return path
    if not PurePath(path).is_absolute():
        return None
    try:
        target = Path(path).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    best: tuple[int, str] | None = None
    for project_id, project in projects.items():
        for root in _roots(project):
            if _inside(target, root) and (best is None or len(root.parts) > best[0]):
                best = (len(root.parts), project_id)
    return best[1] if best else None


def path_in_project(path: str, project_id: str) -> bool:
    if not PurePath(path).is_absolute():
        return False
    try:
        target = Path(path).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return any(
        _inside(target, root)
        for root in _roots(project_registry.load_projects().get(project_id))
    )
