"""Object-level authorization for project-scoped routes (epic #318, #319).

Projects are owned by an organization (``org_id`` on the project record).
Access is gated per-org via ``OrgMember`` — except for the **service
principal** (machine-to-machine callers authenticated by the legacy
``API_TOKEN``, and the auth-disabled dev user), which bypasses per-org checks so
Factory-sibling traffic (PFactory / TFactory) and the local UI keep working
unchanged (design A on #319).

The decision is split in two so it can be unit-tested without a database:

* :func:`check_project_access` — the pure rule (raises ``HTTPException``).
* :class:`ProjectAccessChecker` / :func:`require_project_access` — the FastAPI
  dependency that loads the project + membership and applies the rule.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database.engine import get_db
from ..database.models import OrgMember
from .organizations import ROLE_LEVELS

__all__ = [
    "check_project_access",
    "ProjectAccessChecker",
    "require_project_access",
    "is_service_principal",
]


def is_service_principal(user: dict | None) -> bool:
    """Whether the caller is a machine/dev principal that bypasses per-org authz.

    True for the legacy ``API_TOKEN`` service identity (``is_service``) and the
    auth-disabled global admin. These are trusted M2M/local callers (Factory
    siblings + the local UI), not multi-tenant human users.
    """
    if not isinstance(user, dict):
        return False
    return bool(user.get("is_service")) or user.get("role") == "admin"


def check_project_access(
    user: dict | None,
    project: dict | None,
    membership: OrgMember | None,
    minimum_role: str = "viewer",
) -> None:
    """Pure authorization rule. Raises ``HTTPException`` on denial; returns None on allow.

    - no user                       → 401
    - service principal / admin      → allow (M2M / dev)
    - project missing                → 404
    - project has no ``org_id``      → 403 (unowned legacy project, admin-only)
    - caller not a member            → 403
    - member role below ``minimum``  → 403
    """
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if is_service_principal(user):
        return

    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )

    org_id = project.get("org_id")
    if not org_id:
        # Legacy project never assigned to an org — only the service/admin
        # principal (handled above) may touch it until it's reassigned.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Project is not assigned to an organization",
        )

    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this project",
        )

    minimum_level = ROLE_LEVELS.get(minimum_role, 0)
    if ROLE_LEVELS.get(membership.role, -1) < minimum_level:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"This action requires at least '{minimum_role}' role in the "
                f"project's organization."
            ),
        )


class ProjectAccessChecker:
    """FastAPI dependency: authorize the caller against a project's owning org.

    Reads identity from ``request.state.user`` (set by ``TokenAuthMiddleware``)
    so the service-principal path needs no DB ``User`` row. For human users it
    resolves the project's ``org_id`` and looks up ``OrgMember``.
    """

    def __init__(self, minimum_role: str = "viewer") -> None:
        self.minimum_role = minimum_role

    async def _authorize(
        self, project_id: str, request: Request, db: AsyncSession
    ) -> dict:
        """Shared rule used by the project- and task-scoped variants."""
        user = getattr(request.state, "user", None)

        # Service principal short-circuits before any DB / project lookup.
        if is_service_principal(user):
            check_project_access(user, None, None, self.minimum_role)
            return user

        from .projects import load_projects  # lazy: avoid projects↔authz cycle

        project = load_projects().get(project_id)
        membership = None
        if (
            isinstance(user, dict)
            and user.get("id")
            and project
            and project.get("org_id")
        ):
            result = await db.execute(
                select(OrgMember).where(
                    OrgMember.org_id == project["org_id"],
                    OrgMember.user_id == user["id"],
                )
            )
            membership = result.scalar_one_or_none()

        check_project_access(user, project, membership, self.minimum_role)
        return user

    async def __call__(
        self,
        project_id: str,
        request: Request,
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        return await self._authorize(project_id, request, db)


class TaskAccessChecker(ProjectAccessChecker):
    """Like :class:`ProjectAccessChecker` but keyed on a ``task_id`` path param
    of the form ``project_id:spec_id`` (the form task/execution routes use)."""

    async def __call__(
        self,
        task_id: str,
        request: Request,
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        project_id = task_id.split(":", 1)[0] if ":" in task_id else task_id
        return await self._authorize(project_id, request, db)


def require_project_access(minimum_role: str = "viewer") -> ProjectAccessChecker:
    """Factory for the project-scoped access dependency (``project_id`` param)."""
    return ProjectAccessChecker(minimum_role)


def require_task_access(minimum_role: str = "viewer") -> TaskAccessChecker:
    """Factory for the task-scoped access dependency (``task_id`` param)."""
    return TaskAccessChecker(minimum_role)


# Stable id of the deployment "default" org (mirrors database.engine).
DEFAULT_ORG_ID = "default"


async def accessible_org_ids(request: Request, db: AsyncSession) -> set[str] | None:
    """Org ids the caller may see, or ``None`` meaning "all" (#319 list filter).

    - Service principal → ``None`` (sees every project; local UI + M2M).
    - Human JWT user → the set of orgs they're a member of.
    - No identity → empty set.
    """
    user = getattr(request.state, "user", None)
    if is_service_principal(user):
        return None
    if not isinstance(user, dict) or not user.get("id"):
        return set()
    result = await db.execute(
        select(OrgMember.org_id).where(OrgMember.user_id == user["id"])
    )
    return set(result.scalars().all())


async def resolve_owner_org_id(request: Request, db: AsyncSession) -> str:
    """Pick the org a newly-created project should belong to (#319).

    - Service principal (legacy token / dev) → the deployment ``default`` org.
    - Human JWT user → their first org membership (their Personal org).
    - Fallback → ``default`` org (never fails the create path).
    """
    user = getattr(request.state, "user", None)
    if is_service_principal(user) or not isinstance(user, dict) or not user.get("id"):
        return DEFAULT_ORG_ID
    result = await db.execute(
        select(OrgMember.org_id).where(OrgMember.user_id == user["id"])
    )
    org_id = result.scalars().first()
    return org_id or DEFAULT_ORG_ID
