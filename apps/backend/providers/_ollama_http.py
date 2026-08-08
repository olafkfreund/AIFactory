"""Shared synchronous Ollama HTTP helpers for the plain + agentic providers.

Both ``OllamaProvider`` and ``OllamaAgenticProvider`` need identical
synchronous request/health-check helpers (run via ``asyncio.to_thread``). They
live here as a mixin so the two providers can't drift.
"""

from __future__ import annotations

import contextlib
import json
import os
import urllib.error
import urllib.request
from typing import Any

_PATH_TAGS: str = "/api/tags"

DEFAULT_OLLAMA_BASE_URL: str = "http://localhost:11434"

# Env vars naming the Ollama endpoint, in precedence order. ``OLLAMA_HOST`` is
# Ollama's own client variable and is accepted last so an operator who has set
# only that is not surprised by localhost.
_BASE_URL_ENV: tuple[str, ...] = ("OLLAMA_BASE_URL", "OLLAMA_API_URL", "OLLAMA_HOST")

_API_KEY_ENV: tuple[str, ...] = ("OLLAMA_API_KEY",)


def _first_env(names: tuple[str, ...]) -> str | None:
    for name in names:
        val = os.environ.get(name)
        if val and val.strip():
            return val.strip()
    return None


def resolve_ollama_base_url() -> str:
    """The Ollama endpoint from the environment, else localhost (#1099).

    THE resolver for the Ollama endpoint — the providers, the low-tier probe and
    anything else that needs it call this one, so a deployment that sets
    ``OLLAMA_BASE_URL`` cannot be honoured by one caller and ignored by another.

    That was the bug: neither Ollama provider read any environment variable, so
    ``OllamaAgenticProvider`` defaulted to ``http://localhost:11434`` and a run
    pinned to ``ollama:<model>`` silently tried localhost inside a pod, where
    there is no Ollama. The endpoint was only reachable by spelling the phase
    ``openai-compatible:`` instead.
    """
    return (_first_env(_BASE_URL_ENV) or DEFAULT_OLLAMA_BASE_URL).rstrip("/")


def resolve_ollama_api_key() -> str | None:
    """Bearer token for a hosted Ollama endpoint, or None for a local one.

    ``OLLAMA_API_KEY`` was declared as build-Job env passthrough and read by
    nothing, so a hosted endpoint was called unauthenticated (#1099 C4).
    """
    return _first_env(_API_KEY_ENV)


class OllamaHTTPMixin:
    """Synchronous HTTP helpers shared by the Ollama providers.

    Both providers assign ``_base_url`` and ``_timeout`` in ``__init__``; they
    are declared here as annotations (no value) so the mixin's methods
    type-check under ``mypy --strict`` without creating real class attributes.
    ``_api_key`` is optional and defaults to None on the class so a provider
    that never sets it still type-checks and sends no Authorization header.
    """

    _base_url: str
    _timeout: int
    _api_key: str | None = None

    def _auth_headers(self) -> dict[str, str]:
        """``Authorization`` for a hosted endpoint; empty for a local one.

        HTTPS only, deliberately. ``OLLAMA_API_KEY`` is a deployment-wide value
        while the endpoint is chosen per run, so keying the header purely on
        "a token is configured" would send a hosted ollama.com credential to
        whatever plaintext box ``OLLAMA_BASE_URL`` happened to name — the fleet's
        own self-hosted server is plain http and needs no token at all. Scheme is
        the one signal that separates the two without guessing at hostnames, and
        it also keeps the token off the wire in clear text.

        An internal endpoint that genuinely requires auth should terminate TLS;
        passing ``api_key=`` explicitly to the constructor does not bypass this
        (the same rule applies), which is intended.
        """
        if not self._api_key or not self._base_url.lower().startswith("https://"):
            return {}
        return {"Authorization": f"Bearer {self._api_key}"}

    def _http_post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Synchronous HTTP POST to the Ollama API.

        This is intentionally synchronous so it can be run in a thread
        via ``asyncio.to_thread()`` without blocking the event loop.

        Args:
            url: Full URL to POST to (e.g. ``"http://localhost:11434/api/chat"``).
            payload: JSON-serialisable request body dict.

        Returns:
            Parsed JSON response as a Python dict.

        Raises:
            RuntimeError: On HTTP errors or JSON decode failures.
        """
        body_bytes = json.dumps(payload).encode("utf-8")
        # URL is built from the configured Ollama base_url (http/https), not
        # untrusted input — the urllib scheme audit (S310) does not apply.
        req = urllib.request.Request(  # noqa: S310
            url,
            data=body_bytes,
            headers={"Content-Type": "application/json", **self._auth_headers()},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            error_body = ""
            with contextlib.suppress(Exception):
                error_body = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(
                f"Ollama API HTTP error {exc.code}: {exc.reason}. Response body: {error_body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Cannot reach Ollama server at '{self._base_url}': {exc.reason}. "
                "Ensure Ollama is running (ollama serve) and base_url is correct."
            ) from exc

        try:
            parsed: dict[str, Any] = json.loads(raw.decode("utf-8", errors="replace"))
            return parsed
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Ollama API returned invalid JSON: {exc}") from exc

    def _verify_connection(self) -> None:
        """Synchronous health check — verify the Ollama server is reachable.

        Issues a lightweight GET to ``/api/tags`` (lists available models).
        Raises ``RuntimeError`` if the server cannot be reached.  Called
        from ``__aenter__`` via ``asyncio.to_thread()``.

        Raises:
            RuntimeError: If the server is unreachable or returns an error.
        """
        url = f"{self._base_url}{_PATH_TAGS}"
        req = urllib.request.Request(  # noqa: S310
            url, headers=self._auth_headers(), method="GET"
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                resp.read()
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Cannot reach Ollama server at '{self._base_url}': {exc.reason}. "
                "Ensure Ollama is running (ollama serve) and base_url is correct."
            ) from exc
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"Ollama server health check failed — HTTP {exc.code}: {exc.reason}."
            ) from exc
