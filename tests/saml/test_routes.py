"""Routes-layer tests for SAML SP endpoints (Epic #35 #41 PR-1b2).

These tests mock the OneLogin SDK at its public boundary
(``OneLogin_Saml2_Auth``) so they exercise OUR code — RelayState
verification, replay cache, cross-IdP collision guard, cookie shape —
without bringing in the C-extension signature path. The PR-1a test
suite (``test_client.py``) already exercises the SDK integration on
real fixtures.

What we DO test here:
  * 404 when SAML disabled
  * /login mints valid RelayState + redirects via SDK
  * /login rejects unknown idp name
  * /acs happy path: SCIM-provisioned user gets session cookie
  * /acs SDK rejection → 400
  * /acs replay rejection → 400
  * /acs tampered RelayState → 400
  * /acs unknown user → 403
  * /acs cross-IdP collision → 409 (user has OIDC identity already)
  * /acs idempotent re-login (same kind+subject) → 302
  * /acs IdP-init without default return_to → 400
  * /metadata returns XML
"""

from __future__ import annotations

import asyncio
import os
import secrets as _test_secrets
import sys
from datetime import datetime, timezone
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
# DB + app fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_db(tmp_path):
    """Per-test file-backed SQLite. Returns (engine, SessionLocal).

    Uses a tmp_path file (not ``mode=memory&cache=shared``) for two reasons:
    1. ``cache=shared`` in-memory dbs require the init connection to stay
       alive — closing the init event loop tears down aiosqlite background
       threads in a way that interferes with later ``asyncio.run()`` calls
       in CI (where every test is collected up-front + pytest's plugin
       layer creates its own loops). File-backed is bulletproof.
    2. ``tmp_path`` is per-test and auto-cleaned by pytest, so isolation
       is guaranteed without nonce gymnastics.
    """
    from server.database.models import Base

    db_path = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())

    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    return engine, SessionLocal
@pytest.fixture(autouse=True)
def _clean_saml_env(monkeypatch, tmp_path, idp_metadata_xml):
    """Each test sets SAML env vars explicitly; clear leftovers + the
    routes-module singletons between runs."""
    for key in list(os.environ):
        if key.startswith("SAML_"):
            monkeypatch.delenv(key, raising=False)

    # Stash the metadata XML in a file we can point SAML_IDP_METADATA_FILE at.
    metadata_path = tmp_path / "idp-metadata.xml"
    metadata_path.write_text(idp_metadata_xml, encoding="utf-8")
    monkeypatch.setenv("SAML_IDP_METADATA_FILE", str(metadata_path))

    # Force a stable JWT secret so cookie/RelayState HMACs are
    # deterministic. ``Settings`` is constructed at import time (it's a
    # global), so env-var monkeypatch doesn't propagate — we mutate the
    # already-built instance directly. Restore on teardown so leakage
    # into the next test is impossible.
    from server import config as _config

    _orig_secret = _config.settings.JWT_SECRET
    _orig_alg = _config.settings.JWT_ALGORITHM
    _config.settings.JWT_SECRET = "test-jwt-secret-32-bytes-minimum-len!"
    _config.settings.JWT_ALGORITHM = "HS256"

    # Reset the SAML routes-module singletons.
    from server.saml import routes as _routes

    _routes.reset_saml_state_for_tests()
    yield
    _routes.reset_saml_state_for_tests()
    _config.settings.JWT_SECRET = _orig_secret
    _config.settings.JWT_ALGORITHM = _orig_alg


@pytest.fixture
def saml_enabled(monkeypatch, sp_cert_pem, sp_key_pem, tmp_path):
    """Switch SAML on with a valid file-based config."""
    sp_cert = tmp_path / "sp.crt"
    sp_cert.write_text(sp_cert_pem, encoding="utf-8")
    sp_key = tmp_path / "sp.key"
    sp_key.write_text(sp_key_pem, encoding="utf-8")

    monkeypatch.setenv("SAML_ENABLED", "true")
    monkeypatch.setenv("SAML_SP_ENTITY_ID", "https://test-sp.example.com/saml")
    monkeypatch.setenv("SAML_ACS_URL", "https://test-sp.example.com/api/auth/saml/acs")
    monkeypatch.setenv("SAML_SP_CERT_FILE", str(sp_cert))
    monkeypatch.setenv("SAML_SP_KEY_FILE", str(sp_key))
    monkeypatch.setenv("SAML_IDP_NAME", "corp-sso")
    monkeypatch.setenv(
        "SAML_IDP_INIT_DEFAULT_RETURN_TO",
        "https://test-sp.example.com/dashboard",
    )


def _make_app(fresh_db) -> FastAPI:
    """Build a FastAPI app with the SAML router mounted + DB override."""
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


async def _seed_user(SessionLocal, email: str, *, role: str = "user"):
    """Insert a User row + return its ID."""
    from server.database.models import User

    async with SessionLocal() as s:
        u = User(
            email=email,
            password_hash="x",
            role=role,
            is_active=True,
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u.id


async def _seed_external_identity(
    SessionLocal, user_id: str, kind: str, subject: str,
):
    from server.database.models import ExternalIdentity

    async with SessionLocal() as s:
        s.add(ExternalIdentity(user_id=user_id, kind=kind, subject=subject))
        await s.commit()


# ---------------------------------------------------------------------------
# Mock OneLogin SDK
# ---------------------------------------------------------------------------


class _FakeAuth:
    """Configurable stand-in for OneLogin_Saml2_Auth.

    Tests set the class-level ``mode`` + ``nameid`` + ``assertion_id``
    + ``not_on_or_after`` before invoking the route. The ``login()``
    method returns a deterministic SSO redirect URL.
    """

    mode: str = "ok"  # "ok" | "sdk_error" | "validation_errors" | "not_authn"
    nameid: str = "alice@corp.example.com"
    assertion_id: str = "_assertion-id-default"
    not_on_or_after: float = 9_999_999_999.0  # far future

    def __init__(self, request_data, old_settings=None):
        self._request_data = request_data
        self._old_settings = old_settings
        self._errors: list[str] = []
        self._authenticated = False

    def login(self, return_to: str | None = None, force_authn: bool = False) -> str:
        # OneLogin embeds the RelayState in the URL; tests don't care
        # about the AuthnRequest payload — just that we got a URL back.
        return f"https://idp.example.com/sso?RelayState={return_to or ''}"

    def process_response(self) -> None:
        if _FakeAuth.mode == "sdk_error":
            raise RuntimeError("simulated SDK signature failure")
        if _FakeAuth.mode == "validation_errors":
            self._errors = ["invalid_response"]
            return
        if _FakeAuth.mode == "not_authn":
            self._authenticated = False
            return
        self._authenticated = True
        self._errors = []

    def get_errors(self) -> list[str]:
        return self._errors

    def is_authenticated(self) -> bool:
        return self._authenticated

    def get_nameid(self) -> str:
        return _FakeAuth.nameid

    def get_last_assertion_id(self) -> str:
        return _FakeAuth.assertion_id

    def get_last_assertion_not_on_or_after(self) -> float:
        return _FakeAuth.not_on_or_after


@pytest.fixture(autouse=True)
def _patch_onelogin_auth(monkeypatch):
    """Replace ``OneLogin_Saml2_Auth`` everywhere routes.py uses it.

    The route does ``from onelogin.saml2.auth import OneLogin_Saml2_Auth``
    inside the handler (lazy), so we patch the module-level symbol.
    """
    import onelogin.saml2.auth as _auth_mod

    monkeypatch.setattr(_auth_mod, "OneLogin_Saml2_Auth", _FakeAuth)

    # Reset class-level defaults before each test so leakage between
    # tests can't mask bugs.
    _FakeAuth.mode = "ok"
    _FakeAuth.nameid = "alice@corp.example.com"
    _FakeAuth.assertion_id = f"_id-{_test_secrets.token_hex(8)}"
    _FakeAuth.not_on_or_after = 9_999_999_999.0
    yield


# ---------------------------------------------------------------------------
# Disabled-by-default tests
# ---------------------------------------------------------------------------


def test_login_404_when_disabled(fresh_db):
    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = client.get("/api/auth/saml/login")
    assert resp.status_code == 404


def test_acs_404_when_disabled(fresh_db):
    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/saml/acs",
            data={"SAMLResponse": "ignored"},
        )
    assert resp.status_code == 404


def test_metadata_404_when_disabled(fresh_db):
    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = client.get("/api/auth/saml/metadata")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /login tests
# ---------------------------------------------------------------------------


def test_login_redirects_with_relay_state(fresh_db, saml_enabled):
    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = client.get(
            "/api/auth/saml/login?idp=corp-sso",
            follow_redirects=False,
        )
    assert resp.status_code == 302
    location = resp.headers["location"]
    # _FakeAuth.login returns sso?RelayState=<token>.
    assert "RelayState=" in location
    # The RelayState should be the HMAC-signed payload, not raw text.
    relay_token = location.split("RelayState=", 1)[1]
    # payload.signature shape.
    assert "." in relay_token


def test_login_default_idp_when_param_omitted(fresh_db, saml_enabled):
    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = client.get(
            "/api/auth/saml/login", follow_redirects=False,
        )
    assert resp.status_code == 302


def test_login_rejects_unknown_idp(fresh_db, saml_enabled):
    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = client.get(
            "/api/auth/saml/login?idp=wrong-name",
            follow_redirects=False,
        )
    assert resp.status_code == 400
    assert "Unknown IdP" in resp.text


# ---------------------------------------------------------------------------
# /acs tests
# ---------------------------------------------------------------------------


def _post_acs(
    client: TestClient,
    saml_response: str = "<dummy/>",
    relay_state: str | None = None,
):
    form = {"SAMLResponse": saml_response}
    if relay_state is not None:
        form["RelayState"] = relay_state
    return client.post(
        "/api/auth/saml/acs", data=form, follow_redirects=False,
    )


def _mint_valid_relay_state() -> str:
    """Build a RelayState that matches our test config."""
    from server.saml.relay_state import mint

    return mint(
        secret_key=b"test-jwt-secret-32-bytes-minimum-len!",
        idp="corp-sso",
        return_to="https://test-sp.example.com/welcome",
    )


def test_acs_happy_path_sp_init(fresh_db, saml_enabled):
    """Provisioned user + clean external_ids + valid RelayState → cookie."""
    _engine, SessionLocal = fresh_db
    user_id = asyncio.run(_seed_user(SessionLocal, "alice@corp.example.com"))

    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = _post_acs(client, relay_state=_mint_valid_relay_state())

    assert resp.status_code == 302
    assert resp.headers["location"] == "https://test-sp.example.com/welcome"
    cookie = resp.headers.get("set-cookie", "")
    assert "access_token=" in cookie
    assert "HttpOnly" in cookie

    # ExternalIdentity row created.
    async def _check():
        from server.database.models import ExternalIdentity
        from sqlalchemy import select

        async with SessionLocal() as s:
            rows = (await s.execute(
                select(ExternalIdentity).where(
                    ExternalIdentity.user_id == user_id,
                ),
            )).scalars().all()
            return rows

    rows = asyncio.run(_check())
    assert len(rows) == 1
    assert rows[0].kind == "saml:corp-sso"
    assert rows[0].subject == "alice@corp.example.com"

    # Access token decodes + carries the expected sub/email.
    raw = cookie.split("access_token=", 1)[1].split(";", 1)[0]
    payload = jwt.decode(
        raw, "test-jwt-secret-32-bytes-minimum-len!", algorithms=["HS256"],
    )
    assert payload["email"] == "alice@corp.example.com"
    assert payload["type"] == "access"


def test_acs_sdk_rejection_returns_400(fresh_db, saml_enabled):
    """If the SDK raises on process_response, we surface 400 — never 500."""
    asyncio.run(_seed_user(fresh_db[1], "alice@corp.example.com"))
    _FakeAuth.mode = "sdk_error"

    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = _post_acs(client, relay_state=_mint_valid_relay_state())
    assert resp.status_code == 400
    # Generic message (no echo of attacker payload).
    assert resp.json()["detail"] == "SAML response rejected"


def test_acs_validation_errors_returns_400(fresh_db, saml_enabled):
    asyncio.run(_seed_user(fresh_db[1], "alice@corp.example.com"))
    _FakeAuth.mode = "validation_errors"

    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = _post_acs(client, relay_state=_mint_valid_relay_state())
    assert resp.status_code == 400


def test_acs_not_authenticated_returns_400(fresh_db, saml_enabled):
    asyncio.run(_seed_user(fresh_db[1], "alice@corp.example.com"))
    _FakeAuth.mode = "not_authn"

    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = _post_acs(client, relay_state=_mint_valid_relay_state())
    assert resp.status_code == 400


def test_acs_replay_attack_rejected(fresh_db, saml_enabled):
    """Submitting the same assertion_id twice → second one rejected."""
    asyncio.run(_seed_user(fresh_db[1], "alice@corp.example.com"))
    _FakeAuth.assertion_id = "_id-fixed-for-replay-test"

    app = _make_app(fresh_db)
    with TestClient(app) as client:
        first = _post_acs(client, relay_state=_mint_valid_relay_state())
        assert first.status_code == 302  # accepted

        second = _post_acs(client, relay_state=_mint_valid_relay_state())
    assert second.status_code == 400
    assert "replay" in second.json()["detail"].lower()


def test_acs_tampered_relay_state_rejected(fresh_db, saml_enabled):
    asyncio.run(_seed_user(fresh_db[1], "alice@corp.example.com"))

    # Build a RelayState with the WRONG secret — HMAC won't verify.
    from server.saml.relay_state import mint as _mint

    bad_relay = _mint(
        secret_key=b"WRONG-SECRET-DIFFERENT-FROM-CONFIG-ABCDE",
        idp="corp-sso",
        return_to="https://test-sp.example.com/welcome",
    )

    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = _post_acs(client, relay_state=bad_relay)
    assert resp.status_code == 400


def test_acs_relay_state_idp_mismatch_rejected(fresh_db, saml_enabled):
    """RelayState was minted for a DIFFERENT idp — reject."""
    asyncio.run(_seed_user(fresh_db[1], "alice@corp.example.com"))

    from server.saml.relay_state import mint as _mint

    relay = _mint(
        secret_key=b"test-jwt-secret-32-bytes-minimum-len!",
        idp="other-idp",  # not "corp-sso"
        return_to="https://test-sp.example.com/welcome",
    )

    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = _post_acs(client, relay_state=relay)
    assert resp.status_code == 400


def test_acs_unknown_user_returns_403(fresh_db, saml_enabled):
    """No user row → SCIM hasn't provisioned → 403 (decision #4)."""
    _FakeAuth.nameid = "ghost@corp.example.com"

    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = _post_acs(client, relay_state=_mint_valid_relay_state())
    assert resp.status_code == 403
    assert "not provisioned" in resp.json()["detail"].lower()


def test_acs_cross_idp_collision_rejected(fresh_db, saml_enabled):
    """User already has an OIDC identity → SAML link rejected with 409."""
    _engine, SessionLocal = fresh_db
    user_id = asyncio.run(
        _seed_user(SessionLocal, "alice@corp.example.com"),
    )
    asyncio.run(_seed_external_identity(
        SessionLocal, user_id, kind="oidc:github", subject="gh-sub-12345",
    ))

    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = _post_acs(client, relay_state=_mint_valid_relay_state())
    assert resp.status_code == 409
    assert "admin" in resp.json()["detail"].lower()


def test_acs_same_kind_different_subject_rejected(fresh_db, saml_enabled):
    """Existing saml:corp-sso identity but for a different subject → 409.

    Defends against an attacker who somehow gets a SAML assertion for
    the same email but a different NameID (e.g. IdP misconfig where
    NameID drifts between provider versions).
    """
    _engine, SessionLocal = fresh_db
    user_id = asyncio.run(
        _seed_user(SessionLocal, "alice@corp.example.com"),
    )
    asyncio.run(_seed_external_identity(
        SessionLocal, user_id,
        kind="saml:corp-sso", subject="alice@previous-tenant.example.com",
    ))

    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = _post_acs(client, relay_state=_mint_valid_relay_state())
    assert resp.status_code == 409


def test_acs_idempotent_relogin_succeeds(fresh_db, saml_enabled):
    """Same (kind, subject) → no new row, but session minted."""
    _engine, SessionLocal = fresh_db
    user_id = asyncio.run(
        _seed_user(SessionLocal, "alice@corp.example.com"),
    )
    asyncio.run(_seed_external_identity(
        SessionLocal, user_id,
        kind="saml:corp-sso", subject="alice@corp.example.com",
    ))

    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = _post_acs(client, relay_state=_mint_valid_relay_state())
    assert resp.status_code == 302

    # Still exactly one row, not two.
    async def _count():
        from server.database.models import ExternalIdentity
        from sqlalchemy import select

        async with SessionLocal() as s:
            rows = (await s.execute(
                select(ExternalIdentity).where(
                    ExternalIdentity.user_id == user_id,
                ),
            )).scalars().all()
            return len(rows)

    assert asyncio.run(_count()) == 1


def test_acs_idp_init_uses_default_return_to(fresh_db, saml_enabled):
    """No RelayState → IdP-init flow → redirect to default return_to."""
    asyncio.run(_seed_user(fresh_db[1], "alice@corp.example.com"))

    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = _post_acs(client, relay_state=None)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://test-sp.example.com/dashboard"


def test_acs_idp_init_without_default_return_to_rejected(
    fresh_db, saml_enabled, monkeypatch,
):
    """Operator disabled IdP-init by leaving default_return_to unset."""
    monkeypatch.delenv("SAML_IDP_INIT_DEFAULT_RETURN_TO", raising=False)
    asyncio.run(_seed_user(fresh_db[1], "alice@corp.example.com"))

    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = _post_acs(client, relay_state=None)
    assert resp.status_code == 400
    assert "IdP-init" in resp.json()["detail"]


def test_acs_email_match_is_case_insensitive(fresh_db, saml_enabled):
    """IdP asserts MixedCase email; user row stored lowercase. Match."""
    asyncio.run(_seed_user(fresh_db[1], "alice@corp.example.com"))
    _FakeAuth.nameid = "Alice@Corp.Example.COM"

    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = _post_acs(client, relay_state=_mint_valid_relay_state())
    assert resp.status_code == 302


def test_acs_stamps_last_login_at(fresh_db, saml_enabled):
    """Successful SAML login updates ``users.last_login_at`` (Epic #43)."""
    _engine, SessionLocal = fresh_db
    user_id = asyncio.run(_seed_user(SessionLocal, "alice@corp.example.com"))

    before = datetime.now(timezone.utc).replace(tzinfo=None)
    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = _post_acs(client, relay_state=_mint_valid_relay_state())
    assert resp.status_code == 302

    async def _read():
        from server.database.models import User

        async with SessionLocal() as s:
            return await s.get(User, user_id)

    user_after = asyncio.run(_read())
    assert user_after.last_login_at is not None
    assert user_after.last_login_at >= before


# ---------------------------------------------------------------------------
# /metadata
# ---------------------------------------------------------------------------


def test_metadata_returns_sp_xml(fresh_db, saml_enabled):
    app = _make_app(fresh_db)
    with TestClient(app) as client:
        resp = client.get("/api/auth/saml/metadata")
    assert resp.status_code == 200
    assert "text/xml" in resp.headers["content-type"]
    body = resp.text
    assert "EntityDescriptor" in body
    assert "https://test-sp.example.com/saml" in body
