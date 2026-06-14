"""Per-provider billing-mode classification (#96).

The cockpit must not show a dollar cost for work that isn't metered in dollars.
A Claude/Codex/Antigravity *subscription* still makes the SDK report a *notional*
API-equivalent ``cost_usd`` — real money was not spent, so surfacing it as cost is
misleading. A local Ollama model costs no dollars at all (only GPU/wall time). Only
true metered usage — a provider driven by an API key, or an Ollama *cloud* model —
is real spend.

This module classifies a worker's provider into one of four billing modes so the
completion event can carry it and CFactory can show the right metric:

    api          metered — a provider authenticated with an API key   -> show cost
    cloud        metered — a remote/cloud Ollama endpoint              -> show cost
    subscription flat-rate — Claude/Codex/Antigravity/Copilot on a sub -> tokens + time
    local        local Ollama (localhost)                              -> GPU/wall time
    unknown      can't tell                                            -> CFactory falls
                                                                          back to cost>0

The signal is "inferred, zero-config" (the chosen policy): provider name +
presence of the provider's API-key env var + the Ollama endpoint. No operator
config to maintain; everything is derived from how the run is already authenticated.
"""

from __future__ import annotations

import os
from typing import Mapping
from urllib.parse import urlparse

# Providers that run on a flat subscription / OAuth when no API key is present.
_SUBSCRIPTION_PROVIDERS = {
    "claude",
    "codex",
    "antigravity",
    "copilot",
    "opencode",
    "gemini",
}

# For a subscription-capable provider, the presence of any of these env vars means
# it is instead billing via a metered API key (so the mode is "api", not "subscription").
# Copilot/opencode are subscription-only (their token is the subscription, not metered).
_API_KEY_ENV: dict[str, tuple[str, ...]] = {
    "claude": ("ANTHROPIC_API_KEY",),
    "codex": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "antigravity": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "copilot": (),
    "opencode": (),
}

# Providers that are inherently key-based / metered (no subscription path).
_METERED_PROVIDERS = {"openai-compatible", "openai", "github-models", "azure"}

_OLLAMA = {"ollama", "local-ollama", "local"}
_OLLAMA_HOST_ENV = ("OLLAMA_HOST", "OLLAMA_BASE_URL")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", ""}


def _is_local_host(base: str) -> bool:
    """True when ``base`` points at the local machine (so Ollama runs on local GPU)."""
    raw = (base or "").strip()
    if raw in _LOCAL_HOSTS:
        return True
    # Tolerate bare host or full URL.
    host = urlparse(raw if "://" in raw else f"//{raw}").hostname
    return (host or "").lower() in _LOCAL_HOSTS


def classify_billing_mode(
    provider: str | None, env: Mapping[str, str] | None = None
) -> str:
    """Classify a provider into ``api`` | ``cloud`` | ``subscription`` | ``local`` |
    ``unknown``. Pure + env-driven (defaults to ``os.environ``) so it is trivially
    testable and has no config to maintain."""
    e = env if env is not None else os.environ
    p = (provider or "").strip().lower()
    if not p:
        return "unknown"
    if p in _OLLAMA:
        base = next((e[k] for k in _OLLAMA_HOST_ENV if e.get(k)), "")
        return "local" if _is_local_host(base) else "cloud"
    if p in _METERED_PROVIDERS:
        return "api"
    if p in _SUBSCRIPTION_PROVIDERS:
        keys = _API_KEY_ENV.get(p, ())
        return "api" if any((e.get(k) or "").strip() for k in keys) else "subscription"
    return "unknown"


# A mode is "metered" when it costs real dollars (so cost should be shown).
_METERED_MODES = {"api", "cloud"}


def is_metered(mode: str | None) -> bool:
    """Whether a billing mode represents real dollar spend (show cost) vs a
    subscription/local mode (show tokens + time)."""
    return (mode or "").strip().lower() in _METERED_MODES
