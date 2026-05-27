"""Tests for the stdio-MCP proxy (Issue #154).

Two concerns:

1. **Auth + scope:** ``require_acw_scope`` accepts the legacy admin
   token as a wildcard, rejects missing / bad / scope-mismatched
   ``acw_`` keys with 401 vs 403 respectively, and accepts valid
   ``acw_`` keys with the right scope.

2. **Client routing:** ``http_client._read_token`` prefers env-var
   ``AIFACTORY_MCP_KEY`` over the legacy admin token, and the
   ``request()`` path rewrite prepends ``/api/mcp-stdio`` to outbound
   calls so they hit the proxy.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))
_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


from server.mcp_remote.auth import AuthenticatedKey, MCPAuthError
from server.mcp_stdio.auth import (
    MCP_READ_SCOPE,
    PROJECT_WRITE_SCOPE,
    _LegacyAdminKey,
    require_acw_scope,
)

# ── Legacy admin token = wildcard ────────────────────────────────────


def test_legacy_admin_key_has_every_scope():
    """The synthetic legacy key advertises every named scope."""
    k = _LegacyAdminKey()
    assert k.has_scope(MCP_READ_SCOPE)
    assert k.has_scope(PROJECT_WRITE_SCOPE)
    assert k.has_scope("task:write")
    assert k.has_scope("task:merge")
    # Unknown scopes also pass — by design, the legacy admin is
    # unconstrained, so even a typo-scope wouldn't block it.
    assert k.has_scope("anything-the-caller-passes")


# ── Dependency: auth + scope behaviour ───────────────────────────────


def _app_with_scope(scope: str) -> FastAPI:
    """Build a tiny FastAPI app whose only route is gated by ``scope``.

    Used to drive the auth dependency end-to-end via TestClient.
    """
    app = FastAPI()

    @app.get("/probe")
    async def probe(_=__import__("fastapi").Depends(require_acw_scope(scope))):
        return {"ok": True}

    return app


def test_missing_auth_header_returns_401():
    client = TestClient(_app_with_scope(MCP_READ_SCOPE))
    r = client.get("/probe")
    assert r.status_code == 401
    assert "Missing" in r.json()["detail"] or "malformed" in r.json()["detail"]


def test_legacy_admin_token_acts_as_wildcard(monkeypatch):
    """Token matching settings.API_TOKEN passes any scope check."""
    from server import config

    # Patch settings.API_TOKEN to a known value.
    monkeypatch.setattr(
        config.get_settings(), "API_TOKEN", "legacy-secret-token-for-test"
    )
    client = TestClient(_app_with_scope("task:merge"))
    r = client.get(
        "/probe", headers={"Authorization": "Bearer legacy-secret-token-for-test"}
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_unknown_acw_key_returns_401(monkeypatch):
    """An unknown bearer token falls through to acw_ validation, which
    rejects via MCPAuthError → 401."""
    async def _raise(_header):
        raise MCPAuthError("Invalid API key")

    monkeypatch.setattr(
        "server.mcp_stdio.auth.mcp_remote_auth.authenticate", _raise
    )
    client = TestClient(_app_with_scope(MCP_READ_SCOPE))
    r = client.get("/probe", headers={"Authorization": "Bearer acw_unknown"})
    assert r.status_code == 401
    assert "Invalid API key" in r.json()["detail"]


def test_acw_key_with_wrong_scope_returns_403(monkeypatch):
    """A valid acw_ key that lacks the requested scope → 403, not 401.

    The 401/403 split lets the client tell 'your key is bad' (regen)
    apart from 'your key works but is scoped wrong' (mint a new one).
    """
    async def _ok(_header):
        return AuthenticatedKey(
            key_id="key-123",
            scopes=frozenset({MCP_READ_SCOPE}),  # READ only
            user_id="user-1",
        )

    monkeypatch.setattr(
        "server.mcp_stdio.auth.mcp_remote_auth.authenticate", _ok
    )
    client = TestClient(_app_with_scope(PROJECT_WRITE_SCOPE))  # need WRITE
    r = client.get("/probe", headers={"Authorization": "Bearer acw_readonly"})
    assert r.status_code == 403
    assert "project:write" in r.json()["detail"]


def test_acw_key_with_right_scope_passes(monkeypatch):
    """Happy path: scoped acw_ key passes → handler runs."""
    async def _ok(_header):
        return AuthenticatedKey(
            key_id="key-456",
            scopes=frozenset({MCP_READ_SCOPE, PROJECT_WRITE_SCOPE}),
            user_id="user-2",
        )

    monkeypatch.setattr(
        "server.mcp_stdio.auth.mcp_remote_auth.authenticate", _ok
    )
    client = TestClient(_app_with_scope(PROJECT_WRITE_SCOPE))
    r = client.get(
        "/probe", headers={"Authorization": "Bearer acw_writer"}
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# ── Client-side token resolution ─────────────────────────────────────


def test_client_prefers_env_var_over_files(tmp_path, monkeypatch):
    """$AIFACTORY_MCP_KEY beats both .mcp-key and the legacy token."""
    from agents.tools_pkg import http_client

    # Set up files that should be IGNORED.
    mcp_key_file = tmp_path / ".mcp-key"
    mcp_key_file.write_text("acw_from_file\n")
    legacy_file = tmp_path / ".token"
    legacy_file.write_text("legacy_admin_token\n")

    monkeypatch.setattr(http_client, "DEFAULT_MCP_KEY_FILE", str(mcp_key_file))
    monkeypatch.setattr(http_client, "DEFAULT_TOKEN_FILE", str(legacy_file))
    monkeypatch.setenv("AIFACTORY_MCP_KEY", "acw_from_env")

    assert http_client._read_token() == "acw_from_env"


def test_client_falls_back_to_mcp_key_file_then_legacy(tmp_path, monkeypatch):
    """No env var → .mcp-key file → legacy token chain."""
    from agents.tools_pkg import http_client

    mcp_key_file = tmp_path / ".mcp-key"
    legacy_file = tmp_path / ".token"
    legacy_file.write_text("legacy_admin\n")

    monkeypatch.setattr(http_client, "DEFAULT_MCP_KEY_FILE", str(mcp_key_file))
    monkeypatch.setattr(http_client, "DEFAULT_TOKEN_FILE", str(legacy_file))
    monkeypatch.delenv("AIFACTORY_MCP_KEY", raising=False)
    monkeypatch.delenv("AIFACTORY_API_TOKEN_FILE", raising=False)

    # No .mcp-key file → uses legacy.
    assert http_client._read_token() == "legacy_admin"

    # Drop in a .mcp-key file → it takes precedence.
    mcp_key_file.write_text("acw_scoped\n")
    assert http_client._read_token() == "acw_scoped"


def test_client_rewrites_path_to_proxy_prefix(monkeypatch):
    """``request("GET", "/api/tasks")`` → outbound hits
    ``/api/mcp-stdio/tasks``. Confirms the stdio MCP never calls the
    raw REST surface and so cannot bypass scope gates by accident."""
    from agents.tools_pkg import http_client

    captured = {}

    class _FakeResp:
        status_code = 200
        content = b"{}"
        text = "{}"

        def json(self):
            return {}

    class _FakeClient:
        async def request(self, method, path, **kwargs):
            captured["method"] = method
            captured["path"] = path
            return _FakeResp()

        async def aclose(self):
            pass

    monkeypatch.setattr(http_client._state, "_client", _FakeClient())
    monkeypatch.setattr(http_client._state, "_base_url", "http://localhost:3101")
    monkeypatch.setattr(http_client, "_read_token", lambda: "acw_test_key")

    import asyncio
    asyncio.run(http_client.request("GET", "/api/tasks"))
    assert captured["path"] == "/api/mcp-stdio/tasks"

    # And paths already under the proxy prefix pass through unchanged.
    asyncio.run(http_client.request("GET", "/api/mcp-stdio/projects"))
    assert captured["path"] == "/api/mcp-stdio/projects"
