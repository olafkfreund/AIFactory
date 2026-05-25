"""P3 — OIDC SSO acceptance tests.

Six tests map directly to the six acceptance bullets in Epic #26
issue #30. As implementation chunks land, the ``@pytest.mark.skip``
decorator is removed from the relevant test and a real body replaces
the ``pytest.fail`` placeholder.

Status:
  - P3.1 (Keycloak login happy path) — test_login_callback_pkce_roundtrip GREEN
  - P3.2..P3.5 — still skipped (each names the chunk that will flip it)
"""

from __future__ import annotations

import os
from urllib.parse import parse_qs, urlparse

import pytest

from tests.oidc.helpers import (
    authlib_available,
    keycloak_drive_login_url,
    reimport_oidc,
)


# Keycloak realm config (mirrors tests/oidc/fixtures/keycloak-realm.json).
TEST_USER_EMAIL = "alice@example.com"
TEST_USER_NAME = "Alice Example"
TEST_USER_USERNAME = "alice"
TEST_USER_PASSWORD = "alice-test-pass"


def _build_test_app():
    """Construct a fresh FastAPI app instance with OIDC env wired.

    Avoids the cached singleton in main.py's ``create_app()`` so each
    test can drive its own env config.
    """
    # The web-server module path is on sys.path via tests/oidc/helpers.
    # Re-import the OIDC layer with current env so client.py picks up
    # the fixture-provided issuer/secret.
    reimport_oidc({
        "APP_OIDC_ENABLED": "true",
        "APP_OIDC_ISSUER_URL": os.environ["OIDC_ISSUER_URL"],
        "APP_OIDC_CLIENT_ID": os.environ["OIDC_CLIENT_ID"],
        "APP_OIDC_CLIENT_SECRET": os.environ["OIDC_CLIENT_SECRET"],
    })

    # Build a minimal app with only what OIDC needs — avoids importing
    # the full main.py which pulls in DB engine, graphiti, etc.
    from fastapi import FastAPI
    from starlette.middleware.sessions import SessionMiddleware

    from server.routes import oidc_routes

    app = FastAPI()
    app.add_middleware(
        SessionMiddleware,
        secret_key="test-session-secret-for-p3-oidc",
        session_cookie="aif_oidc_session",
        max_age=600,
        same_site="lax",
        https_only=False,
    )
    app.include_router(oidc_routes.router)
    return app


@pytest.mark.oidc
@pytest.mark.slow
@pytest.mark.skipif(not authlib_available(), reason="authlib not installed")
def test_login_callback_pkce_roundtrip(oidc_issuer_url, oidc_client_id) -> None:
    """Authorization Code + PKCE end-to-end against Keycloak.

    Flow:
      1. Test client GET /api/auth/oidc/login
         → expect 302 to Keycloak with state + code_challenge in query.
      2. Drive Keycloak's HTML login form headlessly with the test user;
         Keycloak issues an auth code.
      3. Test client GET /api/auth/oidc/callback?code=...&state=...
         → server retrieves stashed PKCE verifier from its Starlette
         session, exchanges the code with Keycloak, JIT-provisions a
         User, and 302s back to "/" with HTTP-only access_token cookie.
      4. Assert the cookie is set and decodes as a valid internal JWT
         carrying the test user's email + role.
    """
    # Build a sync TestClient so cookie persistence works trivially.
    from fastapi.testclient import TestClient
    from jose import jwt

    from server.config import get_settings

    # Each test gets a fresh app — get_settings() is itself a singleton
    # but JWT_SECRET is stable across the run.
    app = _build_test_app()

    # IMPORTANT: For Keycloak to accept the callback redirect_uri, the
    # realm must whitelist it. Our realm fixture includes "*", so any
    # URI works. The TestClient's base URL is "http://testserver".
    with TestClient(app, follow_redirects=False) as client:
        # Step 1: kick off OIDC login.
        resp = client.get("/api/auth/oidc/login")
        assert resp.status_code in (302, 307), (
            f"/login should 302 to Keycloak; got {resp.status_code} "
            f"body={resp.text[:300]!r}"
        )
        kc_url = resp.headers["location"]
        parsed = urlparse(kc_url)
        qs = parse_qs(parsed.query)
        assert qs.get("response_type") == ["code"], "expected response_type=code"
        assert qs.get("client_id") == [oidc_client_id]
        assert "code_challenge" in qs, "PKCE code_challenge missing from auth URL"
        assert qs.get("code_challenge_method") == ["S256"]
        assert "state" in qs, "state nonce missing from auth URL"

        state = qs["state"][0]

        # Step 2: drive Keycloak's login form using the COMPLETE auth URL
        # so every param (nonce, code_challenge, redirect_uri, etc.)
        # authlib added is preserved.
        code = keycloak_drive_login_url(
            auth_url=kc_url,
            username=TEST_USER_USERNAME,
            password=TEST_USER_PASSWORD,
        )

        # Step 3: complete the callback. The TestClient still carries
        # the SessionMiddleware cookie from step 1, which holds the
        # PKCE verifier authlib needs for the token exchange.
        resp = client.get(
            f"/api/auth/oidc/callback?code={code}&state={state}"
        )

        assert resp.status_code in (302, 307), (
            f"/callback should redirect on success; got {resp.status_code} "
            f"body={resp.text[:500]!r}"
        )
        assert resp.headers["location"] == "/", (
            f"expected redirect to '/'; got {resp.headers['location']!r}"
        )

        # Step 4: assert the access token cookie is valid.
        access_cookie = client.cookies.get("access_token")
        assert access_cookie, "access_token cookie not set after callback"

        settings = get_settings()
        payload = jwt.decode(
            access_cookie,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
        )
        assert payload["type"] == "access"
        assert payload["email"] == TEST_USER_EMAIL
        assert payload["role"] in {"member", "admin"}  # JIT default

        # Verify a User row was JIT-provisioned for the OIDC sub.
        # (We don't query the DB directly here — that's covered in
        # test_jit_provisions_user_and_org_member in P3.3.)


@pytest.mark.oidc
@pytest.mark.slow
@pytest.mark.skipif(not authlib_available(), reason="authlib not installed")
def test_pkce_state_tamper_rejected(oidc_issuer_url, oidc_client_id) -> None:
    """Tampering the ``state`` parameter at /callback must raise.

    Drive the normal flow up to the moment we'd hit /callback, then
    swap the state value before submission. authlib's
    authorize_access_token raises a state-mismatch error; our handler
    returns 400 with a generic "OIDC callback rejected" message and
    NEVER echoes the tampered state back (reflected-XSS defense).
    No session cookie is set.
    """
    from fastapi.testclient import TestClient

    app = _build_test_app()

    with TestClient(app, follow_redirects=False) as client:
        # Begin the OIDC flow normally.
        resp = client.get("/api/auth/oidc/login")
        assert resp.status_code in (302, 307)
        kc_url = resp.headers["location"]
        parsed = urlparse(kc_url)
        qs = parse_qs(parsed.query)
        original_state = qs["state"][0]

        # Get an auth code from Keycloak — this code is bound to the
        # *original* state (and PKCE verifier in the session).
        code = keycloak_drive_login_url(
            auth_url=kc_url,
            username=TEST_USER_USERNAME,
            password=TEST_USER_PASSWORD,
        )

        # Now tamper: replace state with attacker-controlled value.
        tampered_state = "attacker-injected-state-value-xxxxxxxxx"
        assert tampered_state != original_state, "fixture must use a distinct value"

        resp = client.get(
            f"/api/auth/oidc/callback?code={code}&state={tampered_state}"
        )
        assert resp.status_code == 400, (
            f"tampered state must be rejected; got {resp.status_code} "
            f"body={resp.text[:300]!r}"
        )

        # Defense-in-depth: the response body must NOT echo back the
        # tampered state value (would create a reflected-XSS sink).
        assert tampered_state not in resp.text, (
            "response body must not echo tampered state (reflected-XSS defense)"
        )

        # No session was minted.
        assert client.cookies.get("access_token") is None
        assert client.cookies.get("refresh_token") is None


@pytest.mark.oidc
@pytest.mark.slow
@pytest.mark.skipif(not authlib_available(), reason="authlib not installed")
@pytest.mark.skip(reason="P3.3 implementation pending: JIT user provisioning")
def test_jit_provisions_user_and_org_member(oidc_issuer_url, oidc_client_id) -> None:
    """First login from an unknown ``sub`` creates a User + OrganizationMember."""
    pytest.fail("P3.3 not landed")


@pytest.mark.oidc
@pytest.mark.slow
@pytest.mark.skipif(not authlib_available(), reason="authlib not installed")
@pytest.mark.skip(reason="P3.4 implementation pending: userinfo cache + JWT TTL")
def test_userinfo_cache_avoids_per_request_rtt(oidc_issuer_url, oidc_client_id) -> None:
    """N API calls within one refresh window hit the userinfo cache."""
    pytest.fail("P3.4 not landed")


@pytest.mark.oidc
@pytest.mark.slow
@pytest.mark.skipif(not authlib_available(), reason="authlib not installed")
@pytest.mark.skip(reason="P3.4 implementation pending: revocation within TTL")
def test_user_disabled_in_idp_revoked_within_ttl(oidc_issuer_url, oidc_client_id) -> None:
    """Disabling a user in the IdP revokes access on the next refresh."""
    pytest.fail("P3.4 not landed")


@pytest.mark.oidc
@pytest.mark.slow
@pytest.mark.skipif(not authlib_available(), reason="authlib not installed")
@pytest.mark.skip(reason="P3.5 implementation pending: logout flow")
def test_logout_redirects_to_end_session_endpoint(oidc_issuer_url, oidc_client_id) -> None:
    """``POST /api/auth/oidc/logout`` redirects to the IdP's end_session_endpoint."""
    pytest.fail("P3.5 not landed")
