"""Who may use a terminal session (#3740).

A session is bound to its creating user and project. The service principal
(legacy token, auth-disabled dev user) keeps full access; everyone else needs
to own the session and still hold its project. Every check fails closed.
"""

from typing import Any

from server.config import get_settings
from server.database import engine
from server.routes.project_authz import authorize_project_for_user
from server.specpath import path_in_project, project_id_for_path

__all__ = ["may_use", "path_in_project", "project_id_for_path", "project_ok"]


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
