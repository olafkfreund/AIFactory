"""Project authorization from team membership and token scope ceilings."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server import project_registry
from server.config import get_settings
from server.database.engine import get_db
from server.database.models import OrgMember, Team, TeamMembership
from server.utils.roles import ROLE_LEVELS

__all__ = [
    "ProjectAccessChecker",
    "require_project_access",
    "is_service_principal",
    "authorize_project_for_user",
    "require_project_role",
    "accessible_project_ids",
    "scope_ceiling",
]


@dataclass(frozen=True)
class ProjectAccess:
    project_id: str
    org_id: str
    team_id: str | None
    role: str


def scope_ceiling(scopes: object) -> str | None:
    """Highest project role permitted by a token's scopes."""
    if scopes is None:
        return "owner"
    if not isinstance(scopes, (list, set, tuple, frozenset)):
        return None
    scopes = set(scopes)
    if scopes & {"task:merge", "project:admin", "*:admin", "admin"}:
        return "admin"
    if scopes & {"project:write", "task:write", "plan:write", "mcp:write", "api"}:
        return "member"
    if scopes & {"mcp:read", "project:read", "task:read", "*:read"}:
        return "viewer"
    return None


def _auth_disabled() -> bool:
    """Whether global auth is off (dev mode). Mirrors TokenAuthMiddleware so the
    authz dependency also no-ops under DISABLE_AUTH — including in tests/apps
    that mount routers without the middleware."""
    try:
        from ..config import get_settings

        return bool(get_settings().DISABLE_AUTH)
    except Exception:
        return False


def is_service_principal(user: dict | None) -> bool:
    """Legacy identity predicate for callers migrating to project grants."""
    if not isinstance(user, dict):
        return False
    return bool(user.get("is_service"))


async def _team_grant(
    principal: dict[str, object], project: dict[str, object], db: AsyncSession
) -> tuple[str | None, str | None]:
    org_id = project.get("org_id")
    if not org_id or (principal.get("org_id") and principal["org_id"] != org_id):
        return None, None

    team_id = project.get("team_id")
    if team_id is None:
        team_id = (
            await db.execute(
                select(Team.id).where(Team.org_id == org_id, Team.is_default.is_(True))
            )
        ).scalar_one_or_none()
    if not team_id:
        return None, None
    default_team = (
        await db.execute(
            select(Team.is_default).where(Team.id == team_id, Team.org_id == org_id)
        )
    ).scalar_one_or_none()
    if default_team is None:
        return None, None

    if principal.get("scoped_service") and principal.get("org_id") == org_id:
        return ("admin" if default_team else None), team_id

    user_id = principal.get("id")
    if not user_id:
        return None, team_id
    role = (
        await db.execute(
            select(TeamMembership.role).where(
                TeamMembership.team_id == team_id,
                TeamMembership.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if role is None and default_team:
        role = (
            await db.execute(
                select(OrgMember.role).where(
                    OrgMember.org_id == org_id, OrgMember.user_id == user_id
                )
            )
        ).scalar_one_or_none()
    return role, team_id


async def _effective_role(
    principal: dict[str, object], project: dict[str, object], db: AsyncSession
) -> tuple[str | None, str | None]:
    if _auth_disabled() or (
        principal.get("is_legacy_shared_token")
        and get_settings().LEGACY_SHARED_TOKEN_FULL_ACCESS
    ):
        team_id = project.get("team_id")
        return "owner", team_id if isinstance(team_id, str) else None
    if principal.get("is_legacy_shared_token"):
        return None, None
    role, team_id = await _team_grant(principal, project, db)
    scopes = principal.get("scopes")
    ceiling = scope_ceiling(scopes)
    if role in ROLE_LEVELS and ceiling is not None:
        return min((role, ceiling), key=lambda r: ROLE_LEVELS[r]), team_id
    return None, team_id


async def require_project_role(
    principal: dict[str, object] | None,
    project_id: str | None,
    min_role: str = "viewer",
    db: AsyncSession | None = None,
) -> ProjectAccess:
    """Resolve the team grant and token ceiling for one registered project."""
    if db is None:
        raise ValueError("db is required")
    if min_role not in ROLE_LEVELS:
        raise ValueError(f"Unknown project role {min_role!r}")
    if not principal and _auth_disabled():
        principal = {"id": "default"}
    if not principal:
        raise HTTPException(status_code=401, detail="Authentication required")

    project = (
        (await project_registry.aload_projects()).get(project_id)
        if project_id
        else None
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    org_id = str(project.get("org_id") or "")
    try:
        role, team_id = await _effective_role(principal, project, db)
        if role is None or ROLE_LEVELS.get(role, -1) < ROLE_LEVELS["viewer"]:
            raise HTTPException(status_code=404, detail="Project not found")
        if ROLE_LEVELS[role] < ROLE_LEVELS[min_role]:
            await db.rollback()
            from server.services.audit_service import ACTION_GATE_REJECTED

            await _audit_authz_denial(
                ACTION_GATE_REJECTED,
                principal,
                project_id,
                org_id,
                min_role,
                f"This action requires at least '{min_role}' role",
            )
            raise HTTPException(status_code=403, detail="Forbidden")
        return ProjectAccess(str(project_id), org_id, team_id, role)
    finally:
        if db.in_transaction():
            await db.rollback()


async def authorize_project_for_user(
    user: dict | None,
    project_id: str | None,
    db: AsyncSession,
    minimum_role: str = "viewer",
) -> str | None:
    """Compatibility entry point for WebSocket and MCP callers."""
    return (await require_project_role(user, project_id, minimum_role, db)).org_id


async def accessible_project_ids(
    request: Request, db: AsyncSession, minimum_role: str = "viewer"
) -> set[str]:
    """Registered projects visible to the caller at the requested role."""
    principal = getattr(request.state, "user", None)
    projects = await project_registry.aload_projects()
    if not isinstance(principal, dict) and _auth_disabled():
        principal = {"id": "default"}
    if not isinstance(principal, dict):
        return set()
    if _auth_disabled() or (
        principal.get("is_legacy_shared_token")
        and get_settings().LEGACY_SHARED_TOKEN_FULL_ACCESS
    ):
        return set(projects)
    if principal.get("is_legacy_shared_token"):
        return set()
    ceiling = scope_ceiling(principal.get("scopes"))
    if ceiling is None or ROLE_LEVELS[ceiling] < ROLE_LEVELS[minimum_role]:
        return set()
    visible = set()
    try:
        for project_id, project in projects.items():
            role, _ = await _team_grant(principal, project, db)
            if (
                role is not None
                and ROLE_LEVELS.get(role, -1) >= ROLE_LEVELS[minimum_role]
            ):
                visible.add(project_id)
        return visible
    finally:
        if db.in_transaction():
            await db.rollback()


async def _audit_authz_denial(
    action: str,
    user: dict | None,
    project_id: str | None,
    org_id: str | None,
    minimum_role: str,
    detail: object,
) -> None:
    """Write a chained authz-denial audit row on its own committed session.

    Not the request-scoped session: nothing commits it after a 403, so the row
    was rolled back with it (#1919). A denial has no business transaction to
    share.

    Fail-safe: ``log_audit_event_bg`` already swallows its own errors, but the
    whole emit is additionally guarded so a denial is always re-raised
    unchanged even if audit wiring breaks.
    """
    try:
        from ..services.audit_service import log_audit_event_bg

        await log_audit_event_bg(
            action=action,
            resource_type="project",
            resource_id=project_id,
            user_id=user.get("id") if isinstance(user, dict) else None,
            org_id=org_id,
            details={"detail": str(detail), "minimum_role": minimum_role},
        )
    except Exception:  # pragma: no cover - defensive
        pass


class ProjectAccessChecker:
    """FastAPI dependency for a project ID in a path parameter."""

    def __init__(self, minimum_role: str = "viewer") -> None:
        self.minimum_role = minimum_role

    async def _authorize(
        self, project_id: str, request: Request, db: AsyncSession
    ) -> dict:
        """Shared rule used by the project- and task-scoped variants."""
        user = getattr(request.state, "user", None)
        if user is None and _auth_disabled():
            user = {"id": "default", "role": "admin", "is_service": True}
        await require_project_role(user, project_id, self.minimum_role, db)
        return user

    async def __call__(
        self,
        request: Request,
        db: AsyncSession = Depends(get_db),
    ) -> dict[str, object]:
        pid = (
            request.path_params.get("project_id")
            or request.path_params.get("projectId")
            or request.query_params.get("project_id")
            or ""
        )
        return await self._authorize(pid, request, db)


class TaskAccessChecker(ProjectAccessChecker):
    """Like :class:`ProjectAccessChecker` but keyed on a ``task_id`` path param
    of the form ``project_id:spec_id`` (the form task/execution routes use)."""

    async def __call__(
        self,
        task_id: str,
        request: Request,
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        if ":" not in task_id:
            raise HTTPException(status_code=400, detail="Invalid task ID")
        project_id = task_id.partition(":")[0]
        return await self._authorize(project_id, request, db)


def require_project_access(minimum_role: str = "viewer") -> ProjectAccessChecker:
    """Factory for the project-scoped access dependency (``project_id`` param)."""
    return ProjectAccessChecker(minimum_role)


def require_task_access(minimum_role: str = "viewer") -> TaskAccessChecker:
    """Factory for the task-scoped access dependency (``task_id`` param)."""
    return TaskAccessChecker(minimum_role)


# Stable id of the deployment "default" org (mirrors database.engine).
DEFAULT_ORG_ID = "default"


def _roles_at_or_above(minimum_role: str) -> list[str]:
    """Every ``ROLE_LEVELS`` name whose level is >= ``minimum_role``'s.

    Pure function pulled out of ``accessible_org_ids`` so the role-floor
    logic (#1554 finding 2) is unit-testable without a database session.
    An unrecognised ``minimum_role`` floors at level 0 (``viewer``).
    """
    threshold = ROLE_LEVELS.get(minimum_role, 0)
    return [role for role, level in ROLE_LEVELS.items() if level >= threshold]


async def accessible_org_ids(
    request: Request, db: AsyncSession, minimum_role: str = "viewer"
) -> set[str] | None:
    """Org ids the caller may see, or ``None`` meaning "all" (#319 list filter).

    - Service principal → ``None`` (sees every project; local UI + M2M).
    - Human JWT user → the set of orgs where their role is at least
      ``minimum_role`` (default ``"viewer"`` — any membership, unchanged
      behaviour for every existing caller).
    - No identity → empty set.

    ``minimum_role`` exists for fleet-wide WRITE endpoints that have no single
    ``project_id``/``task_id`` to hang ``require_project_access``/
    ``require_task_access`` off of (e.g. the merger's PR-opening sweep, #1554
    finding 2) — those still need "more than viewer" the same way the
    per-task PR endpoint does, just applied across every org at once instead
    of one project's membership row.
    """
    user = getattr(request.state, "user", None)
    if _auth_disabled() or is_service_principal(user):
        return None
    if not isinstance(user, dict) or not user.get("id"):
        return set()
    query = select(OrgMember.org_id).where(OrgMember.user_id == user["id"])
    if minimum_role != "viewer":
        query = query.where(OrgMember.role.in_(_roles_at_or_above(minimum_role)))
    result = await db.execute(query)
    org_ids = set(result.scalars().all())
    if user.get("api_key_id"):  # a key is bound to its own org (#1913)
        org_ids &= {user.get("org_id")}
    return org_ids


async def resolve_owner_org_id(request: Request, db: AsyncSession) -> str:
    """Pick the org a newly-created project should belong to (#319).

    - Service principal (legacy token / dev) → the deployment ``default`` org.
    - Human JWT user → their first org membership (their Personal org).
    - Fallback → ``default`` org (never fails the create path).
    """
    user = getattr(request.state, "user", None)
    if _auth_disabled():
        return DEFAULT_ORG_ID
    if not isinstance(user, dict) or not user.get("id"):
        raise HTTPException(status_code=401, detail="Authentication required")
    if user.get("is_legacy_shared_token"):
        if get_settings().LEGACY_SHARED_TOKEN_FULL_ACCESS:
            return DEFAULT_ORG_ID
        raise HTTPException(status_code=403, detail="Forbidden")
    ceiling = scope_ceiling(user.get("scopes"))
    if ceiling is None or ROLE_LEVELS[ceiling] < ROLE_LEVELS["member"]:
        raise HTTPException(status_code=403, detail="Forbidden")
    query = select(OrgMember.org_id).where(
        OrgMember.user_id == user["id"],
        OrgMember.role.in_(_roles_at_or_above("member")),
    )
    if user.get("org_id"):
        query = query.where(OrgMember.org_id == user["org_id"])
    org_id = (await db.execute(query)).scalars().first()
    if not org_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    return str(org_id)
