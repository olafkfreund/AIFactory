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
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from jose import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..database import Organization, OrgMember, User
from ..database.engine import get_db
from ..oidc import get_oauth_client, is_oidc_enabled

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth/oidc", tags=["Auth (OIDC)"])


# ---------------------------------------------------------------------------
# Internal JWT helpers — mirror auth_routes.py exactly so the produced
# tokens are interchangeable with locally-authenticated tokens.
# ---------------------------------------------------------------------------


def _create_access_token(user: User) -> str:
    settings = get_settings()
    expires = datetime.now(timezone.utc) + timedelta(
        minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES
    )
    payload = {
        "sub": user.id,
        "email": user.email,
        "role": user.role,
        "type": "access",
        "exp": expires,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def _create_refresh_token(user: User) -> str:
    settings = get_settings()
    expires = datetime.now(timezone.utc) + timedelta(
        days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS
    )
    payload = {
        "sub": user.id,
        "type": "refresh",
        "exp": expires,
        "iat": datetime.now(timezone.utc),
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
    return await oauth.oidc.authorize_redirect(
        request, redirect_uri, nonce=nonce
    )


@router.get("/callback", summary="OIDC callback — exchange code for tokens", name="oidc_callback")
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

    # P3.1 minimal JIT: find-or-create User by email.
    # P3.3 will replace this with a proper sub-based lookup + role
    # claim mapping + OrganizationMember provisioning.
    name = userinfo.get("name") or email.split("@")[0]
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(
            email=email,
            name=name,
            password_hash="",  # OIDC users have no local password
            role="member",
            is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        logger.info("OIDC JIT-provisioned new user: %s (sub=%s)", email, sub)

    access_token = _create_access_token(user)
    refresh_token = _create_refresh_token(user)

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
