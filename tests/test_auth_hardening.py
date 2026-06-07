#!/usr/bin/env python3
"""
Auth-hardening tests (#324 — epic #318)
=======================================

Covers the two self-contained findings in this slice:

- **M1** — the legacy ``API_TOKEN`` must be compared in constant time
  (``hmac.compare_digest``) and an unset token must never match.
- **H7** — the server must refuse to bind an unauthenticated admin API
  (``DISABLE_AUTH``) to a non-loopback host unless ``DEBUG`` explicitly
  accepts the risk.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server import auth as auth_mod  # noqa: E402
from server.config import (  # noqa: E402
    enforce_disable_auth_safety,
    host_is_loopback,
)


class TestLegacyApiTokenConstantTime:
    """M1 — legacy token match is constant-time and rejects empty config."""

    def _patch_token(self, monkeypatch, configured: str) -> None:
        monkeypatch.setattr(
            auth_mod, "get_settings",
            lambda: SimpleNamespace(API_TOKEN=configured),
        )

    def test_correct_token_matches(self, monkeypatch):
        self._patch_token(monkeypatch, "s3cret-token")
        assert auth_mod._is_legacy_api_token("s3cret-token") is True

    def test_wrong_token_rejected(self, monkeypatch):
        self._patch_token(monkeypatch, "s3cret-token")
        assert auth_mod._is_legacy_api_token("nope") is False

    def test_empty_configured_token_never_matches(self, monkeypatch):
        # An unset API_TOKEN must not enable the legacy path, even for an
        # empty presented token.
        self._patch_token(monkeypatch, "")
        assert auth_mod._is_legacy_api_token("") is False
        assert auth_mod._is_legacy_api_token("anything") is False

    def test_empty_presented_token_rejected(self, monkeypatch):
        self._patch_token(monkeypatch, "s3cret-token")
        assert auth_mod._is_legacy_api_token("") is False


class TestHostIsLoopback:
    @pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "", "  LocalHost "])
    def test_loopback_hosts(self, host):
        assert host_is_loopback(host) is True

    @pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "10.0.0.2"])
    def test_exposed_hosts(self, host):
        assert host_is_loopback(host) is False


class TestEnforceDisableAuthSafety:
    """H7 — refuse to bind an unauthenticated admin API to the network."""

    def _settings(self, **kw) -> SimpleNamespace:
        base = {"DISABLE_AUTH": True, "HOST": "0.0.0.0", "DEBUG": False}
        base.update(kw)
        return SimpleNamespace(**base)

    def test_no_op_when_auth_enabled(self):
        # DISABLE_AUTH off → never raises regardless of host.
        enforce_disable_auth_safety(self._settings(DISABLE_AUTH=False, HOST="0.0.0.0"))

    def test_allows_loopback(self):
        enforce_disable_auth_safety(self._settings(HOST="127.0.0.1"))

    def test_allows_exposed_with_debug_override(self):
        # DEBUG is the explicit local-dev opt-in for the risky combo.
        enforce_disable_auth_safety(self._settings(HOST="0.0.0.0", DEBUG=True))

    def test_refuses_exposed_without_debug(self):
        with pytest.raises(SystemExit):
            enforce_disable_auth_safety(self._settings(HOST="0.0.0.0", DEBUG=False))

    def test_refuses_lan_ip_without_debug(self):
        with pytest.raises(SystemExit):
            enforce_disable_auth_safety(self._settings(HOST="192.168.1.5", DEBUG=False))
