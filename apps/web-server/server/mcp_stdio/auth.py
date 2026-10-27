"""Auth + scope gating for the stdio-MCP proxy (Issue #154).

Routes under ``/api/mcp-stdio/`` accept two kinds of bearer tokens:

1. **acw_ API keys** minted via ``routes/api_keys.py`` — validated
   against the ``ApiKey`` table, hashed at rest, scope-gated. This is
   the recommended path for enterprise installs: each developer holds
   a scoped key, no shared host-wide secret.

2. **Legacy admin token** (``settings.API_TOKEN`` — usually backed by
   ``~/.aifactory/.token``) — acts as a wildcard "all scopes" key.
   This preserves v1.0 behavior so single-user laptops keep working
   without minting a key.

Scopes are the same vocabulary used by ``mcp_remote`` plus a few
finer-grained ones for write operations:

- ``mcp:read``     — read tools (list/status/logs)
- ``project:write`` — create projects
- ``task:write``    — start, stop, recover, approve, create-and-run tasks
- ``task:merge``    — create-pr, merge-pr (default-branch blast radius)

Use ``require_acw_scope(scope)`` as a FastAPI dependency on each
proxy route. It returns the resolved ``AuthenticatedKey`` (or a
synthetic legacy key) so handlers can record key_id in audit logs.
Task routes enforce project membership for ``acw_`` keys. List routes
filter results to the key owner's organizations.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth import _acw_principal, _is_legacy_api_token
from server.config import get_settings
from server.database.engine import get_db
from server.error_ref import client_error
from server.mcp_remote import auth as mcp_remote_auth
from server.routes.project_authz import authorize_project_for_user

logger = logging.getLogger(__name__)

# Re-export the named scopes so callers don't have to know which
# module owns the constants.
MCP_READ_SCOPE = "mcp:read"
PROJECT_WRITE_SCOPE = "project:write"
TASK_WRITE_SCOPE = "task:write"
TASK_MERGE_SCOPE = "task:merge"

ALL_SCOPES = frozenset(
    {
        MCP_READ_SCOPE,
        PROJECT_WRITE_SCOPE,
        TASK_WRITE_SCOPE,
        TASK_MERGE_SCOPE,
    }
)


@dataclass(frozen=True)
class _LegacyAdminKey:
    """Synthetic key returned when the caller authenticated via the
    legacy admin token. Behaves like a real ``AuthenticatedKey`` but
    advertises every scope so existing v1.0 callers keep working.

    Mirroring the surface (``key_id``, ``user_id``, ``has_scope``) lets
    handlers treat both auth paths uniformly. ``key_id="legacy-admin"``
    in audit logs makes it easy to spot calls that bypassed scopes.
    """

    key_id: str = "legacy-admin"
    user_id: str | None = None

    @property
    def scopes(self) -> frozenset[str]:
        return ALL_SCOPES

    def has_scope(self, _scope: str) -> bool:
        return True


def _strip_bearer(authorization: str | None) -> str | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization[7:].strip()


def require_acw_scope(scope: str):
    """FastAPI ``Depends`` factory — returns a coroutine that resolves
    the auth header into either an ``AuthenticatedKey`` (acw_ path) or
    a ``_LegacyAdminKey`` (legacy admin path), and rejects when the
    resolved key lacks ``scope``.

    HTTP status mapping:
    - Missing / malformed Authorization → 401
    - Unknown / disabled acw_ key       → 401
    - Authenticated but lacks scope      → 403

    The 401-vs-403 split matters for the stdio MCP client's error
    messages: 401 says "your key is bad" (regenerate); 403 says "your
    key works but is scoped wrong" (mint a new one with the right
    scope).
    """

    async def _check(request: Request):
        token = _strip_bearer(request.headers.get("Authorization"))
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or malformed Authorization header (expected 'Bearer <token>')",
            )

        # Legacy admin token = wildcard. Matched constant-time via the
        # shared helper, which also refuses to authenticate anything when
        # API_TOKEN is unset (Factory#324 M1).
        if _is_legacy_api_token(token):
            return _LegacyAdminKey()

        try:
            key = await mcp_remote_auth.authenticate(f"Bearer {token}")
        except mcp_remote_auth.MCPAuthError as exc:
            # MCPAuthError is an InputRejectedError: its messages describe the
            # caller's own credential ("Invalid API key", "API key has been
            # revoked") and are worth handing back. client_error reads
            # client_message, so anything else landing here -- a driver error
            # from the key lookup, say -- gets a reference id instead of its
            # own text (Factory#718).
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=client_error(logger, "Authentication failed", exc),
            ) from exc

        if not key.has_scope(scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"API key lacks required scope '{scope}'. "
                    f"Mint a new key with the right scope via Settings → API Keys."
                ),
            )

        return key

    return _check


_AcwKey = mcp_remote_auth.AuthenticatedKey | _LegacyAdminKey


def acw_principal(key: _AcwKey) -> dict[str, object]:
    """Build the principal used by REST authorization for this key."""
    if isinstance(key, _LegacyAdminKey):
        return {"id": "default", "role": "user", "is_service": True}
    return _acw_principal(
        key,
        scoped_service_tokens_enabled=getattr(
            get_settings(), "SCOPED_SERVICE_TOKENS_ENABLED", False
        ),
    )


def require_acw_principal(scope: str) -> Callable[..., Awaitable[_AcwKey]]:
    """Require scope and expose the key owner to delegated list handlers."""

    async def _check(
        request: Request,
        # Closure-local dependency cannot resolve through Annotated with
        # postponed annotations.
        key: _AcwKey = Depends(require_acw_scope(scope)),  # noqa: B008
    ) -> _AcwKey:
        request.state.user = acw_principal(key)
        return key

    return _check


def require_acw_access(
    scope: str, minimum_role: str = "member"
) -> Callable[..., Awaitable[_AcwKey]]:
    """Require the scope and the REST route's project membership check."""

    async def _check(
        request: Request,
        db: Annotated[AsyncSession, Depends(get_db)],
        # Closure-local dependency: Annotated would be a string under
        # ``from __future__ import annotations`` and could not resolve it.
        key: _AcwKey = Depends(require_acw_scope(scope)),  # noqa: B008
    ) -> _AcwKey:
        principal = acw_principal(key)
        task_id = request.path_params.get("task_id")
        project_id = (
            task_id.partition(":")[0]
            if task_id is not None
            else request.query_params.get("project_id")
        )
        await authorize_project_for_user(principal, project_id, db, minimum_role)
        return key

    return _check
