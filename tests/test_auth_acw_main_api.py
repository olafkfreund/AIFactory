"""#479: the main API middleware accepts a per-user `acw_` key.

A token minted in Settings -> API Keys (an `acw_<...>` key) should authenticate
direct programmatic REST calls, not just the stdio-MCP proxy path. The middleware
tries JWT, then the legacy token, then validates an `acw_`-prefixed token via the
shared acw_ authenticator.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

from server.auth import TokenAuthMiddleware  # noqa: E402


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(TokenAuthMiddleware)

    @app.get("/api/whoami")
    async def whoami(request_state_user=None):
        from fastapi import Request  # local import to read state

        return {"ok": True}

    return app


@pytest.fixture
def no_disable_auth(monkeypatch):
    # Force auth ON, no legacy token match, JWT decode fails → reach Strategy 3.
    monkeypatch.setattr(
        "server.auth.get_settings",
        lambda: SimpleNamespace(DISABLE_AUTH=False, API_TOKEN="legacy-xyz", JWT_SECRET="s"),
    )
    monkeypatch.setattr("server.auth._try_decode_jwt", lambda token: None)
    monkeypatch.setattr("server.auth._is_legacy_api_token", lambda token: False)


def test_valid_acw_key_authenticates(no_disable_auth, monkeypatch):
    key = SimpleNamespace(key_id="k1", user_id="u1", org_id="o1", scopes=frozenset())

    async def fake_authenticate(header):
        assert header == "Bearer acw_validtoken"
        return key

    monkeypatch.setattr("server.mcp_remote.auth.authenticate", fake_authenticate)

    client = TestClient(_app())
    r = client.get("/api/whoami", headers={"Authorization": "Bearer acw_validtoken"})
    assert r.status_code == 200, r.text


def test_invalid_acw_key_rejected(no_disable_auth, monkeypatch):
    async def fake_authenticate(header):
        raise Exception("unknown key")

    monkeypatch.setattr("server.mcp_remote.auth.authenticate", fake_authenticate)

    client = TestClient(_app())
    r = client.get("/api/whoami", headers={"Authorization": "Bearer acw_badtoken"})
    assert r.status_code == 401, r.text


def test_non_acw_invalid_token_still_401(no_disable_auth):
    # A non-acw_, non-JWT, non-legacy token never reaches acw_ validation.
    client = TestClient(_app())
    r = client.get("/api/whoami", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401, r.text
