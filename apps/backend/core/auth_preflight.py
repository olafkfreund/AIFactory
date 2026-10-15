"""Build-time auth pre-flight (RFC-0008 §3.2 item a, #611).

The 2026-06-18 taskboard demo built nothing useful because the
``CLAUDE_CODE_OAUTH_TOKEN`` had expired: the credential was *present* (so the
config check in ``provider_health.py`` passed) but *invalid*, and the build
silently produced an empty result that escalated to human review.

This module adds a **live** credential probe run just before a build starts. It
is distinct from ``provider_health.provider_credential_health`` (a quota-free
*configuration* check): this one makes one cheap, generation-free API call and
classifies the credential as ``ok`` / ``auth_failed`` / ``inconclusive``.

Rollout safety — three modes via ``AIFACTORY_AUTH_PREFLIGHT``:

- ``off``      → skip entirely.
- ``warn``     → probe and log, but never block the build (default — zero risk).
- ``enforce``  → abort with a named error on a definitive ``auth_failed``.

Default is ``warn`` so turning the probe on can never false-abort a valid build
(important for the OAuth path, which can't be verified offline). Headless /
benchmark runs flip to ``enforce`` for fail-fast behaviour. ``inconclusive``
(network error, 5xx, timeout) never blocks in any mode.
"""

from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

_ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models?limit=1"
_GOOGLE_MODELS_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1"
)
_ANTHROPIC_VERSION = "2023-06-01"
# Beta header the Claude Code OAuth token is presented with against the API.
_OAUTH_BETA = "oauth-2025-04-20"
_TIMEOUT_S = 10

Status = Literal["ok", "auth_failed", "inconclusive", "skipped"]
Mode = Literal["off", "warn", "enforce"]


@dataclass(frozen=True)
class PreflightResult:
    """Outcome of probing one provider's credential."""

    provider: str
    status: Status
    detail: str
    credential_kind: str = ""  # "api_key" | "oauth" | ""

    @property
    def is_auth_failure(self) -> bool:
        return self.status == "auth_failed"


# Definite results (ok / auth_failed) per (provider, credential), so a handoff
# does not re-probe every time. Inconclusive results are never cached. Keyed on
# the raw credential: in-process only (os.environ already holds it), never
# logged; a fast hash of it trips CodeQL py/weak-sensitive-data-hashing.
_CACHE_TTL_S = 300
_cache: dict[tuple[str, str], tuple[float, PreflightResult]] = {}


def _cached(
    provider: str, secret: str, probe: Callable[[], PreflightResult]
) -> PreflightResult:
    key = (provider, secret)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL_S:
        return hit[1]
    result = probe()
    if result.status in ("ok", "auth_failed"):
        _cache[key] = (time.monotonic(), result)
    return result


def preflight_mode(env: dict[str, str] | None = None) -> Mode:
    """Resolve the pre-flight mode from ``AIFACTORY_AUTH_PREFLIGHT`` (default warn)."""
    source = os.environ if env is None else env
    raw = (source.get("AIFACTORY_AUTH_PREFLIGHT") or "warn").strip().lower()
    if raw in ("off", "0", "false", "no"):
        return "off"
    if raw in ("enforce", "strict", "block"):
        return "enforce"
    return "warn"


def _http_status(url: str, headers: dict[str, str], timeout: float = _TIMEOUT_S) -> int:
    """GET ``url`` and return the HTTP status code (raising URLError on transport
    failure so the caller can classify it as inconclusive). Never reads the body."""
    return _http_get(url, headers, timeout, read_body=False)[0]


def _read_body(resp: Any) -> str:
    """First 2000 chars of a response body; "" when the read itself fails."""
    try:
        return str(resp.read().decode("utf-8", errors="replace"))[:2000]
    except (OSError, http.client.HTTPException):
        return ""


def _http_get(
    url: str,
    headers: dict[str, str],
    timeout: float = _TIMEOUT_S,
    *,
    read_body: bool = True,
) -> tuple[int, str]:
    """GET ``url`` and return the HTTP status and (optionally) the body."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, _read_body(resp) if read_body else ""
    except urllib.error.HTTPError as exc:
        return exc.code, _read_body(exc) if read_body else ""


def probe_anthropic(
    env: dict[str, str] | None = None, timeout: float = _TIMEOUT_S
) -> PreflightResult:
    """Probe the anthropic credential with a generation-free ``GET /v1/models``.

    Prefers ``ANTHROPIC_API_KEY`` (verified: 200 valid / 401 invalid via the
    ``x-api-key`` header) and otherwise probes ``CLAUDE_CODE_OAUTH_TOKEN`` via
    the ``authorization: Bearer`` + oauth-beta header. Any non-auth outcome
    (network/5xx/unexpected) is ``inconclusive`` and never blocks.
    """
    source = os.environ if env is None else env
    base_headers = {"anthropic-version": _ANTHROPIC_VERSION}

    api_key = (source.get("ANTHROPIC_API_KEY") or "").strip()
    oauth = (source.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()

    if api_key:
        headers = {**base_headers, "x-api-key": api_key}
        kind = "api_key"
    elif oauth:
        headers = {
            **base_headers,
            "authorization": f"Bearer {oauth}",
            "anthropic-beta": _OAUTH_BETA,
        }
        kind = "oauth"
    else:
        return PreflightResult(
            "anthropic", "skipped", "no anthropic credential in environment"
        )

    return _cached(
        "anthropic",
        api_key or oauth,
        lambda: _classify_anthropic(headers, kind, timeout),
    )


def _classify_anthropic(
    headers: dict[str, str], kind: str, timeout: float
) -> PreflightResult:
    try:
        code = _http_status(_ANTHROPIC_MODELS_URL, headers, timeout=timeout)
    except ValueError:  # e.g. a CR/LF in the credential; never echo it
        return PreflightResult(
            "anthropic", "inconclusive", "credential is not a valid header", kind
        )
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        http.client.HTTPException,
    ) as exc:
        return PreflightResult(
            "anthropic", "inconclusive", f"probe transport error: {exc}", kind
        )

    if code == 200:
        return PreflightResult("anthropic", "ok", "credential accepted (200)", kind)
    if code in (401, 403):
        return PreflightResult(
            "anthropic",
            "auth_failed",
            f"credential rejected ({code}) — expired or invalid "
            f"{'ANTHROPIC_API_KEY' if kind == 'api_key' else 'CLAUDE_CODE_OAUTH_TOKEN'}",
            kind,
        )
    return PreflightResult(
        "anthropic", "inconclusive", f"unexpected status {code}", kind
    )


def _google_key_invalid(body: str) -> bool:
    """True only when Google's error JSON names reason ``API_KEY_INVALID``."""
    try:
        err = json.loads(body).get("error")
    except (ValueError, AttributeError):
        return False
    details = err.get("details") if isinstance(err, dict) else None
    return isinstance(details, list) and any(
        isinstance(d, dict) and d.get("reason") == "API_KEY_INVALID" for d in details
    )


def probe_google(
    env: dict[str, str] | None = None, timeout: float = _TIMEOUT_S
) -> PreflightResult:
    """Probe ``GEMINI_API_KEY`` with a generation-free models request.

    Skipped when the CLI is on Vertex/ADC, where the key is not what it uses.
    ``GOOGLE_API_KEY`` is not probed: other components (the graphiti embedder)
    use it, so its presence says nothing about the gemini CLI.
    """
    source = os.environ if env is None else env
    if (source.get("GOOGLE_GENAI_USE_VERTEXAI") or "").strip() or (
        source.get("GOOGLE_APPLICATION_CREDENTIALS") or ""
    ).strip():
        return PreflightResult(
            "google", "skipped", "gemini uses Vertex/ADC credentials", "adc"
        )
    key = (source.get("GEMINI_API_KEY") or "").strip()
    if not key:
        return PreflightResult(
            "google", "skipped", "no GEMINI_API_KEY in environment", "api_key"
        )
    return _cached("google", key, lambda: _classify_google(key, timeout))


def _classify_google(key: str, timeout: float) -> PreflightResult:
    try:
        code, body = _http_get(
            _GOOGLE_MODELS_URL, {"x-goog-api-key": key}, timeout=timeout
        )
    except ValueError:  # e.g. a CR/LF in the key; never echo it
        return PreflightResult(
            "google", "inconclusive", "credential is not a valid header", "api_key"
        )
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        http.client.HTTPException,
    ):
        return PreflightResult(
            "google", "inconclusive", "probe transport error", "api_key"
        )

    if code == HTTPStatus.OK:
        return PreflightResult("google", "ok", "credential accepted (200)", "api_key")
    # Only a 400 naming API_KEY_INVALID is a definite rejection. A 403 can be
    # SERVICE_DISABLED or an IP/service block on a valid key, so it is not.
    if code == HTTPStatus.BAD_REQUEST and _google_key_invalid(body):
        return PreflightResult(
            "google",
            "auth_failed",
            f"credential rejected ({code} API_KEY_INVALID) — invalid GEMINI_API_KEY",
            "api_key",
        )
    return PreflightResult(
        "google", "inconclusive", f"unexpected status {code}", "api_key"
    )


# Model-name prefixes → the provider whose credential the build will use.
# Anthropic and Google credentials can be checked before a build starts.
def providers_for_models(models: list[str]) -> list[str]:
    """Return the distinct probeable providers implied by ``models``."""
    out: list[str] = []
    for m in models:
        name = (m or "").lower()
        if name.startswith("claude") and "anthropic" not in out:
            out.append("anthropic")
        elif name.startswith("gemini") and "google" not in out:
            out.append("google")
    return out


_PROBES = {"anthropic": probe_anthropic, "google": probe_google}


def run_auth_preflight(
    models: list[str],
    env: dict[str, str] | None = None,
    timeout: float = _TIMEOUT_S,
) -> list[PreflightResult]:
    """Probe every probeable provider implied by ``models``. Pure — callers
    decide what to do with the results based on :func:`preflight_mode`."""
    results: list[PreflightResult] = []
    for provider in providers_for_models(models):
        probe = _PROBES.get(provider)
        if probe is not None:
            results.append(probe(env, timeout))
    return results
