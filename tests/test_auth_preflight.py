"""Build-time auth pre-flight (#611 / RFC-0008 §3.2 a+b).

Covers the live credential probe (classification + mode resolution + provider
mapping) and the opt-in headless ANTHROPIC_API_KEY preference. The HTTP helpers
are monkeypatched so these stay offline and quota-free.
"""

from __future__ import annotations

import http.client
import json
import urllib.error

import pytest
from core import auth
from core import auth_preflight as ap


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    ap._cache.clear()
    yield
    ap._cache.clear()


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
    monkeypatch.setattr(ap, "_http_status", lambda url, headers, **_: 200)
    r = ap.probe_anthropic({"ANTHROPIC_API_KEY": "sk-ant-xxx"})
    assert r.status == "ok" and r.credential_kind == "api_key"


def test_probe_auth_failed_401(monkeypatch):
    monkeypatch.setattr(ap, "_http_status", lambda url, headers, **_: 401)
    r = ap.probe_anthropic({"ANTHROPIC_API_KEY": "sk-ant-bad"})
    assert r.status == "auth_failed" and r.is_auth_failure
    assert "ANTHROPIC_API_KEY" in r.detail


def test_probe_auth_failed_403(monkeypatch):
    monkeypatch.setattr(ap, "_http_status", lambda url, headers, **_: 403)
    r = ap.probe_anthropic({"CLAUDE_CODE_OAUTH_TOKEN": "oat-bad"})
    assert r.status == "auth_failed" and r.credential_kind == "oauth"
    assert "CLAUDE_CODE_OAUTH_TOKEN" in r.detail


def test_probe_prefers_api_key_over_oauth(monkeypatch):
    captured = {}

    def fake(url, headers, **_):
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

    def fake(url, headers, **_):
        captured["headers"] = headers
        return 200

    monkeypatch.setattr(ap, "_http_status", fake)
    ap.probe_anthropic({"CLAUDE_CODE_OAUTH_TOKEN": "oat-123"})
    assert captured["headers"]["authorization"] == "Bearer oat-123"
    assert captured["headers"]["anthropic-beta"] == ap._OAUTH_BETA


def test_probe_inconclusive_on_transport_error(monkeypatch):
    def boom(url, headers, **_):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(ap, "_http_status", boom)
    r = ap.probe_anthropic({"ANTHROPIC_API_KEY": "sk-ant-xxx"})
    assert r.status == "inconclusive" and not r.is_auth_failure


def test_probe_inconclusive_on_5xx(monkeypatch):
    monkeypatch.setattr(ap, "_http_status", lambda url, headers, **_: 503)
    r = ap.probe_anthropic({"ANTHROPIC_API_KEY": "sk-ant-xxx"})
    assert r.status == "inconclusive"


def test_probe_skipped_without_credential():
    r = ap.probe_anthropic({})
    assert r.status == "skipped"


_KEY_INVALID = json.dumps(
    {"error": {"code": 400, "details": [{"reason": "API_KEY_INVALID"}]}}
)
_SERVICE_DISABLED = json.dumps(
    {"error": {"code": 403, "details": [{"reason": "SERVICE_DISABLED"}]}}
)


@pytest.mark.parametrize(
    "code,body,status",
    [
        (200, "", "ok"),
        (400, _KEY_INVALID, "auth_failed"),
        # A 403 can be a disabled service or an IP block on a valid key.
        (403, _SERVICE_DISABLED, "inconclusive"),
        (401, "", "inconclusive"),
        (400, '"API_KEY_INVALID" mentioned outside error.details', "inconclusive"),
        (400, "bad request", "inconclusive"),
        (503, "unavailable", "inconclusive"),
    ],
)
def test_probe_google_classifies_response(monkeypatch, code, body, status):
    monkeypatch.setattr(ap, "_http_get", lambda url, headers, **_: (code, body))
    result = ap.probe_google({"GEMINI_API_KEY": "gemini-secret"})
    assert result.status == status
    if status == "auth_failed":
        assert "GEMINI_API_KEY" in result.detail
    assert "gemini-secret" not in result.detail


def test_probe_google_transport_error_is_inconclusive(monkeypatch):
    def fail(url, headers, **_):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(ap, "_http_get", fail)
    assert ap.probe_google({"GEMINI_API_KEY": "gemini-secret"}).status == "inconclusive"


@pytest.mark.parametrize(
    "env",
    [
        {},
        # GOOGLE_API_KEY is also the graphiti embedder's key; not the CLI's.
        {"GOOGLE_API_KEY": "google-secret"},
        {"GEMINI_API_KEY": "k", "GOOGLE_GENAI_USE_VERTEXAI": "true"},
        {"GEMINI_API_KEY": "k", "GOOGLE_APPLICATION_CREDENTIALS": "/adc.json"},
    ],
)
def test_probe_google_skips_without_a_usable_api_key(monkeypatch, env):
    def fail(url, headers, **_):
        pytest.fail("no request expected")

    monkeypatch.setattr(ap, "_http_get", fail)
    assert ap.probe_google(env).status == "skipped"


def test_probe_google_uses_key_in_header(monkeypatch):
    captured = {}

    def fake(url, headers, **_):
        captured["url"] = url
        captured["headers"] = headers
        return 200, ""

    monkeypatch.setattr(ap, "_http_get", fake)
    result = ap.probe_google({"GEMINI_API_KEY": "gemini-secret"})
    assert result.status == "ok"
    assert captured["url"] == ap._GOOGLE_MODELS_URL
    assert "gemini-secret" not in captured["url"] + result.detail
    assert captured["headers"] == {"x-goog-api-key": "gemini-secret"}


def test_http_get_survives_incomplete_body(monkeypatch):
    class Resp:
        status = 400

        def read(self):
            raise http.client.IncompleteRead(b"partial")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(ap.urllib.request, "urlopen", lambda req, timeout: Resp())
    assert ap._http_get("https://example.invalid", {}) == (400, "")


@pytest.mark.parametrize(
    "probe,env",
    [
        (ap.probe_google, {"GEMINI_API_KEY": "bad\r\nkey"}),
        (ap.probe_anthropic, {"ANTHROPIC_API_KEY": "bad\r\nkey"}),
    ],
)
def test_invalid_header_value_is_inconclusive_and_not_echoed(probe, env):
    # Real urllib, no network: the CR/LF header is refused before any request.
    result = probe(env)
    assert result.status == "inconclusive"
    assert "bad" not in result.detail


def test_definite_results_are_cached_inconclusive_are_not(monkeypatch):
    calls = []

    def fake(url, headers, **_):
        calls.append(url)
        return (503, "") if len(calls) == 1 else (200, "")

    monkeypatch.setattr(ap, "_http_get", fake)
    env = {"GEMINI_API_KEY": "gemini-secret"}
    assert ap.probe_google(env).status == "inconclusive"
    assert ap.probe_google(env).status == "ok"
    assert ap.probe_google(env).status == "ok"
    assert len(calls) == 2


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
    monkeypatch.setattr(ap, "_http_status", lambda url, headers, **_: 200)
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


@pytest.mark.parametrize(
    "probe,env",
    [
        (ap.probe_google, {"GEMINI_API_KEY": "gemini-secret"}),
        (ap.probe_anthropic, {"ANTHROPIC_API_KEY": "sk-ant-xxx"}),
    ],
)
def test_bad_status_line_is_inconclusive(monkeypatch, probe, env):
    # A broken proxy response must not crash a build in warn mode.
    def bad(req, timeout):
        raise http.client.BadStatusLine("garbage")

    monkeypatch.setattr(ap.urllib.request, "urlopen", bad)
    assert probe(env).status == "inconclusive"


def test_rotated_key_is_probed_fresh(monkeypatch):
    seen = []

    def fake(url, headers, **_):
        seen.append(headers["x-goog-api-key"])
        if headers["x-goog-api-key"] == "key-a":
            return 400, _KEY_INVALID
        return 200, ""

    monkeypatch.setattr(ap, "_http_get", fake)
    assert ap.probe_google({"GEMINI_API_KEY": "key-a"}).status == "auth_failed"
    assert ap.probe_google({"GEMINI_API_KEY": "key-b"}).status == "ok"
    assert seen == ["key-a", "key-b"]


def test_anthropic_probe_does_not_read_body(monkeypatch):
    calls = []

    def fake(url, headers, timeout, *, read_body=True):
        calls.append(read_body)
        return 200, ""

    monkeypatch.setattr(ap, "_http_get", fake)
    assert ap.probe_anthropic({"ANTHROPIC_API_KEY": "sk-ant-xxx"}).status == "ok"
    assert calls == [False]
