"""SCIM Bearer-token authentication (Epic #35 #41).

The SCIM endpoint sits OUTSIDE the portal's normal auth stack: SCIM
clients (Okta, Azure AD) authenticate per-request with a static
Bearer token from a Kubernetes Secret, not via cookie or OIDC.

The token is read once at module import time from the
``SCIM_BEARER_TOKEN`` env var. Comparison is constant-time. Wrong /
missing tokens get a 401 with a typed SCIM error body.

## Rotation

Rotating the token = recreate the K8s Secret + restart the pod. The
``rollout restart`` flow with ``maxSurge: 1`` gives zero-downtime
rotation. Documented in the concept doc that ships with PR-2.
"""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .schemas import ScimError

logger = logging.getLogger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)


def _expected_token() -> str:
    """Read the configured Bearer token at call time (not import time).

    Reading per-request rather than caching at import means tests can
    monkeypatch the env var without re-importing the module, and
    operators who rotate via Secret-mount + pod restart still get
    the new value when the new pod starts.

    Returns empty string when unset — auth then unconditionally
    rejects."""
    return os.environ.get("SCIM_BEARER_TOKEN", "").strip()


async def require_scim_bearer(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    """FastAPI dependency: 401s when the Authorization header is
    missing or doesn't match the configured token.

    Returns None on success (no user object — SCIM clients aren't
    'users' in the portal sense; they're integration agents). The
    route handlers don't need the credential after the check.
    """
    expected = _expected_token()
    if not expected:
        # Misconfigured: SCIM enabled but token not set. Crashloop
        # would be louder, but a 503 is safer than a 401 silently
        # accepting because both sides are empty.
        logger.error(
            "SCIM auth: SCIM_BEARER_TOKEN env var is empty — endpoint "
            "is exposed but cannot authenticate any request"
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ScimError(
                status="503",
                detail="SCIM endpoint is not configured (token missing)",
                scim_type="invalidVers",
            ).model_dump(by_alias=True, exclude_none=True),
        )

    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ScimError(
                status="401",
                detail="Authorization header missing or not Bearer scheme",
            ).model_dump(by_alias=True, exclude_none=True),
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Constant-time comparison defends against timing oracle.
    if not hmac.compare_digest(creds.credentials, expected):
        # Don't log the rejected token — it could be a typo'd valid
        # token or could be a probe. Just count + log a generic line.
        logger.warning("SCIM auth: bearer token mismatch from client")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ScimError(
                status="401",
                detail="Invalid bearer token",
            ).model_dump(by_alias=True, exclude_none=True),
            headers={"WWW-Authenticate": "Bearer"},
        )
