"""LiteLLM gateway helpers (Epic #35 #38 PR-1).

Centralises the gateway-redirect logic for Python-HTTP providers:

- ``resolve_base_url(native_default)`` reads ``LITELLM_GATEWAY_URL``
  and returns it when set; otherwise the provider's native default.
- ``LiteLLMGatewayUnavailableError`` is raised when a request to the
  gateway exhausts the circuit-breaker retry budget. Distinguished
  from generic ``URLError`` so the agent sees a clear error.

Scope (per the locked #38 design doc)

Only Python-HTTP-based providers fit this pattern:

- ``openai_compatible.py`` — OpenAI / OpenAI-compat servers (LM
  Studio / vLLM / OpenRouter / Together / Groq)
- ``ollama.py`` — local Ollama HTTP

CLI-spawn providers (Claude SDK via ``claude``, Codex CLI, Gemini
CLI) are out of scope for v1.1: the subprocess speaks the native
provider's format which LiteLLM's OpenAI-compatible endpoint can't
translate. Documented in the design doc + concept doc.

Failure-safe contract

Per the design's reviewer-finding-#1 (circuit breaker): transient
errors (single-pod restart, brief DNS flap) recover via a 3-retry
exponential backoff (100ms / 200ms / 400ms — total 700ms worst-case
budget). Sustained outages exhaust the retries + raise
``LiteLLMGatewayUnavailableError``. No silent fallback to direct
provider calls (that would bypass audit/budget/allowlist enforcement).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


_ENV_VAR = "LITELLM_GATEWAY_URL"


def is_gateway_enabled() -> bool:
    """True when the operator has configured a LiteLLM gateway."""
    return bool(os.environ.get(_ENV_VAR, "").strip())


def resolve_base_url(native_default: str) -> str:
    """Return the URL the provider should hit for HTTP calls.

    When ``LITELLM_GATEWAY_URL`` is set (operator opted into the
    gateway via Helm), all provider calls route through it. Otherwise
    the provider's native API endpoint applies.

    Args:
        native_default: The provider's own default endpoint (e.g.
            ``https://api.openai.com`` for openai_compatible).

    Returns:
        Either the gateway URL or ``native_default``; never returns
        an empty string.
    """
    gateway = os.environ.get(_ENV_VAR, "").strip()
    if gateway:
        logger.debug(
            "provider routing via LiteLLM gateway: %s (native default was %s)",
            gateway,
            native_default,
        )
        return gateway
    return native_default


class LiteLLMGatewayUnavailableError(RuntimeError):
    """The LiteLLM gateway is unreachable after the circuit-breaker
    retry budget exhausted.

    Distinct from generic ``URLError`` / ``HTTPError`` so callers can
    surface the operator-actionable diagnostic clearly. The error
    message includes the gateway URL + an instruction pointing at
    ``kubectl logs -n aifactory -l app=litellm``.

    Raised ONLY when the gateway is enabled (``LITELLM_GATEWAY_URL``
    is set) AND the retry budget exhausted. Direct-provider calls
    raise the underlying URLError unchanged.
    """

    def __init__(self, gateway_url: str, underlying: Exception) -> None:
        super().__init__(
            f"LiteLLM gateway unreachable at {gateway_url} after "
            f"circuit-breaker retries exhausted: {underlying!r}. "
            f"Check gateway status: "
            f"kubectl logs -n aifactory -l app=litellm"
        )
        self.gateway_url = gateway_url
        self.underlying = underlying
