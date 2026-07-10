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
    cors_allows_credentials,
    enforce_cors_safety,
    enforce_disable_auth_safety,
    host_is_loopback,
)


class TestLegacyApiTokenConstantTime:
    """M1 — legacy token match is constant-time and rejects empty config."""

    def _patch_token(self, monkeypatch, configured: str) -> None:
        monkeypatch.setattr(
            auth_mod,
            "get_settings",
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
    @pytest.mark.parametrize(
        "host", ["127.0.0.1", "::1", "localhost", "", "  LocalHost "]
    )
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


class TestCorsSafety:
    """#555 — never combine a wildcard CORS origin with credentialed CORS."""

    def _settings(self, origins, **kw) -> SimpleNamespace:
        base = {"CORS_ORIGINS": origins, "DEBUG": False}
        base.update(kw)
        return SimpleNamespace(**base)

    def test_credentials_allowed_for_explicit_allowlist(self):
        s = self._settings(["https://app.example.com", "http://localhost:3100"])
        assert cors_allows_credentials(s) is True

    def test_credentials_dropped_for_wildcard(self):
        s = self._settings(["*"])
        assert cors_allows_credentials(s) is False

    def test_credentials_dropped_when_wildcard_mixed_in(self):
        s = self._settings(["https://app.example.com", "*"])
        assert cors_allows_credentials(s) is False

    def test_empty_origins_allows_credentials(self):
        # No origins configured is not a wildcard — credentials stay on.
        assert cors_allows_credentials(self._settings([])) is True

    def test_enforce_no_op_for_explicit_allowlist(self):
        enforce_cors_safety(self._settings(["https://app.example.com"]))

    def test_enforce_refuses_wildcard_in_production(self):
        with pytest.raises(SystemExit):
            enforce_cors_safety(self._settings(["*"], DEBUG=False))

    def test_enforce_allows_wildcard_under_debug(self):
        # Under DEBUG we warn and run with credentials dropped rather than exit.
        enforce_cors_safety(self._settings(["*"], DEBUG=True))


class TestWebSocketTokenExtraction:
    """#555 — WS token prefers the Authorization header; ?token= is deprecated."""

    def _ws(self, *, header=None, query=None) -> SimpleNamespace:
        headers = {}
        if header is not None:
            headers["authorization"] = header
        return SimpleNamespace(
            headers=headers,
            query_params={"token": query} if query is not None else {},
        )

    def test_prefers_authorization_header(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(
            auth_mod,
            "_warn_ws_query_token_once",
            lambda: called.__setitem__("n", called["n"] + 1),
        )
        ws = self._ws(header="Bearer header-token", query="query-token")
        assert auth_mod._ws_extract_token(ws) == "header-token"
        # Header present → no deprecation warning for the query param.
        assert called["n"] == 0

    def test_falls_back_to_query_param_with_warning(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(
            auth_mod,
            "_warn_ws_query_token_once",
            lambda: called.__setitem__("n", called["n"] + 1),
        )
        ws = self._ws(query="query-token")
        assert auth_mod._ws_extract_token(ws) == "query-token"
        assert called["n"] == 1

    def test_returns_none_when_no_token(self):
        assert auth_mod._ws_extract_token(self._ws()) is None

    def test_ignores_non_bearer_header(self, monkeypatch):
        # A malformed (non-"Bearer ") header is ignored; falls back to query.
        monkeypatch.setattr(auth_mod, "_warn_ws_query_token_once", lambda: None)
        ws = self._ws(header="Basic abc", query="query-token")
        assert auth_mod._ws_extract_token(ws) == "query-token"


class TestLegacyTokenDeprecationWarning:
    """#555 — legacy wildcard token use logs once, loudly."""

    def test_warns_once(self, monkeypatch):
        # Reset the module-level latch so the test is order-independent.
        monkeypatch.setattr(auth_mod, "_LEGACY_TOKEN_DEPRECATION_LOGGED", False)
        records = []
        monkeypatch.setattr(
            auth_mod.logger,
            "warning",
            lambda *a, **k: records.append(a),
        )
        auth_mod._warn_legacy_api_token_once()
        auth_mod._warn_legacy_api_token_once()
        assert len(records) == 1
