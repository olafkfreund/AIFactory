#!/usr/bin/env python3
"""Check that OIDC client registration still requires PKCE."""

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


def test_pkce_still_required(oidc_env):
    from server.oidc.client import get_oauth_client

    kwargs = _registered_kwargs(get_oauth_client())
    assert kwargs["client_kwargs"]["code_challenge_method"] == "S256"
