"""SAML 2.0 Single Logout (SLO) tests (Epic #35 #209 v1.2).

These tests follow the exact pattern of ``test_routes.py`` — mock the
OneLogin SDK at its module boundary, use a per-test in-memory SQLite DB,
and exercise OUR code paths: RelayState verification, replay cache,
cookie clearing, NoSession response for unknown NameIDs.

What we test:
  * SP-init: /logout with a valid session cookie → 302 to IdP SLO URL
    with signed RelayState.
  * SP-init: /logout without a session cookie → 401.
  * SP-init: /sls with SAMLResponse + matching RelayState → cookies
    cleared + 302 to login.
  * SP-init: /sls with tampered RelayState → 400.
  * IdP-init: /sls with SAMLRequest + NameID matches live session →
    session cleared.
  * IdP-init: /sls with SAMLRequest for unknown NameID (no live cookie)
    → 200 with LogoutResponse (NoSession) rather than 4xx.
  * IdP-init: /sls with SAMLRequest replay → second submission rejected
    (400).
  * SLO disabled: /logout and /sls return 404.
"""

from __future__ import annotations

import asyncio
import os
import secrets as _test_secrets
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_SERVER_ROOT = REPO_ROOT / "apps" / "web-server"
if str(WEB_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(WEB_SERVER_ROOT))


# ---------------------------------------------------------------------------
# DB + app fixture (mirrors test_routes.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_db():
    """Per-test in-memory SQLite. Returns (engine, SessionLocal)."""
    from server.database.models import Base

    nonce = _test_secrets.token_hex(8)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///file:saml-slo-{nonce}"
        "?mode=memory&cache=shared&uri=true",
    )

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_init())
    finally:
        loop.close()

    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    return engine, SessionLocal


@pytest.fixture(autouse=True)
def _clean_saml_env(monkeypatch, tmp_path, idp_metadata_xml):
    """Reset all SAML env vars + the routes-module singletons between tests."""
    for key in list(os.environ):
        if key.startswith("SAML_"):
            monkeypatch.delenv(key, raising=False)

    metadata_path = tmp_path / "idp-metadata.xml"
    metadata_path.write_text(idp_metadata_xml, encoding="utf-8")
    monkeypatch.setenv("SAML_IDP_METADATA_FILE", str(metadata_path))

    from server import config as _config

    _orig_secret = _config.settings.JWT_SECRET
    _orig_alg = _config.settings.JWT_ALGORITHM
    _config.settings.JWT_SECRET = "test-jwt-secret-32-bytes-minimum-len!"
    _config.settings.JWT_ALGORITHM = "HS256"

    from server.saml import routes as _routes

    _routes.reset_saml_state_for_tests()
    yield
    _routes.reset_saml_state_for_tests()
    _config.settings.JWT_SECRET = _orig_secret
    _config.settings.JWT_ALGORITHM = _orig_alg


@pytest.fixture
def saml_slo_enabled(monkeypatch, sp_cert_pem, sp_key_pem, tmp_path):
    """SAML + SLO both enabled with a file-based config."""
    sp_cert = tmp_path / "sp.crt"
    sp_cert.write_text(sp_cert_pem, encoding="utf-8")
    sp_key = tmp_path / "sp.key"
    sp_key.write_text(sp_key_pem, encoding="utf-8")

    monkeypatch.setenv("SAML_ENABLED", "true")
    monkeypatch.setenv("SAML_SP_ENTITY_ID", "https://test-sp.example.com/saml")
    monkeypatch.setenv("SAML_ACS_URL", "https://test-sp.example.com/api/auth/saml/acs")
    monkeypatch.setenv("SAML_SLO_URL", "https://test-sp.example.com/api/auth/saml/sls")
    monkeypatch.setenv("SAML_SP_CERT_FILE", str(sp_cert))
    monkeypatch.setenv("SAML_SP_KEY_FILE", str(sp_key))
    monkeypatch.setenv("SAML_IDP_NAME", "corp-sso")
    monkeypatch.setenv(
        "SAML_IDP_INIT_DEFAULT_RETURN_TO",
        "https://test-sp.example.com/dashboard",
    )
    monkeypatch.setenv("SAML_SLO_ENABLED", "true")


def _make_app(fresh_db) -> FastAPI:
    from server.database.engine import get_db
    from server.saml import routes as saml_routes

    _engine, SessionLocal = fresh_db

    app = FastAPI()
    app.include_router(saml_routes.router)

    async def _override_get_db():
        async with SessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    return app


def _make_access_token(email: str) -> str:
    """Mint a test access_token cookie value for the given email."""
    payload = {
        "sub": "test-user-id",
        "email": email,
        "role": "user",
        "type": "access",
        "exp": int(time.time()) + 900,
        "iat": int(time.time()),
    }
    return jwt.encode(
        payload,
        "test-jwt-secret-32-bytes-minimum-len!",
        algorithm="HS256",
    )


def _mint_valid_relay_state() -> str:
    from server.saml.relay_state import mint

    return mint(
        secret_key=b"test-jwt-secret-32-bytes-minimum-len!",
        idp="corp-sso",
        return_to="/",
    )


# ---------------------------------------------------------------------------
# Mock OneLogin SDK (SLO-specific methods added to the existing _FakeAuth)
# ---------------------------------------------------------------------------


class _FakeSloAuth:
    """Stand-in for OneLogin_Saml2_Auth for SLO routes.

    Class-level state lets tests configure per-scenario behaviour.
    """

    # SP-init /logout
    logout_url: str = "https://idp.example.com/slo?SAMLRequest=fake&RelayState="
    logout_raises: bool = False

    # SLS — SAMLResponse (SP-init confirmation)
    slo_response_errors: list[str] = []
    slo_response_raises: bool = False

    # SLS — SAMLRequest (IdP-init)
    slo_request_errors: list[str] = []
    slo_request_raises: bool = False
    slo_request_idp_redirect: str | None = None
    slo_last_message_id: str = "_logout-request-id-default"

    def __init__(self, request_data, old_settings=None):
        self._request_data = request_data
        self._errors: list[str] = []
        self._last_message_id: str = _FakeSloAuth.slo_last_message_id

    def logout(
        self,
        return_to=None,
        name_id=None,
        session_index=None,
        nq=None,
        name_id_format=None,
        spnq=None,
    ) -> str:
        if _FakeSloAuth.logout_raises:
            raise RuntimeError("simulated: IdP has no SLO URL")
        # Embed the RelayState in the returned URL so our test can inspect it.
        return f"{_FakeSloAuth.logout_url}{return_to or ''}"

    def process_slo(self, keep_local_session=False, request_id=None, delete_session_cb=None):
        # Determine whether we're handling SAMLResponse or SAMLRequest based
        # on what's in get_data.
        get_data = self._request_data.get("get_data", {})
        if "SAMLRequest" in get_data:
            if _FakeSloAuth.slo_request_raises:
                raise RuntimeError("simulated SDK SAMLRequest rejection")
            self._errors = list(_FakeSloAuth.slo_request_errors)
            return _FakeSloAuth.slo_request_idp_redirect
        else:
            if _FakeSloAuth.slo_response_raises:
                raise RuntimeError("simulated SDK SAMLResponse rejection")
            self._errors = list(_FakeSloAuth.slo_response_errors)
            return None

    def get_errors(self) -> list[str]:
        return self._errors

    def get_last_message_id(self) -> str:
        return self._last_message_id


@pytest.fixture(autouse=True)
def _patch_onelogin_slo(monkeypatch):
    """Replace OneLogin_Saml2_Auth for SLO route tests."""
    import onelogin.saml2.auth as _auth_mod

    monkeypatch.setattr(_auth_mod, "OneLogin_Saml2_Auth", _FakeSloAuth)

    # Reset class-level defaults so tests are isolated.
    _FakeSloAuth.logout_url = "https://idp.example.com/slo?SAMLRequest=fake&RelayState="
    _FakeSloAuth.logout_raises = False
    _FakeSloAuth.slo_response_errors = []
    _FakeSloAuth.slo_response_raises = False
    _FakeSloAuth.slo_request_errors = []
    _FakeSloAuth.slo_request_raises = False
    _FakeSloAuth.slo_request_idp_redirect = None
    _FakeSloAuth.slo_last_message_id = f"_id-{_test_secrets.token_hex(8)}"
    yield


# ---------------------------------------------------------------------------
# SLO disabled tests
# ---------------------------------------------------------------------------


def test_logout_404_when_slo_disabled(fresh_db, monkeypatch):
    """When SAML_SLO_ENABLED is false (default), /logout returns 404."""
    monkeypatch.setenv("SAML_ENABLED", "true")
    # Deliberately NOT setting SAML_SLO_ENABLED.
    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = client.post("/api/auth/saml/logout")
    assert resp.status_code == 404


def test_sls_404_when_slo_disabled(fresh_db, monkeypatch):
    """When SAML_SLO_ENABLED is false (default), /sls returns 404."""
    monkeypatch.setenv("SAML_ENABLED", "true")
    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/saml/sls",
            data={"SAMLResponse": "dummy"},
        )
    assert resp.status_code == 404


def test_logout_404_when_saml_disabled(fresh_db):
    """When SAML_ENABLED is false entirely, /logout returns 404."""
    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = client.post("/api/auth/saml/logout")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# SP-init /logout tests
# ---------------------------------------------------------------------------


def test_logout_redirects_to_idp_with_relay_state(fresh_db, saml_slo_enabled):
    """Valid session cookie → 302 to IdP SLO URL with signed RelayState."""
    app = _make_app(fresh_db)
    token = _make_access_token("alice@corp.example.com")

    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/saml/logout",
            cookies={"access_token": token},
            follow_redirects=False,
        )

    assert resp.status_code == 302
    location = resp.headers["location"]
    # Verify the IdP SLO URL prefix is in the redirect.
    assert "idp.example.com/slo" in location
    # Verify the RelayState is present and HMAC-signed (contains a ".").
    assert "RelayState=" in location
    relay_part = location.split("RelayState=", 1)[1]
    assert "." in relay_part, "RelayState should be HMAC-signed (payload.sig)"


def test_logout_401_without_session_cookie(fresh_db, saml_slo_enabled):
    """No session cookie → 401 (nothing to log out from)."""
    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/saml/logout",
            follow_redirects=False,
        )
    assert resp.status_code == 401
    assert "session" in resp.json()["detail"].lower()


def test_logout_503_when_idp_has_no_slo_url(fresh_db, saml_slo_enabled):
    """IdP metadata has no SingleLogoutService → SDK raises → 503."""
    _FakeSloAuth.logout_raises = True
    app = _make_app(fresh_db)
    token = _make_access_token("alice@corp.example.com")

    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/saml/logout",
            cookies={"access_token": token},
        )
    assert resp.status_code == 503
    assert "SLO" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# SLS — SP-init confirmation (SAMLResponse from IdP)
# ---------------------------------------------------------------------------


def test_sls_sp_init_confirmation_clears_cookies(fresh_db, saml_slo_enabled):
    """Valid SAMLResponse + valid RelayState → cookies cleared + 302 to /login."""
    app = _make_app(fresh_db)
    relay = _mint_valid_relay_state()

    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/saml/sls",
            data={"SAMLResponse": "fakeresponse", "RelayState": relay},
            follow_redirects=False,
        )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"
    # Both cookies should be deleted (Set-Cookie with max-age=0 or past expiry).
    cookie_headers = resp.headers.get("set-cookie", "")
    # TestClient may join multiple Set-Cookie into one string; check each.
    assert "access_token" in cookie_headers
    assert "refresh_token" in cookie_headers


def test_sls_sp_init_tampered_relay_state_rejected(fresh_db, saml_slo_enabled):
    """SAMLResponse with tampered RelayState → 400."""
    from server.saml.relay_state import mint as _mint

    bad_relay = _mint(
        secret_key=b"WRONG-SECRET-DIFFERENT-FROM-CONFIG-ABCDE",
        idp="corp-sso",
        return_to="/",
    )

    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/saml/sls",
            data={"SAMLResponse": "fakeresponse", "RelayState": bad_relay},
        )
    assert resp.status_code == 400
    assert "RelayState" in resp.json()["detail"]


def test_sls_sp_init_relay_state_idp_mismatch_rejected(fresh_db, saml_slo_enabled):
    """RelayState signed for a different IdP name → 400."""
    from server.saml.relay_state import mint as _mint

    wrong_idp_relay = _mint(
        secret_key=b"test-jwt-secret-32-bytes-minimum-len!",
        idp="other-idp",
        return_to="/",
    )

    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/saml/sls",
            data={"SAMLResponse": "fakeresponse", "RelayState": wrong_idp_relay},
        )
    assert resp.status_code == 400


def test_sls_sp_init_missing_relay_state_rejected(fresh_db, saml_slo_enabled):
    """SAMLResponse with no RelayState → 400 (can't trust post-logout redirect)."""
    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/saml/sls",
            data={"SAMLResponse": "fakeresponse"},
        )
    assert resp.status_code == 400
    assert "RelayState" in resp.json()["detail"]


def test_sls_sp_init_sdk_error_rejected(fresh_db, saml_slo_enabled):
    """SDK raises during process_slo (SAMLResponse path) → 400."""
    _FakeSloAuth.slo_response_raises = True

    app = _make_app(fresh_db)
    relay = _mint_valid_relay_state()
    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/saml/sls",
            data={"SAMLResponse": "fakeresponse", "RelayState": relay},
        )
    assert resp.status_code == 400


def test_sls_sp_init_validation_errors_rejected(fresh_db, saml_slo_enabled):
    """SDK returns validation errors on SAMLResponse → 400."""
    _FakeSloAuth.slo_response_errors = ["invalid_logout_response"]

    app = _make_app(fresh_db)
    relay = _mint_valid_relay_state()
    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/saml/sls",
            data={"SAMLResponse": "fakeresponse", "RelayState": relay},
        )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# SLS — IdP-initiated (SAMLRequest)
# ---------------------------------------------------------------------------


def test_sls_idp_init_kills_session(fresh_db, saml_slo_enabled):
    """IdP sends LogoutRequest for a user with a live session → session cleared."""
    _FakeSloAuth.slo_last_message_id = f"_lr-{_test_secrets.token_hex(8)}"

    app = _make_app(fresh_db)
    token = _make_access_token("alice@corp.example.com")

    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/saml/sls",
            data={"SAMLRequest": "fakelogoutrequest"},
            cookies={"access_token": token},
            follow_redirects=False,
        )

    # Should succeed (200 or 302 depending on whether SDK produced a URL).
    assert resp.status_code in (200, 302)
    # Session cookies should be cleared.
    cookie_header = resp.headers.get("set-cookie", "")
    assert "access_token" in cookie_header


def test_sls_idp_init_no_session_returns_success(fresh_db, saml_slo_enabled):
    """IdP sends LogoutRequest for a NameID with no live SP session.

    Per decision D-2, we return a success (200/302) with the LogoutResponse
    body rather than a 4xx — using NoSession status internally. The test
    verifies we do NOT 404 or 400 here, which would break IdP propagation.
    """
    _FakeSloAuth.slo_last_message_id = f"_lr-{_test_secrets.token_hex(8)}"
    # No session cookie sent.
    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/saml/sls",
            data={"SAMLRequest": "fakelogoutrequest"},
            follow_redirects=False,
        )

    # Must NOT be 4xx (that would break IdP propagation chain).
    assert resp.status_code not in (400, 401, 403, 404)
    assert resp.status_code in (200, 302)


def test_sls_idp_init_replay_rejected(fresh_db, saml_slo_enabled):
    """Same LogoutRequest ID submitted twice → second rejected (400)."""
    fixed_id = f"_lr-fixed-{_test_secrets.token_hex(4)}"
    _FakeSloAuth.slo_last_message_id = fixed_id

    app = _make_app(fresh_db)
    with TestClient(app) as client:
        first = client.post(
            "/api/auth/saml/sls",
            data={"SAMLRequest": "fakelogoutrequest"},
            follow_redirects=False,
        )
        assert first.status_code in (200, 302), f"First submission failed: {first.status_code}"

        second = client.post(
            "/api/auth/saml/sls",
            data={"SAMLRequest": "fakelogoutrequest"},
            follow_redirects=False,
        )

    assert second.status_code == 400
    assert "replay" in second.json()["detail"].lower()


def test_sls_idp_init_sdk_error_rejected(fresh_db, saml_slo_enabled):
    """SDK raises during process_slo (SAMLRequest path) → 400."""
    _FakeSloAuth.slo_request_raises = True

    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/saml/sls",
            data={"SAMLRequest": "fakelogoutrequest"},
        )
    assert resp.status_code == 400


def test_sls_idp_init_validation_errors_rejected(fresh_db, saml_slo_enabled):
    """SDK returns validation errors on SAMLRequest → 400."""
    _FakeSloAuth.slo_request_errors = ["invalid_logout_request_signature"]

    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/saml/sls",
            data={"SAMLRequest": "fakelogoutrequest"},
        )
    assert resp.status_code == 400


def test_sls_idp_init_with_redirect_url(fresh_db, saml_slo_enabled):
    """When SDK produces a redirect URL for the LogoutResponse, we follow it."""
    _FakeSloAuth.slo_last_message_id = f"_lr-{_test_secrets.token_hex(8)}"
    _FakeSloAuth.slo_request_idp_redirect = "https://idp.example.com/slo/response"

    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/saml/sls",
            data={"SAMLRequest": "fakelogoutrequest"},
            follow_redirects=False,
        )
    assert resp.status_code == 302
    assert "idp.example.com" in resp.headers["location"]


def test_sls_idp_init_with_html_form(fresh_db, saml_slo_enabled):
    """When SDK produces an HTML auto-submit form, we return it as text/html."""
    _FakeSloAuth.slo_last_message_id = f"_lr-{_test_secrets.token_hex(8)}"
    _FakeSloAuth.slo_request_idp_redirect = (
        "<html><body><form method='post' action='https://idp.example.com/slo'>"
        "<input type='hidden' name='SAMLResponse' value='fake'/></form>"
        "<script>document.forms[0].submit();</script></body></html>"
    )

    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/saml/sls",
            data={"SAMLRequest": "fakelogoutrequest"},
            follow_redirects=False,
        )
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "SAMLResponse" in resp.text


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_sls_no_saml_fields_returns_400(fresh_db, saml_slo_enabled):
    """POST to /sls with neither SAMLRequest nor SAMLResponse → 400."""
    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/saml/sls",
            data={},
        )
    assert resp.status_code == 400
