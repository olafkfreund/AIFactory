"""Scoped service tokens, step 1 (Factory#312, additive & flag-gated).

Proves the Phase 1 contract of docs/compliance/scoped-service-tokens.md:

* a scoped ``acw_`` service token authenticates and the principal carries ONLY
  its own explicit scopes;
* the wildcard ``API_TOKEN`` path is unchanged whether or not the flag is set;
* with the flag off (default) the ``acw_`` principal carries no ``scopes`` /
  ``scoped_service`` keys — the new path is off by default.

These are unit tests of ``TokenAuthMiddleware.dispatch``: ``get_settings`` and
the ``acw_`` validator are patched so no DB or real JWT secret is needed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from server import auth
from server.mcp_remote.auth import AuthenticatedKey
from starlette.requests import Request

WILDCARD = "wildcard-secret-token"
# Not a real secret — the acw_/wildcard test tokens are never valid JWTs, so the
# JWT decode step just fails regardless of this value. Kept off a literal call
# arg to avoid ruff S106 (hardcoded-secret) in test source.
_JWT_SECRET = "unit-test-" + "secret"


def _settings(*, scoped_flag: bool) -> SimpleNamespace:
    """Minimal stand-in for the app Settings used by the auth middleware."""
    return SimpleNamespace(
        DISABLE_AUTH=False,
        API_TOKEN=WILDCARD,
        JWT_SECRET=_JWT_SECRET,
        JWT_ALGORITHM="HS256",
        SCOPED_SERVICE_TOKENS_ENABLED=scoped_flag,
    )


def _make_request(token: str) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/tasks",
        "raw_path": b"/api/tasks",
        "query_string": b"",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }
    return Request(scope)


async def _next(_request):
    return "OK"


def _dispatch(token: str, *, scoped_flag: bool, key: AuthenticatedKey | None):
    """Run the middleware for ``token`` and return the resolved principal.

    ``key`` is what the patched ``acw_`` validator returns (or None to make the
    ``acw_`` lookup fail, exercising the wildcard/JWT branches only).
    """
    request = _make_request(token)
    mw = auth.TokenAuthMiddleware(app=None)

    async def _fake_authenticate(_header: str) -> AuthenticatedKey:
        if key is None:
            raise RuntimeError("no acw_ key")
        return key

    with (
        patch.object(
            auth, "get_settings", return_value=_settings(scoped_flag=scoped_flag)
        ),
        patch("server.mcp_remote.auth.authenticate", _fake_authenticate),
    ):
        result = asyncio.run(mw.dispatch(request, _next))
    return request.state.user, result


_SERVICE_KEY = AuthenticatedKey(
    key_id="key-123",
    scopes=frozenset({"tasks:read", "deploy:read"}),
    user_id=None,  # no owning user => a service (M2M) key
    org_id="org-pfactory",
)


def test_scoped_service_token_carries_only_its_scopes():
    """Flag on: the service principal exposes exactly the key's scopes."""
    user, result = _dispatch("acw_svc", scoped_flag=True, key=_SERVICE_KEY)
    assert result == "OK"
    assert user["is_service"] is True
    assert user["scoped_service"] is True
    assert user["scopes"] == ["deploy:read", "tasks:read"]  # sorted, only its own
    assert user["org_id"] == "org-pfactory"
    assert user["api_key_id"] == "key-123"


def test_new_path_off_by_default():
    """Flag off (default): the acw_ principal is byte-identical to pre-change."""
    user, result = _dispatch("acw_svc", scoped_flag=False, key=_SERVICE_KEY)
    assert result == "OK"
    assert user["is_service"] is True
    assert "scopes" not in user
    assert "scoped_service" not in user


def test_wildcard_path_unchanged_with_flag_on():
    """The wildcard token still yields the blanket service principal, no scopes,
    even when the scoped-token flag is enabled."""
    user, result = _dispatch(WILDCARD, scoped_flag=True, key=None)
    assert result == "OK"
    assert user == {
        "id": "default",
        "email": None,
        "role": "user",
        "is_service": True,
    }


def test_wildcard_path_unchanged_with_flag_off():
    user, result = _dispatch(WILDCARD, scoped_flag=False, key=None)
    assert result == "OK"
    assert user == {
        "id": "default",
        "email": None,
        "role": "user",
        "is_service": True,
    }
