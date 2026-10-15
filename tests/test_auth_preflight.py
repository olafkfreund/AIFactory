"""Build-time auth pre-flight (#611 / RFC-0008 §3.2 a+b).

Covers the live credential probe (classification + mode resolution + provider
mapping) and the opt-in headless ANTHROPIC_API_KEY preference. The HTTP helpers
are monkeypatched so these stay offline and quota-free.
"""

from __future__ import annotations

import urllib.error

import pytest
from core import auth
from core import auth_preflight as ap

# ── mode resolution ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "warn"),
        ("", "warn"),
        ("warn", "warn"),
        ("off", "off"),
        ("0", "off"),
        ("false", "off"),
        ("enforce", "enforce"),
        ("strict", "enforce"),
        ("block", "enforce"),
        ("garbage", "warn"),
    ],
)
def test_preflight_mode(raw, expected, monkeypatch):
    if raw is None:
        monkeypatch.delenv("AIFACTORY_AUTH_PREFLIGHT", raising=False)
    else:
        monkeypatch.setenv("AIFACTORY_AUTH_PREFLIGHT", raw)
    assert ap.preflight_mode() == expected


# ── probe classification ─────────────────────────────────────────────────────


def test_probe_ok_with_api_key(monkeypatch):
    monkeypatch.setattr(ap, "_http_status", lambda url, headers: 200)
    r = ap.probe_anthropic({"ANTHROPIC_API_KEY": "sk-ant-xxx"})
    assert r.status == "ok" and r.credential_kind == "api_key"


def test_probe_auth_failed_401(monkeypatch):
    monkeypatch.setattr(ap, "_http_status", lambda url, headers: 401)
    r = ap.probe_anthropic({"ANTHROPIC_API_KEY": "sk-ant-bad"})
    assert r.status == "auth_failed" and r.is_auth_failure
    assert "ANTHROPIC_API_KEY" in r.detail


def test_probe_auth_failed_403(monkeypatch):
    monkeypatch.setattr(ap, "_http_status", lambda url, headers: 403)
    r = ap.probe_anthropic({"CLAUDE_CODE_OAUTH_TOKEN": "oat-bad"})
    assert r.status == "auth_failed" and r.credential_kind == "oauth"
    assert "CLAUDE_CODE_OAUTH_TOKEN" in r.detail


def test_probe_prefers_api_key_over_oauth(monkeypatch):
    captured = {}

    def fake(url, headers):
        captured["headers"] = headers
        return 200

    monkeypatch.setattr(ap, "_http_status", fake)
    r = ap.probe_anthropic(
        {"ANTHROPIC_API_KEY": "sk-ant-xxx", "CLAUDE_CODE_OAUTH_TOKEN": "oat"}
    )
    assert r.credential_kind == "api_key"
    assert "x-api-key" in captured["headers"]
    assert "authorization" not in captured["headers"]


def test_probe_oauth_header_shape(monkeypatch):
    captured = {}

    def fake(url, headers):
        captured["headers"] = headers
        return 200

    monkeypatch.setattr(ap, "_http_status", fake)
    ap.probe_anthropic({"CLAUDE_CODE_OAUTH_TOKEN": "oat-123"})
    assert captured["headers"]["authorization"] == "Bearer oat-123"
    assert captured["headers"]["anthropic-beta"] == ap._OAUTH_BETA


def test_probe_inconclusive_on_transport_error(monkeypatch):
    def boom(url, headers):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(ap, "_http_status", boom)
    r = ap.probe_anthropic({"ANTHROPIC_API_KEY": "sk-ant-xxx"})
    assert r.status == "inconclusive" and not r.is_auth_failure


def test_probe_inconclusive_on_5xx(monkeypatch):
    monkeypatch.setattr(ap, "_http_status", lambda url, headers: 503)
    r = ap.probe_anthropic({"ANTHROPIC_API_KEY": "sk-ant-xxx"})
    assert r.status == "inconclusive"


def test_probe_skipped_without_credential():
    r = ap.probe_anthropic({})
    assert r.status == "skipped"


@pytest.mark.parametrize(
    "code,body,status",
    [
        (200, "", "ok"),
        (401, "", "auth_failed"),
        (403, "", "auth_failed"),
        (400, '{"reason": "API_KEY_INVALID"}', "auth_failed"),
        (400, "bad request", "inconclusive"),
        (503, "unavailable", "inconclusive"),
    ],
)
def test_probe_google_classifies_response(monkeypatch, code, body, status):
    monkeypatch.setattr(ap, "_http_get", lambda url, headers: (code, body))
    result = ap.probe_google({"GEMINI_API_KEY": "gemini-secret"})
    assert result.status == status
    assert result.credential_kind == "api_key"
    if status == "auth_failed":
        assert "GEMINI_API_KEY" in result.detail
    assert "gemini-secret" not in result.detail


def test_probe_google_transport_error_is_inconclusive(monkeypatch):
    def fail(url, headers):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(ap, "_http_get", fail)
    assert ap.probe_google({"GEMINI_API_KEY": "gemini-secret"}).status == "inconclusive"


def test_probe_google_skips_without_key(monkeypatch):
    def fail(url, headers):
        pytest.fail("no request expected without a key")

    monkeypatch.setattr(ap, "_http_get", fail)
    assert ap.probe_google({}).status == "skipped"


@pytest.mark.parametrize(
    "env,key",
    [
        ({"GOOGLE_API_KEY": "google-secret"}, "google-secret"),
        (
            {"GEMINI_API_KEY": "gemini-secret", "GOOGLE_API_KEY": "google-secret"},
            "gemini-secret",
        ),
    ],
)
def test_probe_google_uses_key_in_header(monkeypatch, env, key):
    captured = {}

    def fake(url, headers):
        captured["url"] = url
        captured["headers"] = headers
        return 200, ""

    monkeypatch.setattr(ap, "_http_get", fake)
    result = ap.probe_google(env)
    assert result.status == "ok"
    assert captured["url"] == ap._GOOGLE_MODELS_URL
    assert key not in captured["url"] + result.detail
    assert captured["headers"] == {"x-goog-api-key": key}


# ── provider mapping + orchestration ─────────────────────────────────────────


def test_providers_for_models_maps_claude_to_anthropic():
    assert ap.providers_for_models(["claude-opus-4-8", "claude-haiku-4-5"]) == [
        "anthropic"
    ]


def test_providers_for_models_maps_gemini_and_deduplicates():
    assert ap.providers_for_models(["gemini", "claude-x", "gemini-2.5-pro"]) == [
        "google",
        "anthropic",
    ]


def test_run_auth_preflight_probes_anthropic(monkeypatch):
    monkeypatch.setattr(ap, "_http_status", lambda url, headers: 200)
    results = ap.run_auth_preflight(
        ["claude-opus-4-8"], {"ANTHROPIC_API_KEY": "sk-ant-xxx"}
    )
    assert len(results) == 1 and results[0].status == "ok"


def test_run_auth_preflight_empty_for_non_probeable():
    assert ap.run_auth_preflight(["gpt-4o"]) == []


# ── (b) opt-in headless ANTHROPIC_API_KEY preference ─────────────────────────


def test_headless_pref_off_by_default(monkeypatch):
    monkeypatch.delenv("AIFACTORY_HEADLESS_PREFER_API_KEY", raising=False)
    assert auth.headless_prefer_api_key() is False


def test_headless_pref_requires_api_key_auth_enabled(monkeypatch):
    # Prefer flag on, but API-key auth NOT enabled → OAuth still wins.
    monkeypatch.setenv("AIFACTORY_HEADLESS_PREFER_API_KEY", "1")
    monkeypatch.delenv("AIFACTORY_ALLOW_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oat-tok")
    assert auth.get_auth_token() == "oat-tok"


def test_headless_pref_returns_api_key_when_both_enabled(monkeypatch):
    monkeypatch.setenv("AIFACTORY_HEADLESS_PREFER_API_KEY", "1")
    monkeypatch.setenv("AIFACTORY_ALLOW_API_KEY", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oat-tok")
    assert auth.get_auth_token() == "sk-ant-xxx"
    assert auth.get_auth_token_source() == "ANTHROPIC_API_KEY (headless preference)"
