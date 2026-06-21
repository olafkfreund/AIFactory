"""Tests for ``pfactory.tiers.resolve_low_tier_model`` (RFC-0011 #634).

The Ollama probe HTTP layer (``urllib.request.urlopen``) is mocked so the test
never touches the network. Covers: Ollama reachable -> ``ollama:<first model>``;
Ollama unreachable / timeout -> ``haiku``; empty model list -> ``haiku``;
malformed payload -> ``haiku``; env-var precedence for the base URL.
"""

from __future__ import annotations

import io
import json
from contextlib import contextmanager
from unittest import mock

from pfactory import tiers


@contextmanager
def _fake_urlopen(payload, captured_urls=None):
    """Patch urllib so urlopen returns ``payload`` (dict -> JSON) or raises it."""

    def _open(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        if captured_urls is not None:
            captured_urls.append(url)
        if isinstance(payload, Exception):
            raise payload
        body = json.dumps(payload).encode("utf-8")

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.close()
                return False

        return _Resp(body)

    with mock.patch.object(tiers.urllib.request, "urlopen", _open):
        yield


def test_returns_ollama_first_model_when_reachable(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_API_URL", raising=False)
    payload = {"models": [{"name": "qwen3:14b"}, {"name": "llama3"}]}
    with _fake_urlopen(payload):
        assert tiers.resolve_low_tier_model() == "ollama:qwen3:14b"


def test_falls_back_to_haiku_when_unreachable(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_API_URL", raising=False)
    with _fake_urlopen(OSError("connection refused")):
        assert tiers.resolve_low_tier_model() == "haiku"


def test_falls_back_to_haiku_on_empty_model_list(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_API_URL", raising=False)
    with _fake_urlopen({"models": []}):
        assert tiers.resolve_low_tier_model() == "haiku"


def test_falls_back_to_haiku_on_malformed_payload(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_API_URL", raising=False)
    with _fake_urlopen({"unexpected": "shape"}):
        assert tiers.resolve_low_tier_model() == "haiku"
    with _fake_urlopen(["not", "a", "dict"]):
        assert tiers.resolve_low_tier_model() == "haiku"


def test_never_raises_on_arbitrary_exception(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_API_URL", raising=False)
    with _fake_urlopen(RuntimeError("boom")):
        assert tiers.resolve_low_tier_model() == "haiku"


def test_base_url_env_precedence(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://primary:11434/")
    monkeypatch.setenv("OLLAMA_API_URL", "http://secondary:11434")
    captured: list[str] = []
    with _fake_urlopen({"models": [{"name": "m"}]}, captured_urls=captured):
        tiers.resolve_low_tier_model()
    # OLLAMA_BASE_URL wins; trailing slash trimmed; /api/tags appended.
    assert captured == ["http://primary:11434/api/tags"]


def test_base_url_falls_back_to_api_url(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.setenv("OLLAMA_API_URL", "http://secondary:11434")
    captured: list[str] = []
    with _fake_urlopen({"models": [{"name": "m"}]}, captured_urls=captured):
        tiers.resolve_low_tier_model()
    assert captured == ["http://secondary:11434/api/tags"]


def test_base_url_default_localhost(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_API_URL", raising=False)
    captured: list[str] = []
    with _fake_urlopen({"models": [{"name": "m"}]}, captured_urls=captured):
        tiers.resolve_low_tier_model()
    assert captured == ["http://localhost:11434/api/tags"]
