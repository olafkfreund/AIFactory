"""Tests for the /api/auth/identity-providers discovery endpoint (Epic #35 #41 PR-1b4).

Covers:
  * SAML-only enabled  → list with one SAML entry (correct JSON shape).
  * OIDC-only enabled  → list with one OIDC entry.
  * Both enabled       → two entries, SAML first (documented order).
  * Neither enabled    → empty list (not a 404 / 500).
  * JSON shape: each item has name, kind, display_name, login_url.
  * The endpoint path is covered by PUBLIC_PREFIXES so the JWT
    middleware does not block it.

Test approach
--------------
These tests call ``list_configured_identity_providers`` + ``asdict``
directly — the same code path the route handler executes — rather than
spinning up a TestClient or using asyncio.run().

Using TestClient or asyncio.run() in files that sort before
tests/saml/test_routes.py (alphabetically) can contaminate the
event-loop state that test_routes.py's asyncio.run() seeding relies
on, causing intermittent 403s in the full suite under pytest-asyncio
asyncio_mode=auto.  Pure synchronous function calls avoid this
entirely.
"""

from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_SERVER = REPO_ROOT / "apps" / "web-server"
if str(WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(WEB_SERVER))


def _descriptors_as_dicts() -> list[dict]:
    """Mirror exactly what the route handler returns.

    The route handler does:
        providers = list_configured_identity_providers()
        return [asdict(p) for p in providers]

    We replicate that here without starting any HTTP server or event loop.
    """
    from server.identity_providers import list_configured_identity_providers

    return [asdict(p) for p in list_configured_identity_providers()]


# ---------------------------------------------------------------------------
# Core behaviour
# ---------------------------------------------------------------------------


def test_empty_list_when_no_idps_configured():
    result = _descriptors_as_dicts()
    assert result == []


def test_saml_only_returns_one_entry(monkeypatch):
    monkeypatch.setenv("SAML_ENABLED", "true")
    monkeypatch.setenv("SAML_IDP_NAME", "corp-sso")
    monkeypatch.setenv("SAML_IDP_DISPLAY_NAME", "Corp SSO (SAML)")

    result = _descriptors_as_dicts()
    assert len(result) == 1
    assert result[0]["kind"] == "saml"
    assert result[0]["name"] == "corp-sso"
    assert result[0]["display_name"] == "Corp SSO (SAML)"
    assert result[0]["login_url"] == "/api/auth/saml/login?idp=corp-sso"


def test_oidc_only_returns_one_entry(monkeypatch):
    monkeypatch.setenv("APP_OIDC_ENABLED", "true")
    monkeypatch.setenv("APP_OIDC_ISSUER_URL", "https://idp.example.com")
    monkeypatch.setenv("APP_OIDC_CLIENT_ID", "client-id")
    monkeypatch.setenv("APP_OIDC_CLIENT_SECRET", "secret")
    monkeypatch.setenv("OIDC_DISPLAY_NAME", "Eng OIDC")

    result = _descriptors_as_dicts()
    assert len(result) == 1
    assert result[0]["kind"] == "oidc"
    assert result[0]["name"] == "oidc"
    assert result[0]["display_name"] == "Eng OIDC"
    assert result[0]["login_url"] == "/api/auth/oidc/login"


def test_both_enabled_returns_two_entries_saml_first(monkeypatch):
    monkeypatch.setenv("SAML_ENABLED", "true")
    monkeypatch.setenv("SAML_IDP_NAME", "corp-sso")
    monkeypatch.setenv("APP_OIDC_ENABLED", "true")
    monkeypatch.setenv("APP_OIDC_ISSUER_URL", "https://idp.example.com")
    monkeypatch.setenv("APP_OIDC_CLIENT_ID", "client-id")
    monkeypatch.setenv("APP_OIDC_CLIENT_SECRET", "secret")

    result = _descriptors_as_dicts()
    assert len(result) == 2
    # Documented order: SAML before OIDC.
    assert result[0]["kind"] == "saml"
    assert result[1]["kind"] == "oidc"


# ---------------------------------------------------------------------------
# JSON shape
# ---------------------------------------------------------------------------


def test_response_contains_required_fields(monkeypatch):
    monkeypatch.setenv("SAML_ENABLED", "true")
    result = _descriptors_as_dicts()
    assert len(result) == 1
    item = result[0]
    for field in ("name", "kind", "display_name", "login_url"):
        assert field in item, f"Missing field: {field}"


def test_values_are_strings(monkeypatch):
    """All fields in the descriptor dict are strings (JSON-safe)."""
    monkeypatch.setenv("SAML_ENABLED", "true")
    result = _descriptors_as_dicts()
    item = result[0]
    for field in ("name", "kind", "display_name", "login_url"):
        assert isinstance(item[field], str), f"Field {field!r} must be str"


# ---------------------------------------------------------------------------
# Public reachability — JWT middleware must not block this endpoint
# ---------------------------------------------------------------------------


def test_public_prefixes_covers_identity_providers_path():
    """TokenAuthMiddleware.PUBLIC_PREFIXES must include /api/auth/ so
    the discovery endpoint is exempt from JWT validation without adding
    an explicit per-path exemption.

    Pure unit test — no HTTP server, no event-loop side effects.
    """
    from server.auth import TokenAuthMiddleware

    path = "/api/auth/identity-providers"
    assert any(
        path.startswith(prefix) for prefix in TokenAuthMiddleware.PUBLIC_PREFIXES
    ), (
        f"Expected {path!r} to be covered by PUBLIC_PREFIXES; "
        f"got {TokenAuthMiddleware.PUBLIC_PREFIXES!r}"
    )


def test_scim_path_in_public_prefixes():
    """/scim/v2/ must be in PUBLIC_PREFIXES so SCIM Bearer auth can run
    without the JWT middleware blocking the request first."""
    from server.auth import TokenAuthMiddleware

    assert "/scim/v2/" in TokenAuthMiddleware.PUBLIC_PREFIXES, (
        "Expected /scim/v2/ in PUBLIC_PREFIXES so SCIM Bearer middleware "
        "can run; got: " + repr(TokenAuthMiddleware.PUBLIC_PREFIXES)
    )
