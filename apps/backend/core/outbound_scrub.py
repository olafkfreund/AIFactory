"""Outbound PII scrub, shared by both LLM call families (#320 / #1010 / #1128).

AIFactory reaches LLMs down two independent paths, and #1010 covered
neither of them completely:

1. ``providers/`` -- every ``BaseLLMProvider`` adapter (openai-compatible
   text + agentic, codex, antigravity, copilot, opencode, ollama, and the
   Claude adapter). Chokepoint: ``BaseLLMProvider.__init_subclass__``
   wraps each adapter's ``query()``.
2. The Claude Agent SDK directly -- ``core.client.create_client()`` and
   ``core.simple_client.create_simple_client()`` hand a ``ClaudeSDKClient``
   to ~20 modules (the coder and planner agents on the Claude default
   path, the spec pipeline, the GitHub triage / PR-review runners, the
   roadmap runner, insights, the merge resolver). None of these touch
   ``providers/`` at all. Chokepoint: ``wrap_client_outbound_scrub()``,
   applied inside those two factories.

Both chokepoints call ``scrub_outbound_prompt()`` here, so there is ONE
implementation of the scrub and ONE fail-closed contract.

What is deliberately NOT here
-----------------------------
The pattern set is not widened. #1010 restricted the outbound scrub to
the high-precision built-ins (hyphenated SSN, email, US phone,
Luhn-validated cards) and kept operator ``extraRedactionPatterns``
audit-scoped, because this is a code factory: prompts are full of
identifiers, UUIDs, semvers, CIDRs and hashes that a broad pattern would
mangle, corrupting the code the model is asked to write. That restraint
is preserved -- ``build_outbound_redactor()`` passes no extra patterns.

There is also exactly ONE escape hatch, the pre-existing
``LITELLM_AUDIT_SCRUB_OUTBOUND``. No second flag.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def scrub_outbound_enabled() -> bool:
    """Resolve the deployment default for the outbound scrub (#320).

    ``LITELLM_AUDIT_SCRUB_OUTBOUND`` is the kill-switch: set it to
    ``false`` / ``0`` / ``no`` / ``off`` to disable outbound scrubbing
    and restore the pre-#320 behaviour (audit-row redaction only). Any
    other value -- including unset -- leaves scrubbing ON.
    """
    raw = os.environ.get("LITELLM_AUDIT_SCRUB_OUTBOUND", "").strip().lower()
    # ponytail: kill-switch semantics -- only an explicit falsey value
    # disables; unset means default-on.
    return raw not in ("false", "0", "no", "off")


def build_outbound_redactor() -> Any:
    """Construct a ``PiiRedactor`` for the pre-send scrub (#210, #320).

    Lazy import, mirroring the audit hook. Unlike the audit hook, an
    ImportError here is NOT survivable: ``scrub_outbound_prompt`` turns
    it into a refusal to send, because a CLI / agent context without the
    redactor on PYTHONPATH would otherwise ship the raw prompt. Built-in
    patterns only. Cheap (regex compilation is microseconds); no caching
    keeps the operator reload path simple.
    """
    from services.llm_pii_redactor import PiiRedactor  # noqa: PLC0415

    return PiiRedactor(scrub_outbound=True)


def scrub_outbound_prompt(
    prompt: str,
    *,
    owner: str = "outbound",
    build_redactor: Callable[[], Any] | None = None,
) -> str:
    """Redact built-in PII from a prompt about to leave the process.

    Fail-CLOSED (#320): the scrub is enabled, so a missing or crashing
    redactor must NOT silently fall through to sending the raw prompt --
    that silent fail-open is exactly the PII-leak gap this guard closes.
    Log ERROR and abort the call. Operators who want prompts sent
    unredacted set ``LITELLM_AUDIT_SCRUB_OUTBOUND=false``.

    Args:
        prompt: The text about to be sent.
        owner: Caller name, for the operator log line.
        build_redactor: Redactor factory override. ``BaseLLMProvider``
            passes its own method so the per-adapter seam stays
            monkeypatchable; everything else takes the default.
    """
    try:
        redactor = (build_redactor or build_outbound_redactor)()
        scrubbed: str = redactor.redact_outbound(prompt)
    except Exception as exc:
        logger.error(
            "%s: outbound PII scrub is ENABLED but the redactor is "
            "unavailable (%s); refusing to send the prompt unredacted. Set "
            "LITELLM_AUDIT_SCRUB_OUTBOUND=false to disable outbound scrubbing.",
            owner,
            exc,
            exc_info=True,
        )
        raise RuntimeError(
            "Outbound PII scrub is enabled but the redactor is "
            "unavailable; refusing to send an unredacted prompt to "
            "the LLM provider (set LITELLM_AUDIT_SCRUB_OUTBOUND=false "
            "to disable outbound scrubbing)."
        ) from exc
    return scrubbed


class _ScrubbingClient:
    """Transparent proxy that scrubs the prompt on its way into the SDK.

    Same composition shape as ``_EnforcedClaudeSDKClient``: intercept
    ``query()``, forward everything else untouched. Applied INNERMOST
    (under the enforcement wrapper) so the audit row still records the
    raw prompt the caller passed while the wire carries the scrubbed one
    -- the provenance contract #1010 established.
    """

    def __init__(self, underlying: Any) -> None:
        self._underlying = underlying
        # Provenance for callers that want to audit what changed, mirroring
        # the attributes BaseLLMProvider records on adapters.
        self._outbound_prompt_raw: str | None = None
        self._outbound_prompt_scrubbed: bool = False

    async def query(self, prompt: str, *args: Any, **kwargs: Any) -> Any:
        self._outbound_prompt_raw = prompt
        outbound = prompt
        if scrub_outbound_enabled():
            outbound = scrub_outbound_prompt(
                prompt, owner=type(self._underlying).__name__
            )
        self._outbound_prompt_scrubbed = outbound != prompt
        return await self._underlying.query(outbound, *args, **kwargs)

    def receive_response(self) -> Any:
        return self._underlying.receive_response()

    async def __aenter__(self) -> _ScrubbingClient:
        await self._underlying.__aenter__()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> Any:
        return await self._underlying.__aexit__(exc_type, exc_val, exc_tb)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._underlying, name)


def wrap_client_outbound_scrub(client: Any) -> Any:
    """Wrap a Claude SDK client so its prompts are scrubbed before egress.

    Unconditional: the kill-switch is checked per call inside the proxy
    rather than at wrap time, so flipping ``LITELLM_AUDIT_SCRUB_OUTBOUND``
    does not require restarting long-lived agent sessions. Wrapping twice
    is harmless (redaction is idempotent) but pointless, so it is skipped.
    """
    if isinstance(client, _ScrubbingClient):
        return client
    return _ScrubbingClient(underlying=client)


__all__ = [
    "build_outbound_redactor",
    "scrub_outbound_enabled",
    "scrub_outbound_prompt",
    "wrap_client_outbound_scrub",
]
