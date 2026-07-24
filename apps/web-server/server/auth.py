"""
Authentication middleware for Magestic AI Web Server.

Supports dual authentication:
1. JWT tokens (primary) - validated via python-jose, populates request.state.user
2. Legacy bearer token (fallback) - simple string comparison for API key access
3. Scoped ``acw_`` API keys - per-org/project keys minted in Settings → API Keys.

Public paths (no auth required): /api/auth/*, /api/health, static assets, etc.

Legacy wildcard API_TOKEN (DEPRECATED — #555)
---------------------------------------------
The legacy ``API_TOKEN`` grants host-wide ``is_service=True`` and is shared
across Factory siblings, so a compromise of any sibling yields full admin REST
+ WebSocket access. It is RETAINED for backward compatibility — notably the
PFactory->AIFactory machine-to-machine path that ships ``APP_API_TOKEN`` — but
is deprecated. Every authentication via the wildcard token emits a one-time
loud warning (``_warn_legacy_api_token_once``). New machine-to-machine and
programmatic integrations should mint scoped per-org ``acw_`` keys instead,
which carry org/project scope rather than wildcard admin.

SAML sessions (Epic #35 #41)
-----------------------------
SAML-minted access tokens are identical in shape to locally-minted and
OIDC-minted tokens: ``{"sub": ..., "email": ..., "role": ..., "type":
"access", "exp": ..., "iat": ...}``.  No changes to the JWT decoder are
needed — ``_try_decode_jwt`` already handles them.

The SAML flow endpoints (``/api/auth/saml/login``, ``/api/auth/saml/acs``,
``/api/auth/saml/metadata``) and the IdP discovery endpoint
(``/api/auth/identity-providers``) are covered by the existing
``/api/auth/`` prefix in ``PUBLIC_PREFIXES``, so they bypass JWT validation
before the routes module runs — consistent with how the OIDC endpoints are
handled.

SCIM (Epic #35 #41)
--------------------
``/scim/v2/*`` uses its own Bearer-token middleware (``scim/auth.py``) —
the portal JWT is not valid there.  Adding the prefix to ``PUBLIC_PREFIXES``
lets the SCIM Bearer middleware run without the JWT middleware rejecting the
request first.
"""

import hmac
import logging

from fastapi import HTTPException, Request, WebSocket, status
from jose import JWTError, jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .config import get_settings

logger = logging.getLogger(__name__)

# One-time deprecation flags (#555). The legacy wildcard API_TOKEN and the
# WS-terminal ``?token=`` query param are still honored for backward
# compatibility, but each emits a single loud warning per process so the
# signal isn't drowned out by per-request log spam.
_LEGACY_TOKEN_DEPRECATION_LOGGED = False
_WS_QUERY_TOKEN_DEPRECATION_LOGGED = False


def _warn_legacy_api_token_once() -> None:
    """Emit a one-time deprecation warning for legacy wildcard API_TOKEN use.

    The legacy ``API_TOKEN`` grants host-wide ``is_service=True`` and is shared
    across Factory siblings (#555). It remains a documented fallback for
    PFactory->AIFactory M2M (``APP_API_TOKEN``), but new integrations should mint
    scoped per-org ``acw_`` keys (Settings → API Keys) instead, which carry
    org/project scope rather than wildcard admin.
    """
    global _LEGACY_TOKEN_DEPRECATION_LOGGED
    if _LEGACY_TOKEN_DEPRECATION_LOGGED:
        return
    _LEGACY_TOKEN_DEPRECATION_LOGGED = True
    logger.warning(
        "⚠️  DEPRECATED: request authenticated via the legacy wildcard "
        "API_TOKEN, which grants host-wide service access shared across "
        "Factory siblings. Migrate machine-to-machine traffic to scoped "
        "acw_ keys (Settings → API Keys). This token is retained only for "
        "backward compatibility (#555)."
    )


def _warn_ws_query_token_once() -> None:
    """Emit a one-time deprecation warning for WS ``?token=`` query auth (#555).

    Passing a bearer token in the URL leaks it into proxy/access logs and
    browser history — especially dangerous for the PTY/shell terminal socket.
    Clients should send ``Authorization: Bearer <token>`` instead.
    """
    global _WS_QUERY_TOKEN_DEPRECATION_LOGGED
    if _WS_QUERY_TOKEN_DEPRECATION_LOGGED:
        return
    _WS_QUERY_TOKEN_DEPRECATION_LOGGED = True
    logger.warning(
        "⚠️  DEPRECATED: WebSocket token supplied via the ?token= query "
        "param, which leaks into proxy/access logs and browser history. "
        "Send it via the 'Authorization: Bearer <token>' header instead "
        "(#555)."
    )


def _ws_extract_token(websocket: WebSocket) -> str | None:
    """Pull the bearer token from a WebSocket, preferring the header (#555).

    Order: ``Authorization: Bearer <token>`` header first, then the legacy
    ``?token=`` query param. Using the query param emits a one-time deprecation
    warning since it leaks the token into logs/history. Returns ``None`` when no
    token is present.
    """
    auth_header = websocket.headers.get("authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header[7:]

    # Backward-compatible fallback: token in the URL query string.
    query_token = websocket.query_params.get("token")
    if query_token:
        _warn_ws_query_token_once()
        return query_token

    return None


def _is_legacy_api_token(token: str) -> bool:
    """Constant-time match of a presented token against the legacy API_TOKEN.

    Uses ``hmac.compare_digest`` to avoid the timing oracle of ``==`` (#324
    M1). An unset ``API_TOKEN`` never matches, so the legacy path can't be
    enabled by an empty configured token.
    """
    configured = get_settings().API_TOKEN
    if not configured or not token:
        return False
    return hmac.compare_digest(token, configured)


def _try_decode_jwt(token: str) -> dict | None:
    """Attempt to decode a JWT access token.

    Returns the decoded payload dict on success, or ``None`` if the token
    is not a valid JWT (e.g. it is a legacy API token).
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
        )
        # Only accept access tokens (not refresh tokens)
        if payload.get("type") != "access":
            return None
        return payload
    except JWTError:
        return None


def _acw_principal(key, *, scoped_service_tokens_enabled: bool) -> dict:
    """Build the ``request.state.user`` principal for a validated ``acw_`` key.

    Scoped service tokens (Factory#312, step 1 — additive, off by default). When
    ``scoped_service_tokens_enabled`` is true, the key's EXPLICIT scopes are
    surfaced on the principal (``scopes`` + ``scoped_service`` markers) so a later
    phase can gate M2M access per-scope instead of the wildcard's blanket
    ``is_service`` bypass. The flag only ADDS keys to the dict; ``is_service`` is
    unchanged, so with the flag off (default) the result is byte-identical to the
    pre-change behaviour and nothing that reads the principal today is affected.

    See docs/compliance/scoped-service-tokens.md.
    """
    principal = {
        "id": key.user_id,
        "email": None,
        "role": "user",
        "org_id": key.org_id,
        "api_key_id": key.key_id,
        "is_service": key.user_id is None,
    }
    if scoped_service_tokens_enabled:
        principal["scopes"] = sorted(key.scopes)
        principal["scoped_service"] = key.user_id is None
    return principal


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Middleware to validate bearer token on API routes.

    Authentication strategy (tried in order):
    1. If the token is a valid JWT access token, populate
       ``request.state.user`` with the decoded claims and allow the request.
    2. If the token matches the legacy ``settings.API_TOKEN``, set
       ``request.state.user = None`` (backward compatible) and allow.
    3. Otherwise reject with 401.
    """

    # Paths that don't require authentication
    PUBLIC_PATHS = {
        "/",
        "/index.html",
        "/favicon.ico",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/api/health",
        "/api/logs/frontend",  # Allow frontend error logs without auth
    }

    # Path prefixes that don't require authentication
    PUBLIC_PREFIXES = (
        "/assets/",
        "/static/",
        "/api/auth/",  # Auth endpoints (register, login, refresh, logout)
        # Includes /api/auth/saml/* and /api/auth/identity-providers
        "/api/email/auth/",  # OAuth callbacks (redirect from Microsoft/Google)
        # SCIM 2.0 (Epic #35 #41) — uses its own static Bearer-token auth
        # (``scim/auth.py``).  Bypassing the JWT middleware here lets the
        # SCIM Bearer middleware validate the request instead of getting a
        # JWT-middleware 401 first.
        "/scim/v2/",
        # Remote MCP control plane (Epic #50 / Issue #83). The legacy
        # TokenAuthMiddleware validates JWT + the legacy API_TOKEN;
        # MCP clients send their ``acw_<key>`` instead, validated by
        # ``mcp_remote.auth.authenticate`` inside the route handler.
        # Adding the prefix here so the middleware doesn't 401 those
        # requests before our adapter runs.  Routes are only mounted
        # when AIFACTORY_MCP_REMOTE_ENABLED=true, so this prefix is a
        # no-op on the default v1.0 pilot deployment.
        "/api/mcp-remote/",
        # Stdio MCP control-plane proxy (Issue #154). Same shape as
        # mcp-remote: the proxy handlers do their own ``acw_`` key
        # validation via ``mcp_stdio.auth.require_acw_scope``, so this
        # middleware must let those requests through without checking
        # JWT/legacy first.
        "/api/mcp-stdio/",
    )

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Initialize user state to None for every request
        request.state.user = None

        # Skip all auth when disabled (development mode).
        # Populate request.state.user with the default user so that routes
        # using ``Depends(get_current_user)`` work too — the engine creates
        # the "default" user row on startup when DISABLE_AUTH is set.
        settings = get_settings()
        if settings.DISABLE_AUTH:
            request.state.user = {
                "id": "default",
                "email": "default@localhost",
                "role": "admin",
            }
            return await call_next(request)

        # Skip auth for public paths
        if path in self.PUBLIC_PATHS:
            return await call_next(request)

        # Skip auth for public prefixes (static files + auth routes)
        if path.startswith(self.PUBLIC_PREFIXES):
            return await call_next(request)

        # Skip auth for non-API paths (SPA routes handled by static files)
        if not path.startswith("/api"):
            return await call_next(request)

        # --- Authenticate API routes ---
        # Accept the token from the Authorization: Bearer header, OR fall back
        # to the `access_token` cookie set by the OIDC login callback (HttpOnly
        # cookie session). Without this, SSO logins set a cookie the middleware
        # ignored, so the SPA bounced straight back to /login.
        auth_header = request.headers.get("Authorization")
        token = None
        if auth_header:
            if not auth_header.startswith("Bearer "):
                return JSONResponse(
                    {"error": "Invalid Authorization header format"},
                    status_code=status.HTTP_401_UNAUTHORIZED,
                )
            token = auth_header[7:]  # Remove "Bearer " prefix
        else:
            token = request.cookies.get("access_token")

        if not token:
            return JSONResponse(
                {"error": "Missing Authorization header"},
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

        # Strategy 1: Try JWT validation
        jwt_payload = _try_decode_jwt(token)
        if jwt_payload is not None:
            # Populate request.state.user with JWT claims
            request.state.user = {
                "id": jwt_payload.get("sub"),
                "email": jwt_payload.get("email"),
                "role": jwt_payload.get("role", "user"),
            }
            return await call_next(request)

        # Strategy 2: Fall back to legacy bearer token
        if _is_legacy_api_token(token):
            # Legacy wildcard token — a machine/service principal (Factory
            # siblings, the local UI). `is_service` lets project-level authz
            # (epic #318/#319) grant M2M access without per-org membership,
            # preserving the sibling + local-UI contract. Notifications still
            # work via the id.
            #
            # DEPRECATED (#555): this token is host-wide and shared across
            # siblings. It is kept only for backward compatibility (notably
            # PFactory->AIFactory M2M via APP_API_TOKEN). New integrations
            # should mint scoped per-org ``acw_`` keys (Strategy 3 below /
            # Settings → API Keys) instead of the wildcard token.
            _warn_legacy_api_token_once()
            request.state.user = {
                "id": "default",
                "email": None,
                "role": "user",
                "is_service": True,
            }
            return await call_next(request)

        # Strategy 3: per-user ``acw_`` API key minted in Settings → API Keys
        # (#479). Lets a user authenticate programmatic REST access with their
        # own scoped token, not just the stdio-MCP proxy path. Gated on the
        # ``acw_`` prefix so JWT/legacy tokens never incur the DB lookup.
        if token.startswith("acw_"):
            try:
                from .mcp_remote.auth import authenticate as _authenticate_acw

                key = await _authenticate_acw(f"Bearer {token}")
                request.state.user = _acw_principal(
                    key,
                    scoped_service_tokens_enabled=settings.SCOPED_SERVICE_TOKENS_ENABLED,
                )
                return await call_next(request)
            except Exception:
                # Unknown/disabled/expired acw_ key → fall through to 401.
                pass

        # Neither JWT, legacy token, nor a valid acw_ key matched
        return JSONResponse(
            {"error": "Invalid token"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )


async def verify_websocket_token(websocket: WebSocket) -> bool:
    """Verify token for WebSocket connections.

    Token can be passed as:
    - Header (preferred): Authorization: Bearer xxx
    - Query parameter (DEPRECATED, #555): ?token=xxx — leaks into proxy/access
      logs and browser history; emits a one-time deprecation warning.

    Accepts both JWT tokens and legacy API tokens.
    """
    settings = get_settings()

    if settings.DISABLE_AUTH:
        return True

    # Prefer the Authorization header; fall back to the legacy ?token= query
    # param (with a deprecation warning) for backward compatibility (#555).
    token = _ws_extract_token(websocket)

    if not token:
        await websocket.close(code=4001, reason="Unauthorized")
        return False

    # Accept valid JWT access tokens
    if _try_decode_jwt(token) is not None:
        return True

    # Accept legacy API token (deprecated wildcard — see #555)
    if _is_legacy_api_token(token):
        _warn_legacy_api_token_once()
        return True

    await websocket.close(code=4001, reason="Unauthorized")
    return False


async def authenticate_websocket(websocket: WebSocket) -> dict | None:
    """Authenticate a WebSocket connection and return user info.

    Returns a dict with user claims on success, or ``None`` for legacy
    token connections.  Closes the socket and raises if authentication
    fails entirely.

    The returned dict (when not None) contains:
    - ``id``: user UUID
    - ``email``: user email
    - ``role``: global role
    """
    settings = get_settings()

    if settings.DISABLE_AUTH:
        return None

    # Prefer the Authorization header; fall back to the legacy ?token= query
    # param (with a deprecation warning) for backward compatibility (#555).
    token = _ws_extract_token(websocket)

    if not token:
        await websocket.close(code=4001, reason="Unauthorized")
        raise WebSocketAuthError("No token provided")

    # Try JWT — returns user info
    jwt_payload = _try_decode_jwt(token)
    if jwt_payload is not None:
        return {
            "id": jwt_payload.get("sub"),
            "email": jwt_payload.get("email"),
            "role": jwt_payload.get("role", "user"),
        }

    # Fall back to legacy token — no user info available (deprecated, #555)
    if _is_legacy_api_token(token):
        _warn_legacy_api_token_once()
        return None

    await websocket.close(code=4001, reason="Unauthorized")
    raise WebSocketAuthError("Invalid token")


class WebSocketAuthError(Exception):
    """Raised when WebSocket authentication fails."""

    pass
