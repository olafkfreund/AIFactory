"""Tests for the identity_providers discovery module (Epic #35 #41 PR-1b4).

Covers:
  * SAML-only enabled  → one SAML descriptor with expected fields.
  * OIDC-only enabled  → one OIDC descriptor.
  * Both enabled       → two descriptors, SAML first (documented order).
  * Neither enabled    → empty list.
  * SAML display_name defaults to SAML_IDP_NAME when
    SAML_IDP_DISPLAY_NAME is unset.
  * SAML display_name overridden by SAML_IDP_DISPLAY_NAME.
  * OIDC display_name defaults to "OIDC (default)" when OIDC_DISPLAY_NAME
    is unset.
  * SAML login_url carries the idp query parameter.
  * OIDC login_url is the fixed /api/auth/oidc/login path.
  * Partially-configured OIDC (missing required vars) returns no OIDC entry.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_SERVER = REPO_ROOT / "apps" / "web-server"
if str(WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(WEB_SERVER))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _list(monkeypatch, env: dict[str, str]) -> list:
    """Clear all relevant env vars, apply ``env``, return descriptor list."""
    # Wipe any lingering SAML_* / APP_OIDC_* / OIDC_* vars from the
    # process environment before each call so tests are isolated.
    for key in list(os.environ):
        if key.startswith(("SAML_", "APP_OIDC_", "OIDC_")):
            monkeypatch.delenv(key, raising=False)

    for key, value in env.items():
        monkeypatch.setenv(key, value)

    from server.identity_providers import list_configured_identity_providers

    return list_configured_identity_providers()


# ---------------------------------------------------------------------------
# Empty / disabled
# ---------------------------------------------------------------------------


def test_no_idps_when_nothing_configured(monkeypatch):
    result = _list(monkeypatch, {})
    assert result == []


def test_no_idps_when_saml_disabled_explicitly(monkeypatch):
    result = _list(monkeypatch, {"SAML_ENABLED": "false"})
    assert result == []


def test_no_oidc_when_flag_off(monkeypatch):
    result = _list(
        monkeypatch,
        {
            "APP_OIDC_ENABLED": "false",
            "APP_OIDC_ISSUER_URL": "https://idp.example.com",
            "APP_OIDC_CLIENT_ID": "client-id",
            "APP_OIDC_CLIENT_SECRET": "secret",
        },
    )
    assert result == []


def test_no_oidc_when_required_vars_missing(monkeypatch):
    """OIDC flag is on but mandatory vars are absent → not returned."""
    result = _list(
        monkeypatch,
        {
            "APP_OIDC_ENABLED": "true",
            # APP_OIDC_ISSUER_URL, CLIENT_ID, CLIENT_SECRET intentionally absent
        },
    )
    assert result == []


# ---------------------------------------------------------------------------
# SAML-only
# ---------------------------------------------------------------------------


def test_saml_descriptor_kind(monkeypatch):
    result = _list(monkeypatch, {"SAML_ENABLED": "true"})
    assert len(result) == 1
    assert result[0].kind == "saml"


def test_saml_default_name(monkeypatch):
    result = _list(monkeypatch, {"SAML_ENABLED": "true"})
    assert result[0].name == "saml"


def test_saml_custom_name(monkeypatch):
    result = _list(monkeypatch, {"SAML_ENABLED": "true", "SAML_IDP_NAME": "corp-sso"})
    assert result[0].name == "corp-sso"


def test_saml_display_name_defaults_to_idp_name(monkeypatch):
    """When SAML_IDP_DISPLAY_NAME is unset, display_name == SAML_IDP_NAME."""
    result = _list(
        monkeypatch,
        {"SAML_ENABLED": "true", "SAML_IDP_NAME": "corp-sso"},
    )
    assert result[0].display_name == "corp-sso"


def test_saml_display_name_override(monkeypatch):
    result = _list(
        monkeypatch,
        {
            "SAML_ENABLED": "true",
            "SAML_IDP_NAME": "corp-sso",
            "SAML_IDP_DISPLAY_NAME": "Corp SSO (SAML)",
        },
    )
    assert result[0].display_name == "Corp SSO (SAML)"


def test_saml_login_url_contains_idp_param(monkeypatch):
    result = _list(
        monkeypatch,
        {"SAML_ENABLED": "true", "SAML_IDP_NAME": "corp-sso"},
    )
    assert result[0].login_url == "/api/auth/saml/login?idp=corp-sso"


def test_saml_login_url_uses_default_name(monkeypatch):
    """When SAML_IDP_NAME is unset, login_url uses the 'saml' default."""
    result = _list(monkeypatch, {"SAML_ENABLED": "true"})
    assert result[0].login_url == "/api/auth/saml/login?idp=saml"


# ---------------------------------------------------------------------------
# OIDC-only
# ---------------------------------------------------------------------------


_OIDC_VARS = {
    "APP_OIDC_ENABLED": "true",
    "APP_OIDC_ISSUER_URL": "https://idp.example.com",
    "APP_OIDC_CLIENT_ID": "client-id",
    "APP_OIDC_CLIENT_SECRET": "secret",
}


def test_oidc_descriptor_kind(monkeypatch):
    result = _list(monkeypatch, _OIDC_VARS)
    assert len(result) == 1
    assert result[0].kind == "oidc"


def test_oidc_descriptor_name(monkeypatch):
    result = _list(monkeypatch, _OIDC_VARS)
    assert result[0].name == "oidc"


def test_oidc_login_url(monkeypatch):
    result = _list(monkeypatch, _OIDC_VARS)
    assert result[0].login_url == "/api/auth/oidc/login"


def test_oidc_default_display_name(monkeypatch):
    result = _list(monkeypatch, _OIDC_VARS)
    assert result[0].display_name == "OIDC (default)"


def test_oidc_custom_display_name(monkeypatch):
    result = _list(
        monkeypatch,
        {**_OIDC_VARS, "OIDC_DISPLAY_NAME": "Eng GitHub"},
    )
    assert result[0].display_name == "Eng GitHub"


def test_oidc_flag_variants_accepted(monkeypatch):
    """APP_OIDC_ENABLED=1 and APP_OIDC_ENABLED=yes are both valid."""
    for flag_value in ("1", "yes", "YES", "True", "TRUE"):
        result = _list(monkeypatch, {**_OIDC_VARS, "APP_OIDC_ENABLED": flag_value})
        assert len(result) == 1, f"Expected one OIDC entry for APP_OIDC_ENABLED={flag_value!r}"


# ---------------------------------------------------------------------------
# Both enabled — order is SAML first, OIDC second
# ---------------------------------------------------------------------------


def test_both_enabled_returns_two_descriptors(monkeypatch):
    result = _list(
        monkeypatch,
        {
            "SAML_ENABLED": "true",
            "SAML_IDP_NAME": "corp-sso",
            **_OIDC_VARS,
        },
    )
    assert len(result) == 2


def test_both_enabled_saml_first(monkeypatch):
    """Documented order: SAML appears before OIDC."""
    result = _list(
        monkeypatch,
        {
            "SAML_ENABLED": "true",
            "SAML_IDP_NAME": "corp-sso",
            **_OIDC_VARS,
        },
    )
    assert result[0].kind == "saml"
    assert result[1].kind == "oidc"


def test_both_enabled_field_accuracy(monkeypatch):
    """Each descriptor carries its own correct fields when both are enabled."""
    result = _list(
        monkeypatch,
        {
            "SAML_ENABLED": "true",
            "SAML_IDP_NAME": "corp-sso",
            "SAML_IDP_DISPLAY_NAME": "Corp SSO",
            **_OIDC_VARS,
            "OIDC_DISPLAY_NAME": "Engineering Login",
        },
    )
    saml, oidc = result
    assert saml.name == "corp-sso"
    assert saml.display_name == "Corp SSO"
    assert saml.login_url == "/api/auth/saml/login?idp=corp-sso"
    assert oidc.name == "oidc"
    assert oidc.display_name == "Engineering Login"
    assert oidc.login_url == "/api/auth/oidc/login"
