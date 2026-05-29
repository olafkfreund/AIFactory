"""SAML 2.0 SP routes (Epic #35 #41 PR-1b2 + Epic #35 #209 v1.2 SLO).

Five endpoints, mounted under ``/api/auth/saml``:

* ``GET  /login``     — SP-init: builds an AuthnRequest, signs an HMAC
                        RelayState, redirects to the IdP SSO URL.
* ``POST /acs``       — Assertion Consumer Service. Handles both SP-init
                        (RelayState present + verified) and IdP-init
                        (no RelayState; lands on ``idp_init_default_return_to``).
* ``GET  /metadata``  — Public SP metadata XML for the operator to upload
                        at the IdP.
* ``POST /logout``    — SP-initiated SLO: reads NameID from the access-token
                        cookie, builds a signed LogoutRequest, mints an HMAC
                        RelayState, redirects to the IdP SLO URL. Returns 404
                        when ``SAML_SLO_ENABLED`` is false (v1.2).
* ``POST /sls``       — Single Logout Service: handles BOTH the IdP's
                        LogoutResponse (SP-init confirmation, RelayState present)
                        and the IdP's LogoutRequest (IdP-init). Validates
                        signature, replay cache, RelayState; clears session
                        cookies. Returns 404 when ``SAML_SLO_ENABLED`` is false.

What this module DOES NOT do
----------------------------

* It does not parse SAML XML itself — that's the OneLogin SDK's job,
  invoked via the ``SamlClient`` wrapper.
* It does not build the IdP discovery dropdown — that's
  ``identity_providers.py`` (PR-1b4).
* It does not own the JWT-cookie session shape — it reuses the helpers
  that ``routes/oidc_routes.py`` already defines (same access/refresh
  cookie pair downstream middleware reads).

Security boundary
-----------------

This file MUST refuse to:

* Auto-create users (decision #4): if SCIM hasn't provisioned the
  user, /acs returns ``403`` and never inserts a row.
* Link a SAML assertion to a user that already has identities from a
  different IdP kind (decision #4): returns ``409``.
* Trust an assertion that was already consumed (decision #10):
  ``SamlReplayCache.check_and_add`` rejects duplicates inside the
  assertion's own NotOnOrAfter window.
* Trust an unsigned RelayState on SP-init: ``relay_state.verify``
  raises and we treat the flow as IdP-init (which requires
  ``idp_init_default_return_to`` to be configured).
* Trust a LogoutRequest without a valid IdP signature (v1.2 SLO).
* Re-kill a session whose LogoutRequest ID was already seen (v1.2 SLO
  replay defence via ``SamlReplayCache``).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jose import JWTError, jwt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..database.engine import get_db
from ..database.models import ExternalIdentity, User
from . import client as _client_module
from .client import SamlClient, SamlNotConfiguredError
from .relay_state import RelayStateInvalid
from .relay_state import mint as relay_mint
from .relay_state import verify as relay_verify
from .replay_cache import SamlReplayCache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth/saml", tags=["Auth (SAML)"])

# Module-level singletons. Tests reset via the test-only helper at
# the bottom of this file. Mirrors the OIDC routes' get_oauth_client()
# pattern — single instance per process, lazy-initialised.
_saml_client: SamlClient | None = None
_replay_cache: SamlReplayCache = SamlReplayCache()


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def is_saml_enabled() -> bool:
    """True when the operator has set ``SAML_ENABLED=true`` in the env."""
    return os.environ.get("SAML_ENABLED", "").lower() == "true"


def get_saml_client() -> SamlClient:
    """Lazy-init the singleton SAML client + load IdP metadata.

    Raises ``SamlNotConfiguredError`` when required env vars are missing
    — the route layer translates that into a 503 so the operator sees a
    clear "you enabled SAML but didn't finish wiring it" diagnostic
    instead of a 500.
    """
    global _saml_client
    if _saml_client is None:
        config = _client_module.config_from_env()
        _saml_client = SamlClient(config)
        _saml_client.initialise()
    return _saml_client


def reset_saml_state_for_tests() -> None:
    """Drop the singletons. Tests call this in fixtures."""
    global _saml_client
    if _saml_client is not None:
        _saml_client.shutdown()
        _saml_client = None
    _replay_cache.clear()


def _idp_name() -> str:
    """The operator-supplied display name for the configured IdP.

    Defaults to ``"saml"`` when unset — the routes still work, but the
    ``ExternalIdentity.kind`` will be ``"saml:saml"`` which is ugly.
    Operators set this via ``SAML_IDP_NAME`` (matches the
    ``identityProviders[].name`` value from values.yaml).
    """
    return os.environ.get("SAML_IDP_NAME", "saml").strip() or "saml"


# ---------------------------------------------------------------------------
# Internal — JWT session minting (mirrors oidc_routes.py exactly)
# ---------------------------------------------------------------------------


def _create_access_token(user: User) -> str:
    settings = get_settings()
    expires = datetime.now(timezone.utc) + timedelta(
        minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES,
    )
    payload = {
        "sub": user.id,
        "email": user.email,
        "role": user.role,
        "type": "access",
        "exp": expires,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(
        payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM,
    )


def _attach_session_cookies(
    response: RedirectResponse, user: User,
) -> None:
    """Set the access-token cookie. Refresh-token plumbing for SAML
    sessions lands in PR-1b4 (auth.py extension)."""
    settings = get_settings()
    response.set_cookie(
        "access_token",
        _create_access_token(user),
        httponly=True,
        samesite="lax",
        max_age=60 * settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES,
    )


# ---------------------------------------------------------------------------
# Internal — OneLogin request_data shape
# ---------------------------------------------------------------------------


def _build_request_data(request: Request, form: dict[str, Any]) -> dict[str, Any]:
    """OneLogin SDK wants this exact dict shape, regardless of web framework.

    Form fields come pre-parsed because FastAPI ate them via ``Form()``;
    we hand the dict back to the SDK so it can read ``SAMLResponse``
    and ``RelayState``.
    """
    url = request.url
    return {
        "https": "on" if url.scheme == "https" else "off",
        "http_host": url.hostname or "",
        "server_port": str(url.port or (443 if url.scheme == "https" else 80)),
        "script_name": url.path,
        "get_data": dict(request.query_params),
        "post_data": form,
    }


# ---------------------------------------------------------------------------
# Cross-IdP collision guard (design decision #4)
# ---------------------------------------------------------------------------


async def _attach_or_reject_identity(
    db: AsyncSession, user: User, kind: str, subject: str,
) -> None:
    """Apply decision #4's three rules. Mutates ``db`` on success.

    Rules:
    1. Existing (kind, subject) row → idempotent re-login. No-op.
    2. ANY other identity row (any kind, any subject) → 409. Linking
       is admin-only in v1.1.
    3. Otherwise → insert the new (kind, subject) row.

    Why even same-kind / different-subject is rejected: two SAML IdPs
    both asserting the same email is the same hijack-via-email attack
    OIDC↔SAML conflation has, just within one protocol.
    """
    existing = (
        await db.execute(
            select(ExternalIdentity).where(ExternalIdentity.user_id == user.id),
        )
    ).scalars().all()

    for ident in existing:
        if ident.kind == kind and ident.subject == subject:
            # Rule 1 — idempotent.
            logger.info(
                "SAML re-login for user=%s kind=%s (existing identity reused)",
                user.email, kind,
            )
            return

    if existing:
        # Rule 2 — any other identity blocks. Surface enough detail in
        # the log for ops debugging without leaking it to the attacker
        # via the HTTP error body.
        logger.warning(
            "SAML linkage REJECTED — user=%s already has %d identities of "
            "kinds %s; new assertion kind=%s subject=%s",
            user.email, len(existing),
            sorted({i.kind for i in existing}), kind, subject,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Cross-IdP linkage requires admin confirmation. Contact "
                "your AIFactory administrator to link this identity."
            ),
        )

    # Rule 3 — clean slate, attach.
    db.add(ExternalIdentity(user_id=user.id, kind=kind, subject=subject))
    logger.info(
        "SAML first-time link for user=%s kind=%s", user.email, kind,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/login", summary="Begin SAML SP-init flow")
async def saml_login(request: Request, idp: str | None = None):
    """Redirect the browser to the IdP SSO URL with a fresh AuthnRequest.

    The ``idp`` query param identifies which configured IdP to use; in
    v1.1 we have at most one SAML IdP per release, so this is mostly
    forward-compatible — we still validate it against the configured
    name so a typo'd link fails loud instead of silently picking the
    default.
    """
    if not is_saml_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SAML SSO is not configured on this deployment",
        )

    configured_idp = _idp_name()
    # When the caller omits ?idp, default to the configured one.
    target_idp = (idp or configured_idp).strip()
    if target_idp != configured_idp:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown IdP '{target_idp}'",
        )

    try:
        client = get_saml_client()
    except SamlNotConfiguredError as exc:
        logger.error("SAML login attempted with incomplete config: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SAML is enabled but server-side configuration is incomplete",
        ) from exc

    settings = get_settings()
    return_to = client._config.idp_init_default_return_to or "/"

    relay = relay_mint(
        secret_key=settings.JWT_SECRET.encode("utf-8"),
        idp=target_idp,
        return_to=return_to,
    )

    # Build the AuthnRequest via OneLogin. Lazy import — same reason as
    # client.py (skip the C-extension load cost when SAML isn't used).
    from onelogin.saml2.auth import OneLogin_Saml2_Auth

    request_data = _build_request_data(request, form={})
    auth = OneLogin_Saml2_Auth(
        request_data, old_settings=client.build_settings_dict(),
    )
    sso_url = auth.login(return_to=relay, force_authn=False)

    logger.info(
        "SAML login initiated (idp=%s, return_to=%s)", target_idp, return_to,
    )
    return RedirectResponse(url=sso_url, status_code=status.HTTP_302_FOUND)


@router.post("/acs", summary="SAML Assertion Consumer Service")
async def saml_acs(
    request: Request,
    SAMLResponse: str = Form(...),
    RelayState: str | None = Form(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Process a signed SAML assertion + mint our session cookie.

    Trust pipeline (each step rejects on failure):

    1. ``OneLogin_Saml2_Auth.process_response()`` — signature, audience,
       recipient, NotOnOrAfter, XSW. Refusing here is the most common path.
    2. ``SamlReplayCache.check_and_add()`` — duplicate-assertion-id rejection.
    3. ``RelayState`` verification (SP-init only) — HMAC, expiry, ``idp``
       matches the configured one.
    4. User lookup by email — 403 if SCIM hasn't provisioned the row.
    5. Cross-IdP collision guard — 409 if another-kind identity exists.

    On success: insert/keep the ``ExternalIdentity`` row, stamp
    ``users.last_login_at``, set the access-token cookie, 302 to
    ``return_to``.
    """
    if not is_saml_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SAML SSO is not configured on this deployment",
        )

    try:
        client = get_saml_client()
    except SamlNotConfiguredError as exc:
        logger.error("SAML ACS hit with incomplete config: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SAML is enabled but server-side configuration is incomplete",
        ) from exc

    # ----- 1. SDK validation -----
    from onelogin.saml2.auth import OneLogin_Saml2_Auth

    form_data = {"SAMLResponse": SAMLResponse}
    if RelayState is not None:
        form_data["RelayState"] = RelayState

    request_data = _build_request_data(request, form=form_data)
    auth = OneLogin_Saml2_Auth(
        request_data, old_settings=client.build_settings_dict(),
    )

    try:
        auth.process_response()
    except Exception as exc:
        # OneLogin raises a variety of exception subclasses; any of them
        # means an attacker-controlled or malformed payload reached us.
        logger.warning(
            "SAML ACS: SDK rejected response (%s)", type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SAML response rejected",
        ) from exc

    errors = auth.get_errors()
    if errors:
        # Log details (which include error code names — safe), respond
        # with a generic message (reflected-XSS defence; same as OIDC).
        logger.warning("SAML ACS: validation errors %s", errors)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SAML response rejected",
        )

    if not auth.is_authenticated():
        logger.warning("SAML ACS: assertion processed but not authenticated")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SAML response rejected",
        )

    nameid = (auth.get_nameid() or "").strip()
    if not nameid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SAML response missing NameID",
        )

    # ----- 2. Replay defence -----
    assertion_id = auth.get_last_assertion_id() or ""
    not_on_or_after = auth.get_last_assertion_not_on_or_after()
    if not assertion_id or not_on_or_after is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SAML response missing assertion identifiers",
        )

    if not _replay_cache.check_and_add(assertion_id, float(not_on_or_after)):
        logger.warning(
            "SAML ACS: replay rejected (assertion_id=%s)", assertion_id,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SAML response rejected (replay)",
        )

    # ----- 3. RelayState verification (SP-init only) -----
    return_to: str
    settings = get_settings()
    if RelayState:
        try:
            payload = relay_verify(
                secret_key=settings.JWT_SECRET.encode("utf-8"),
                token=RelayState,
            )
        except RelayStateInvalid as exc:
            logger.warning(
                "SAML ACS: RelayState rejected (%s)", str(exc),
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="SAML RelayState rejected",
            ) from exc

        if payload.idp != _idp_name():
            logger.warning(
                "SAML ACS: RelayState idp=%s does not match configured %s",
                payload.idp, _idp_name(),
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="SAML RelayState rejected",
            )

        return_to = payload.return_to
    else:
        # IdP-init flow. Operator MUST have configured a default landing
        # URL; otherwise we have no safe place to send the user.
        default = client._config.idp_init_default_return_to
        if not default:
            logger.warning(
                "SAML ACS: IdP-init received but "
                "SAML_IDP_INIT_DEFAULT_RETURN_TO is unset — rejecting",
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="IdP-init SAML flow is not permitted on this deployment",
            )
        return_to = default

    # ----- 4. User lookup by email -----
    # Case-insensitive — IdPs occasionally normalise case. ``users.email``
    # is stored as the operator's preferred casing; we lower() both sides.
    user_row = (
        await db.execute(
            select(User).where(func.lower(User.email) == nameid.lower()),
        )
    ).scalar_one_or_none()

    if user_row is None:
        # Decision #4: do NOT auto-create. SCIM is expected to have
        # provisioned the row first. Surface a 403 with operator-
        # friendly text so the user knows who to ping.
        logger.warning(
            "SAML ACS: user '%s' not provisioned — SCIM must create first",
            nameid,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User not provisioned. Contact your administrator.",
        )

    # ----- 5. Cross-IdP collision guard + attach -----
    new_kind = f"saml:{_idp_name()}"
    await _attach_or_reject_identity(db, user_row, new_kind, nameid)

    # Epic #35 #43 stamp — last successful login for the access-review
    # report. Same pattern as the OIDC callback.
    user_row.last_login_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()

    redirect = RedirectResponse(
        url=return_to, status_code=status.HTTP_302_FOUND,
    )
    _attach_session_cookies(redirect, user_row)

    logger.info(
        "SAML login OK user=%s idp=%s return_to=%s",
        user_row.email, _idp_name(), return_to,
    )
    return redirect


@router.get("/metadata", summary="Public SP metadata XML")
async def saml_metadata():
    """Return the SP metadata XML the operator uploads at the IdP.

    Cacheable; idempotent; safe to expose without auth (the IdP fetches
    this from a public URL by design).

    When ``SAML_SLO_ENABLED=true`` the metadata XML includes a
    ``<SingleLogoutService>`` element advertising the HTTP-POST binding
    for the ``/sls`` endpoint (v1.2 decision D-6).
    """
    if not is_saml_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SAML SSO is not configured on this deployment",
        )

    try:
        client = get_saml_client()
    except SamlNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SAML is enabled but server-side configuration is incomplete",
        ) from exc

    from onelogin.saml2.settings import OneLogin_Saml2_Settings

    settings_obj = OneLogin_Saml2_Settings(
        client.build_settings_dict(), sp_validation_only=True,
    )
    metadata = settings_obj.get_sp_metadata()
    validation_errors = settings_obj.validate_metadata(metadata)
    if validation_errors:
        logger.error(
            "SAML metadata validation failed: %s", validation_errors,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SP metadata is invalid",
        )

    return Response(content=metadata, media_type="text/xml")


# ---------------------------------------------------------------------------
# SLO helpers (v1.2 — Epic #35 #209)
# ---------------------------------------------------------------------------


def is_slo_enabled() -> bool:
    """True when the operator has set ``SAML_SLO_ENABLED=true``."""
    return os.environ.get("SAML_SLO_ENABLED", "").lower() == "true"


def _read_nameid_from_cookie(request: Request) -> str | None:
    """Extract the ``email`` claim (= SAML NameID) from the access-token cookie.

    Returns None when the cookie is absent, expired, or malformed. The
    caller decides whether None is acceptable (IdP-init can proceed without
    a live cookie; SP-init requires one).
    """
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        settings = get_settings()
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
        )
        return payload.get("email")
    except JWTError:
        # Expired or invalid token — still let the logout proceed
        # (the user may be trying to clean up an already-expired session).
        return None


def _clear_session_cookies(response: Response) -> None:
    """Delete the access-token and refresh-token HTTP-only cookies.

    Uses ``max_age=0`` which is compatible with all browsers; the
    ``delete_cookie`` helper also sends the correct ``Set-Cookie`` header
    with an epoch expiry. The path + domain must match what was set on
    login or the browser will not delete them.
    """
    response.delete_cookie("access_token", httponly=True, samesite="lax")
    response.delete_cookie("refresh_token", httponly=True, samesite="lax")


# ---------------------------------------------------------------------------
# SLO routes (v1.2)
# ---------------------------------------------------------------------------


@router.post("/logout", summary="SP-initiated SAML SLO")
async def saml_logout(request: Request):
    """Begin a SP-initiated Single Logout.

    1. Read the user's NameID (email) from the access-token cookie.
       Returns 401 when no authenticated session is present — there is
       nothing to log out from.
    2. Mint an HMAC-signed RelayState carrying {nameid, nonce, exp}
       so the SLS callback can detect tampering.
    3. Build a signed SAML LogoutRequest via the OneLogin SDK
       (``auth.logout()``).
    4. Redirect the browser to the IdP's SLO URL.

    Returns 404 when ``SAML_SLO_ENABLED`` is false (default; backward compat).
    Returns 503 when SAML config is incomplete.
    Returns 503 when the IdP metadata does not advertise a SLO endpoint and
    no ``SAML_IDP_SLO_URL`` override is set.
    """
    if not is_saml_enabled() or not is_slo_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SAML SLO is not configured on this deployment",
        )

    try:
        client = get_saml_client()
    except SamlNotConfiguredError as exc:
        logger.error("SAML /logout attempted with incomplete config: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SAML is enabled but server-side configuration is incomplete",
        ) from exc

    # Require an authenticated session — we need the NameID to build the
    # LogoutRequest. Without it the IdP cannot match which session to kill.
    nameid = _read_nameid_from_cookie(request)
    if not nameid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No active SAML session",
        )

    settings = get_settings()
    relay = relay_mint(
        secret_key=settings.JWT_SECRET.encode("utf-8"),
        idp=_idp_name(),
        return_to="/",  # post-logout landing page; the SLS handler uses this
    )

    from onelogin.saml2.auth import OneLogin_Saml2_Auth

    request_data = _build_request_data(request, form={})
    auth = OneLogin_Saml2_Auth(
        request_data, old_settings=client.build_settings_dict(),
    )

    try:
        slo_redirect = auth.logout(
            return_to=relay,
            name_id=nameid,
            name_id_format=(
                "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"
            ),
        )
    except Exception as exc:
        # OneLogin raises when the IdP metadata has no SLO URL and
        # SAML_IDP_SLO_URL override is also absent.
        logger.error(
            "SAML /logout: SDK could not build LogoutRequest (%s: %s)",
            type(exc).__name__, exc,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "SAML SLO is enabled but the IdP does not advertise a "
                "SingleLogoutService URL. Check saml.slo.idpSloUrl in "
                "Helm values or the IdP metadata."
            ),
        ) from exc

    logger.info(
        "SAML SP-init SLO: redirecting user nameid=%s to IdP SLO", nameid,
    )
    return RedirectResponse(url=slo_redirect, status_code=status.HTTP_302_FOUND)


@router.post("/sls", summary="SAML Single Logout Service")
async def saml_sls(
    request: Request,
    SAMLResponse: str | None = Form(default=None),
    SAMLRequest: str | None = Form(default=None),
    RelayState: str | None = Form(default=None),
):
    """Single Logout Service — handles both SP-init confirmation and IdP-init.

    **SP-init confirmation** (browser returns from IdP after we initiated SLO):
    - ``SAMLResponse`` is present, ``RelayState`` is our HMAC-signed token.
    - Validate signature + status. Verify RelayState HMAC. Clear cookies.
    - 302 to the login page.

    **IdP-init** (IdP sends a ``LogoutRequest`` to terminate a session):
    - ``SAMLRequest`` is present.
    - Validate signature + audience + replay.
    - Clear session cookies in the HTTP response.
    - Return a signed ``LogoutResponse`` (auto-submit POST form or redirect).
    - Uses ``NoSession`` status when the SP has no live session for the
      NameID (per decision D-2 — never return 4xx for unknown NameID).

    Returns 404 when ``SAML_SLO_ENABLED`` is false.
    Returns 400 on any validation failure (tampered RelayState, bad signature,
    replay, missing required fields).
    """
    if not is_saml_enabled() or not is_slo_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SAML SLO is not configured on this deployment",
        )

    try:
        client = get_saml_client()
    except SamlNotConfiguredError as exc:
        logger.error("SAML /sls attempted with incomplete config: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SAML is enabled but server-side configuration is incomplete",
        ) from exc

    from onelogin.saml2.auth import OneLogin_Saml2_Auth

    # The OneLogin SDK reads SAMLResponse / SAMLRequest from ``get_data``
    # (query params) or ``post_data`` depending on binding. For HTTP-POST
    # (our only supported binding) both arrive as form fields; we put them
    # in ``post_data`` AND mirror them in ``get_data`` because process_slo()
    # reads from ``get_data`` internally.
    post_form: dict[str, str] = {}
    if SAMLResponse is not None:
        post_form["SAMLResponse"] = SAMLResponse
    if SAMLRequest is not None:
        post_form["SAMLRequest"] = SAMLRequest
    if RelayState is not None:
        post_form["RelayState"] = RelayState

    # process_slo reads from get_data, not post_data, for the SAMLRequest/
    # SAMLResponse fields even on HTTP-POST binding. Mirror them so the SDK
    # finds them. See onelogin/saml2/auth.py:process_slo source.
    request_data = _build_request_data(request, form=post_form)
    request_data["get_data"] = dict(post_form)

    auth = OneLogin_Saml2_Auth(
        request_data, old_settings=client.build_settings_dict(),
    )

    # -------------------------------------------------------------------
    # SP-init confirmation path: SAMLResponse present, no SAMLRequest
    # -------------------------------------------------------------------
    if SAMLResponse is not None and SAMLRequest is None:
        # Validate RelayState HMAC BEFORE touching the SDK — a tampered
        # RelayState means we can't trust where to redirect the user.
        if not RelayState:
            logger.warning("SAML SLS: SP-init SAMLResponse received without RelayState")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="SAML SLO response missing RelayState",
            )

        settings = get_settings()
        try:
            relay_payload = relay_verify(
                secret_key=settings.JWT_SECRET.encode("utf-8"),
                token=RelayState,
            )
        except RelayStateInvalid as exc:
            logger.warning("SAML SLS: RelayState rejected (%s)", exc)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="SAML SLO RelayState rejected",
            ) from exc

        if relay_payload.idp != _idp_name():
            logger.warning(
                "SAML SLS: RelayState idp=%s does not match configured %s",
                relay_payload.idp, _idp_name(),
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="SAML SLO RelayState rejected",
            )

        # SDK validates the LogoutResponse signature + status.
        try:
            redirect_url = auth.process_slo(
                keep_local_session=False,
                request_id=None,
            )
        except Exception as exc:
            logger.warning(
                "SAML SLS: SDK rejected LogoutResponse (%s)", type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="SAML SLO response rejected",
            ) from exc

        slo_errors = auth.get_errors()
        if slo_errors:
            logger.warning("SAML SLS: LogoutResponse errors: %s", slo_errors)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="SAML SLO response rejected",
            )

        # Session is confirmed cleared at IdP. Clear our cookies.
        login_page = "/login"
        response = RedirectResponse(
            url=login_page, status_code=status.HTTP_302_FOUND,
        )
        _clear_session_cookies(response)
        logger.info("SAML SLS: SP-init SLO complete; session cleared")
        return response

    # -------------------------------------------------------------------
    # IdP-init path: SAMLRequest (LogoutRequest) present
    # -------------------------------------------------------------------
    if SAMLRequest is not None:
        # The SDK validates the LogoutRequest signature, audience, and
        # NotOnOrAfter inside process_slo(). Any failure appends to errors.
        try:
            idp_redirect_url = auth.process_slo(
                keep_local_session=True,  # we handle session kill ourselves
                request_id=None,
            )
        except Exception as exc:
            logger.warning(
                "SAML SLS: SDK rejected LogoutRequest (%s)", type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="SAML SLO request rejected",
            ) from exc

        slo_errors = auth.get_errors()
        if slo_errors:
            logger.warning("SAML SLS: LogoutRequest errors: %s", slo_errors)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="SAML SLO request rejected",
            )

        # Replay defence: reject if we've seen this LogoutRequest ID before.
        logout_request_id = auth.get_last_message_id() or ""
        if not logout_request_id:
            logger.warning("SAML SLS: IdP-init LogoutRequest missing ID")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="SAML SLO request missing ID",
            )

        # Use a long-lived TTL for LogoutRequests since we don't know their
        # NotOnOrAfter — 1 hour is generous and bounds the replay window.
        _LOGOUT_REQUEST_REPLAY_TTL = 3600.0
        import time as _time
        exp_epoch = _time.time() + _LOGOUT_REQUEST_REPLAY_TTL
        if not _replay_cache.check_and_add(logout_request_id, exp_epoch):
            logger.warning(
                "SAML SLS: replay rejected for LogoutRequest id=%s",
                logout_request_id,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="SAML SLO request rejected (replay)",
            )

        # Kill the SP session: clear cookies regardless of whether the
        # NameID matches a live cookie. Per D-2: we use the HTTP response
        # to clear the cookies and return the LogoutResponse body.
        #
        # ``idp_redirect_url`` is the auto-submit POST form HTML that the
        # SDK built (containing the signed LogoutResponse). Return it as an
        # HTML response so the browser auto-submits it back to the IdP,
        # completing the SLO propagation chain.
        #
        # If the SDK did not produce a redirect URL (IdP metadata has no
        # SLO response URL), fall back to a plain 200 with empty body — the
        # IdP will interpret the response as a successful SLO acknowledgement.
        logger.info(
            "SAML SLS: IdP-init SLO complete for request id=%s", logout_request_id,
        )

        if idp_redirect_url:
            # The SDK returned a redirect URL or an HTML auto-submit form.
            # Detect which: a URL starts with "http"; an HTML form starts
            # with "<" (or whitespace). Use HTMLResponse for the form case
            # so the browser renders and auto-submits it.
            if idp_redirect_url.strip().startswith("<"):
                resp = HTMLResponse(content=idp_redirect_url)
            else:
                resp = RedirectResponse(
                    url=idp_redirect_url,
                    status_code=status.HTTP_302_FOUND,
                )
            _clear_session_cookies(resp)
            return resp

        # Fallback: no IdP SLO response URL in metadata. Cookies cleared,
        # return 200 so the IdP sees a success signal (not a 4xx failure).
        resp = Response(content="", status_code=status.HTTP_200_OK)
        _clear_session_cookies(resp)
        return resp

    # Neither SAMLResponse nor SAMLRequest — malformed request.
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="SAML SLS requires SAMLResponse or SAMLRequest",
    )
