"""OIDC SSO routes for AIFactory (Epic #26 P3).

Endpoints:
  GET  /api/auth/oidc/login     — Authorization Code with PKCE + state.
                                  Redirects the browser to the IdP.
  GET  /api/auth/oidc/callback  — IdP redirects back here with `code`
                                  + `state`. We validate, mint internal
                                  JWT, set HTTP-only cookie, redirect
                                  to the post-login URL.

OIDC sits *alongside* the existing local-password flow in auth_routes.py
— it's a different way to obtain the same internal JWT. Downstream
middleware doesn't know or care which path produced the token.

JIT provisioning, refresh-session model, logout, and userinfo caching
land in subsequent P3 chunks (P3.3 / P3.4 / P3.5).
"""

from __future__ import annotations

import logging
import secrets as _secrets
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..database import User
from ..database.engine import get_db
from ..database.models import OidcRefreshSession
from ..oidc import get_oauth_client, is_oidc_enabled
from ..oidc.provisioning import jit_provision_user
from ..oidc.userinfo_cache import get_cached, invalidate
from ..oidc.userinfo_cache import put as cache_put
from .auth_routes import create_access_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth/oidc", tags=["Auth (OIDC)"])


# ---------------------------------------------------------------------------
# Internal JWT helpers — mirror auth_routes.py exactly so the produced
# tokens are interchangeable with locally-authenticated tokens.
# ---------------------------------------------------------------------------


def _capture_idp_refresh_token(refresh_token: str | None) -> str | None:
    """Return the IdP refresh token to persist, or None if it can't be stored
    encrypted at rest (#366).

    The column is encrypted via the KMS backend; if no KMS key is configured
    (a deployment that hasn't enabled encryption-at-rest), we drop the token
    rather than crash login. The refresh path then falls back to the liveness
    probe — same as a legacy session.
    """
    if not refresh_token:
        return None
    try:
        from ..crypto.kms import get_backend

        get_backend()  # raises if no key configured
        return refresh_token
    except Exception:  # noqa: BLE001
        logger.warning(
            "KMS not configured; not storing IdP refresh token "
            "(OIDC revocation falls back to liveness probe)"
        )
        return None


def _create_refresh_token(user: User, jti: str) -> str:
    """Create a refresh token with a JTI that ties it to a RefreshSession row."""
    settings = get_settings()
    expires = datetime.now(UTC) + timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS)
    payload = {
        "sub": user.id,
        "type": "refresh",
        "jti": jti,
        "exp": expires,
        "iat": datetime.now(UTC),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def _post_login_redirect(request: Request) -> str:
    """Where to send the user after a successful OIDC login.

    Honors ``APP_OIDC_POST_LOGIN_REDIRECT`` env if set; otherwise the
    app's root.
    """
    import os

    return os.environ.get("APP_OIDC_POST_LOGIN_REDIRECT", "/")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/enabled", summary="Whether OIDC SSO is available on this deployment")
async def oidc_enabled() -> dict:
    """Cheap probe (#149) so the login page can decide whether to auto-initiate a
    silent SSO handoff. Avoids bouncing users to a 404 on installs without OIDC.
    """
    return {"enabled": is_oidc_enabled()}


@router.get("/login", summary="Begin OIDC Authorization Code + PKCE flow")
async def oidc_login(request: Request):
    """Redirect the browser to the IdP authorization endpoint.

    Authlib auto-generates the PKCE ``code_verifier``/``code_challenge``
    pair and the ``state`` nonce, stashing both in the Starlette session
    (which is signed via SessionMiddleware so the browser can't tamper
    with them). The callback retrieves them server-side to complete the
    exchange.
    """
    if not is_oidc_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="OIDC SSO is not configured on this deployment",
        )
    import os
    import secrets as _secrets

    oauth = get_oauth_client()
    redirect_uri = os.environ.get("APP_OIDC_REDIRECT_URI") or str(
        request.url_for("oidc_callback")
    )
    # OIDC requires the ID token to echo back the nonce we send in the
    # auth request — authlib validates this at /callback. authlib does
    # NOT auto-generate a nonce; we must pass one explicitly. Stored
    # in the session by authlib for the callback round-trip.
    nonce = _secrets.token_urlsafe(32)
    # Silent SSO handoff (#149): switching between portals that share the one
    # Keycloak realm should not re-prompt. The frontend probes with ?prompt=none;
    # we forward it so Keycloak returns error=login_required (handled gracefully
    # at the callback) instead of showing its own login page when there is no
    # session. Only "none" is forwarded — never a caller-chosen arbitrary prompt.
    extra: dict[str, str] = {}
    if request.query_params.get("prompt") == "none":
        extra["prompt"] = "none"
    return await oauth.oidc.authorize_redirect(
        request, redirect_uri, nonce=nonce, **extra
    )


@router.get(
    "/callback",
    summary="OIDC callback — exchange code for tokens",
    name="oidc_callback",
)
async def oidc_callback(request: Request, db: AsyncSession = Depends(get_db)):
    """Validate the IdP redirect, mint an internal JWT, redirect home.

    Authlib's ``authorize_access_token`` verifies the ``state`` nonce
    (raises ``MismatchingStateError`` if tampered), exchanges the code
    using the stashed PKCE verifier, fetches the ID token + access
    token + userinfo, and validates ID token signature against the
    IdP's JWKS.
    """
    if not is_oidc_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="OIDC SSO is not configured on this deployment",
        )

    # Silent-probe fallback (#149): a ?prompt=none login with no live Keycloak
    # session comes back with error=login_required (and no code). That is the
    # expected "not signed in yet" outcome — route to the manual login page
    # instead of surfacing a 400. Also covers a user-declined/consent error.
    if request.query_params.get("error"):
        logger.info(
            "OIDC callback returned error=%s — routing home",
            str(request.query_params.get("error"))[:64],
        )
        # Redirect to the SPA root, NOT "/login": /login is a client-side React
        # route and some portals' web-server 404s a server request for it. The
        # SPA at "/" renders the login form client-side when unauthenticated.
        return RedirectResponse(url="/")

    oauth = get_oauth_client()
    try:
        token = await oauth.oidc.authorize_access_token(request)
    except Exception as exc:  # authlib's specific exceptions vary by version
        logger.warning(
            "OIDC callback rejected: %s: %s",
            type(exc).__name__,
            str(exc)[:200],
        )
        # IMPORTANT: don't echo the raw error message — it may include
        # attacker-controlled values from a tampered state/code param
        # (reflected-XSS defense).
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OIDC callback rejected",
        )

    userinfo = token.get("userinfo") or {}
    sub = userinfo.get("sub")
    email = userinfo.get("email")
    if not sub or not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ID token missing required claims (sub, email)",
        )

    # JIT provisioning: stable sub-based User lookup + claim-mapped role
    # + OrganizationMember row in the configured default org.
    user = await jit_provision_user(db, userinfo)

    # Epic #35 #43 PR-1b4 — stamp last successful login time. Read by
    # the /api/admin/access-review endpoint (SOC2 CC6.2 / ISO 27001
    # A.9.2.5). Naive-UTC by convention; we strip tzinfo to match the
    # DateTime column type.
    user.last_login_at = datetime.now(UTC).replace(tzinfo=None)

    # Create a RefreshSession so the refresh path can hit IdP userinfo
    # and propagate revocation. The jti binds the refresh JWT to the
    # session row; logging out / revocation = deleting the row.
    settings = get_settings()
    jti = _secrets.token_urlsafe(32)
    refresh_expires = datetime.now(UTC) + timedelta(
        days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS
    )
    session_row = OidcRefreshSession(
        user_id=user.id,
        jti=jti,
        oidc_sub=sub,
        expires_at=refresh_expires.replace(tzinfo=None),
        # #366: store the IdP refresh token for real per-user revocation at
        # refresh time. None when the IdP issues no refresh token OR when
        # encryption-at-rest isn't configured — in both cases the refresh path
        # falls back to the discovery-liveness probe, so login never breaks on
        # a KMS-less deployment.
        idp_refresh_token=_capture_idp_refresh_token(token.get("refresh_token")),
    )
    db.add(session_row)
    await db.commit()
    # Seed the userinfo cache while we have the validated claims.
    cache_put(sub, userinfo)

    access_token = create_access_token(user)
    refresh_token = _create_refresh_token(user, jti=jti)

    redirect = RedirectResponse(url=_post_login_redirect(request))
    # HTTP-only cookie so JS can't read it. Secure left unset for dev;
    # the operator's reverse-proxy / Helm chart will add it when TLS is
    # terminated upstream.
    redirect.set_cookie(
        "access_token",
        access_token,
        httponly=True,
        samesite="lax",
        max_age=60 * get_settings().JWT_ACCESS_TOKEN_EXPIRE_MINUTES,
    )
    redirect.set_cookie(
        "refresh_token",
        refresh_token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * get_settings().JWT_REFRESH_TOKEN_EXPIRE_DAYS,
    )
    return redirect


# ---------------------------------------------------------------------------
# /api/auth/oidc/refresh — IdP-validated refresh (P3.4)
# ---------------------------------------------------------------------------


class RefreshRequest(BaseModel):
    refresh_token: SecretStr


class RefreshResponse(BaseModel):
    access_token: str = Field(repr=False)


async def _validate_against_idp(idp_refresh_token: str | None) -> IdpValidation:
    """Real per-user revocation check via an IdP refresh-token grant (#366).

    Keycloak / Okta / Azure AD reject a refresh-token grant for a disabled or
    revoked user with HTTP 400 ``error=invalid_grant`` — that is the revocation
    signal. A transient IdP failure (5xx / network / non-revocation error) must
    NOT be read as revocation, so it returns ``"idp_error"`` and the caller
    keeps the session alive.

    Legacy sessions (and IdPs that issue no refresh token) have
    ``idp_refresh_token is None`` and fall back to a discovery-liveness probe —
    the pre-#366 behaviour — so they keep working without a forced re-login.
    """
    import os

    issuer = os.environ["APP_OIDC_ISSUER_URL"].rstrip("/")

    # Fallback for sessions without a stored IdP refresh token.
    if not idp_refresh_token:
        try:
            async with httpx.AsyncClient(timeout=10.0) as http:
                r = await http.get(f"{issuer}/.well-known/openid-configuration")
                return "valid" if r.status_code == 200 else "idp_error"
        except Exception:  # noqa: BLE001
            return "idp_error"

    client_id = os.environ["APP_OIDC_CLIENT_ID"]
    client_secret = os.environ["APP_OIDC_CLIENT_SECRET"]
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            disc = (await http.get(f"{issuer}/.well-known/openid-configuration")).json()
            token_url = disc["token_endpoint"]
            tr = await http.post(
                token_url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": idp_refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
            if tr.status_code == 200:
                return "valid"
            if tr.status_code == 400:
                try:
                    error = tr.json().get("error", "invalid_grant")
                except Exception:  # noqa: BLE001
                    error = "invalid_grant"
                # Only invalid_grant means "user revoked". Other 400s
                # (e.g. invalid_client = operator misconfig) are not the
                # user's fault → don't lock them out.
                return "revoked" if error == "invalid_grant" else "idp_error"
            # 5xx / unexpected → transient.
            return "idp_error"
    except Exception:  # noqa: BLE001
        return "idp_error"


@router.post(
    "/refresh",
    response_model=RefreshResponse,
    summary="Refresh access token, validating against IdP",
)
async def oidc_refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)):
    """Validate the refresh JWT + IdP-side liveness, mint a new access token.

    Flow:
      1. Decode the refresh JWT (signature + expiry checked).
      2. Look up the OidcRefreshSession by jti — if missing, the
         session was revoked (logged out, IdP-disabled, etc.).
      3. Check userinfo cache for the sub. If hit, mint a new
         access token and return.
      4. On cache miss, hit the IdP. If it accepts, cache + mint.
         If it rejects, delete the session row + return 401.
    """
    if not is_oidc_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="OIDC SSO is not configured on this deployment",
        )
    settings = get_settings()
    try:
        payload = jwt.decode(
            body.refresh_token.get_secret_value(),
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Not a refresh token")
    jti = payload.get("jti")
    user_id = payload.get("sub")
    if not jti or not user_id:
        raise HTTPException(status_code=401, detail="Refresh token missing jti/sub")

    # Look up the session row.
    result = await db.execute(
        select(OidcRefreshSession).where(OidcRefreshSession.jti == jti)
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(
            status_code=401, detail="Refresh session revoked or unknown"
        )

    # Try cache first.
    cached = get_cached(session.oidc_sub)
    if cached is None:
        # Real per-user revocation check against the IdP (#366).
        result = await _validate_against_idp(session.idp_refresh_token)
        if result == "revoked":
            # User disabled / token revoked at the IdP → kill the session.
            invalidate(session.oidc_sub)
            await db.delete(session)
            await db.commit()
            raise HTTPException(
                status_code=401, detail="User disabled or revoked at the IdP"
            )
        if result == "idp_error":
            # Transient IdP failure — do NOT revoke (that would lock users out
            # on an IdP outage). Signal "retry shortly"; the refresh cookie
            # stays valid and the access token still has TTL left.
            raise HTTPException(
                status_code=503,
                detail="Identity provider temporarily unavailable; retry shortly",
            )
        # result == "valid" — re-cache (presence == "validated within TTL").
        cache_put(session.oidc_sub, {"sub": session.oidc_sub, "validated": True})

    # Look up the user, mint new access token.
    user_result = await db.execute(select(User).where(User.id == session.user_id))
    user = user_result.scalar_one()
    session.last_validated_at = datetime.now(UTC).replace(tzinfo=None)
    await db.commit()
    return RefreshResponse(access_token=create_access_token(user))


# ---------------------------------------------------------------------------
# /api/auth/oidc/logout — delete session + redirect to IdP end-session (P3.5)
# ---------------------------------------------------------------------------


class LogoutRequest(BaseModel):
    refresh_token: SecretStr | None = None


@router.post("/logout", summary="Logout: delete session + redirect to IdP end-session")
async def oidc_logout(
    request: Request,
    body: LogoutRequest | None = None,
    db: AsyncSession = Depends(get_db),
):
    """End the OIDC session locally + redirect to IdP's end_session_endpoint.

    Behavior:
      - If a valid refresh token is present (body or cookie), look up
        the OidcRefreshSession row and delete it. Invalidate the
        userinfo cache for that sub.
      - Look up the IdP's ``end_session_endpoint`` from its OIDC
        discovery document. If advertised, 302 to it (with
        post_logout_redirect_uri). If NOT advertised (some legacy
        OAuth-only IdPs), 302 to our own post-logout page.
      - In all paths, clear the access_token + refresh_token cookies.
    """
    if not is_oidc_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="OIDC SSO is not configured on this deployment",
        )

    supplied = (
        body.refresh_token.get_secret_value() if body and body.refresh_token else None
    )
    refresh_token = supplied or request.cookies.get("refresh_token")

    if refresh_token:
        settings = get_settings()
        try:
            payload = jwt.decode(
                refresh_token,
                settings.JWT_SECRET,
                algorithms=[settings.JWT_ALGORITHM],
            )
            jti = payload.get("jti")
            if jti:
                result = await db.execute(
                    select(OidcRefreshSession).where(OidcRefreshSession.jti == jti)
                )
                session_row = result.scalar_one_or_none()
                if session_row is not None:
                    invalidate(session_row.oidc_sub)
                    await db.delete(session_row)
                    await db.commit()
        except JWTError:
            # Already-invalid token — still proceed with the logout
            # redirect; we can't authenticate against the IdP without
            # a valid token but we can still clear cookies + redirect.
            pass

    # Resolve the IdP's end_session_endpoint.
    import os

    issuer = os.environ["APP_OIDC_ISSUER_URL"].rstrip("/")
    discovery_url = f"{issuer}/.well-known/openid-configuration"
    end_session_url = None
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            disc = (await http.get(discovery_url)).json()
            end_session_url = disc.get("end_session_endpoint")
    except Exception as exc:  # noqa: BLE001
        logger.warning("OIDC discovery failed during logout: %s", exc)

    post_logout = os.environ.get("APP_OIDC_POST_LOGOUT_REDIRECT", "/")
    if end_session_url:
        from urllib.parse import urlencode

        redirect_url = (
            f"{end_session_url}?{urlencode({'post_logout_redirect_uri': post_logout})}"
        )
    else:
        redirect_url = post_logout

    response = RedirectResponse(url=redirect_url, status_code=302)
    response.delete_cookie("access_token")
    response.delete_cookie("refresh_token")
    return response
