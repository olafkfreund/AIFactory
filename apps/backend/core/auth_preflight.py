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

import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Literal

_ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models?limit=1"
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


def preflight_mode(env: dict[str, str] | None = None) -> Mode:
    """Resolve the pre-flight mode from ``AIFACTORY_AUTH_PREFLIGHT`` (default warn)."""
    source = os.environ if env is None else env
    raw = (source.get("AIFACTORY_AUTH_PREFLIGHT") or "warn").strip().lower()
    if raw in ("off", "0", "false", "no"):
        return "off"
    if raw in ("enforce", "strict", "block"):
        return "enforce"
    return "warn"


def _http_status(url: str, headers: dict[str, str]) -> int:
    """GET ``url`` and return the HTTP status code (raising URLError on transport
    failure so the caller can classify it as inconclusive)."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def probe_anthropic(env: dict[str, str] | None = None) -> PreflightResult:
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

    try:
        code = _http_status(_ANTHROPIC_MODELS_URL, headers)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
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


# Model-name prefixes → the provider whose credential the build will use.
# Slice 1 (#611) probes anthropic only (the demo's silent-empty-build cause and
# the pinned planning provider); gemini/openai probes are a follow-up.
def providers_for_models(models: list[str]) -> list[str]:
    """Return the distinct probeable providers implied by ``models``."""
    out: list[str] = []
    for m in models:
        name = (m or "").lower()
        if name.startswith("claude") and "anthropic" not in out:
            out.append("anthropic")
    return out


_PROBES = {"anthropic": probe_anthropic}


def run_auth_preflight(
    models: list[str], env: dict[str, str] | None = None
) -> list[PreflightResult]:
    """Probe every probeable provider implied by ``models``. Pure — callers
    decide what to do with the results based on :func:`preflight_mode`."""
    results: list[PreflightResult] = []
    for provider in providers_for_models(models):
        probe = _PROBES.get(provider)
        if probe is not None:
            results.append(probe(env))
    return results
