#!/usr/bin/env python3
"""
OIDC ID-token claims enforcement (#324 M2 — epic #318)
======================================================

Unit-level check that the authlib client is registered with explicit
``iss``/``aud``/``exp`` claims options and asymmetric-only signing algs, so a
token minted for a different client/IdP (or an unsigned token) is rejected.

This inspects the registered client config and needs no live IdP — the
end-to-end behaviour is exercised by the Keycloak ``oidc (P3 acceptance)`` gate.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))


@pytest.fixture
def oidc_env(monkeypatch):
    monkeypatch.setenv("APP_OIDC_ENABLED", "true")
    monkeypatch.setenv("APP_OIDC_ISSUER_URL", "https://idp.example.com/realms/aif")
    monkeypatch.setenv("APP_OIDC_CLIENT_ID", "aif-client")
    monkeypatch.setenv("APP_OIDC_CLIENT_SECRET", "shh")
    from server.oidc.client import reset_oauth_client_cache

    reset_oauth_client_cache()
    yield
    reset_oauth_client_cache()


def _registered_kwargs(oauth) -> dict:
    # authlib stashes the original register() kwargs in _registry[name] as
    # ``(overwrite: bool, kwargs: dict)``.
    _, kwargs = oauth._registry["oidc"]
    return kwargs


def test_claims_options_enforce_iss_aud_exp(oidc_env):
    from server.oidc.client import get_oauth_client

    kwargs = _registered_kwargs(get_oauth_client())
    co = kwargs["claims_options"]

    assert co["iss"]["essential"] is True
    assert co["iss"]["values"] == ["https://idp.example.com/realms/aif"]
    assert co["aud"]["essential"] is True
    assert co["aud"]["values"] == ["aif-client"]
    assert co["exp"]["essential"] is True


def test_signing_algs_are_asymmetric_only(oidc_env):
    from server.oidc.client import get_oauth_client

    kwargs = _registered_kwargs(get_oauth_client())
    algs = kwargs["client_kwargs"]["id_token_signing_alg_values_supported"]

    # No `none` and no symmetric HS* (HMAC-confusion downgrade vector).
    assert "none" not in algs
    assert not any(a.upper().startswith("HS") for a in algs)
    assert "RS256" in algs


def test_pkce_still_required(oidc_env):
    from server.oidc.client import get_oauth_client

    kwargs = _registered_kwargs(get_oauth_client())
    assert kwargs["client_kwargs"]["code_challenge_method"] == "S256"
