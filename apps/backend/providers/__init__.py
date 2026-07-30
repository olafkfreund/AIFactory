"""
LLM Provider Abstraction Layer
================================

Top-level package for all LLM provider adapters.  Generalised from the
original ``qa/providers/`` package to support any execution phase — not
just QA review.

Defines the minimal interface (``BaseLLMProvider``) that any LLM backend must
satisfy to replace ``ClaudeSDKClient`` inside agent sessions.

Minimal interface
-----------------
Callers consume exactly **two methods** plus the **async context manager**
protocol:

1. ``query(prompt: str) -> None``
   Send the initial prompt and start the response stream.

2. ``receive_response() -> AsyncIterator[Any]``
   Stream back structured message objects.  Each object is inspected
   *only* via ``type(msg).__name__`` string comparisons — no ``isinstance``
   calls — so adapters must yield objects whose class names match exactly:

   Top-level: ``AssistantMessage``, ``UserMessage``
   Blocks:    ``TextBlock``, ``ToolUseBlock``, ``ToolResultBlock``

3. Async context manager (``__aenter__`` / ``__aexit__``)
   Callers always wrap providers in ``async with provider:`` for resource
   management.

Package layout
--------------
    providers/
        __init__.py         — BaseLLMProvider ABC (this file)
        types.py            — Shared message-protocol wrapper classes
        claude.py           — ClaudeProvider   (wraps ClaudeSDKClient)
        codex.py            — CodexCLIProvider  (Codex CLI text-only)
        codex_agentic.py    — CodexAgenticProvider (Codex CLI full-auto)
        antigravity.py      — AntigravityCLIProvider (Antigravity CLI text-only)
        antigravity_agentic.py — AntigravityAgenticProvider (Antigravity --yolo)
        gemini.py / gemini_agentic.py — back-compat shims for the above
        ollama.py           — OllamaProvider   (local Ollama text-only adapter)
        ollama_agentic.py   — OllamaAgenticProvider (native tool calling)
        opencode.py         — OpenCodeProvider (OpenCode CLI runtime, text entry)
        opencode_agentic.py — OpenCodeAgenticProvider (OpenCode CLI ``run`` mode)
        factory.py          — Unified get_provider() + legacy get_qa_llm_provider()

Usage::

    from providers.factory import get_provider

    provider = get_provider("codex", phase="coding", model="gpt-5.3-codex",
                            working_dir=project_dir)
    async with provider:
        await provider.query(prompt)
        async for msg in provider.receive_response():
            ...
"""

from __future__ import annotations

import functools
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from .types import (
    AssistantMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Outbound PII scrub (#320 / #1010 / #1128)
# ---------------------------------------------------------------------------


def _env_scrub_outbound_default() -> bool:
    """Resolve the deployment default for the outbound scrub (#320).

    Outbound PII scrubbing defaults ON. The built-in redactor set is
    deliberately high-precision — hyphenated SSN, email, US phone, and
    Luhn-validated credit cards — and does NOT touch bare code
    identifiers, so it is safe to run on prompts that legitimately
    carry source code (this is a code factory; over-broad redaction
    would corrupt prompts and wreck output quality). Operator-supplied
    ``extraRedactionPatterns`` are NOT applied outbound (they may be
    broad); only the built-in safe set is scrubbed before egress — see
    ``BaseLLMProvider._build_outbound_redactor``.

    ``LITELLM_AUDIT_SCRUB_OUTBOUND`` is the ONE kill-switch: set it to
    ``false`` / ``0`` / ``no`` / ``off`` to disable outbound scrubbing
    and restore the pre-#320 behaviour (audit-row redaction only). Any
    other value — including unset — leaves scrubbing ON.
    """
    raw = os.environ.get("LITELLM_AUDIT_SCRUB_OUTBOUND", "").strip().lower()
    # ponytail: kill-switch semantics — only an explicit falsey value
    # disables; unset means default-on.
    return raw not in ("false", "0", "no", "off")


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------


class BaseLLMProvider(ABC):
    """
    Minimal interface every LLM provider adapter must satisfy.

    Concrete implementations live in the sibling modules:
    - ``providers.claude``          — wraps ClaudeSDKClient (default)
    - ``providers.codex``           — Codex CLI text-only
    - ``providers.codex_agentic``   — Codex CLI full-auto (agentic)
    - ``providers.antigravity``     — Antigravity CLI text-only
    - ``providers.antigravity_agentic`` — Antigravity CLI ``--yolo`` (agentic)
    - ``providers.ollama``          — local Ollama / OpenAI-compatible

    Outbound PII scrub (#1128): ``__init_subclass__`` wraps every
    subclass's ``query()`` so no adapter can send an unscrubbed prompt.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Wrap the subclass's ``query()`` with the outbound PII scrub (#1128).

        #1010 landed the scrub inside ``OpenAICompatibleProvider`` only,
        so the agentic path and every CLI adapter (claude, codex,
        antigravity, copilot, opencode, ollama) sent prompts verbatim —
        an audit control that reads as satisfied while covering a
        minority of calls.

        ``query()`` is the ONE place a prompt enters any adapter: HTTP
        adapters stash it and build the payload later, CLI adapters
        stash it and build argv later, ``ClaudeProvider`` forwards it
        straight to the SDK. Wrapping it here therefore covers every
        adapter that exists AND every adapter added later, with no
        per-adapter opt-in that a new adapter can forget.

        The wrapper records, on the instance:

        - ``_outbound_prompt_raw`` — the prompt as the caller passed it,
          so audit rows can still show what the user typed.
        - ``_outbound_prompt_scrubbed`` — True only when redaction
          actually changed the text.

        An adapter may pin the behaviour by setting ``_scrub_outbound``
        in ``__init__`` (``OpenAICompatibleProvider`` does, for its
        ``scrub_outbound=`` kwarg); otherwise the deployment default
        applies.
        """
        super().__init_subclass__(**kwargs)

        query = cls.__dict__.get("query")
        if query is None or getattr(query, "__outbound_scrub__", False):
            return

        @functools.wraps(query)
        async def _scrubbing_query(
            self: Any, prompt: str, *args: Any, **kwargs: Any
        ) -> Any:
            enabled = getattr(self, "_scrub_outbound", None)
            if enabled is None:
                enabled = _env_scrub_outbound_default()
            self._outbound_prompt_raw = prompt
            outbound = self._scrub_outbound_prompt(prompt) if enabled else prompt
            self._outbound_prompt_scrubbed = outbound != prompt
            return await query(self, outbound, *args, **kwargs)

        setattr(_scrubbing_query, "__outbound_scrub__", True)  # noqa: B010
        setattr(cls, "query", _scrubbing_query)

    def _build_outbound_redactor(self) -> Any:
        """Construct a ``PiiRedactor`` for the pre-send scrub (#210, #320).

        Lazy import, mirroring the audit hook. Unlike the audit hook, an
        ImportError here is NOT survivable: ``_scrub_outbound_prompt``
        turns it into a refusal to send (#320 fail-closed), because a
        CLI / agent context without the redactor on PYTHONPATH would
        otherwise ship the raw prompt. Built-in patterns only —
        operator ``extraRedactionPatterns`` stay audit-scoped (they can
        be broad and would corrupt code prompts). Cheap (regex
        compilation is microseconds); no caching keeps the operator
        reload path simple.
        """
        from services.llm_pii_redactor import PiiRedactor

        return PiiRedactor(scrub_outbound=True)

    def _scrub_outbound_prompt(self, prompt: str) -> str:
        """Redact built-in PII from a prompt that is about to leave the process.

        Fail-CLOSED (#320): the scrub is enabled, so a missing or
        crashing redactor must NOT silently fall through to sending the
        raw prompt — that silent fail-open is exactly the PII-leak gap
        this guard closes. Log ERROR and abort the call. Operators who
        want prompts sent unredacted set the kill-switch
        ``LITELLM_AUDIT_SCRUB_OUTBOUND=false``.
        """
        try:
            redactor = self._build_outbound_redactor()
            scrubbed: str = redactor.redact_outbound(prompt)
        except Exception as exc:
            logger.error(
                "%s: outbound PII scrub is ENABLED but the redactor is "
                "unavailable (%s); refusing to send the prompt unredacted. "
                "Set LITELLM_AUDIT_SCRUB_OUTBOUND=false to disable outbound "
                "scrubbing.",
                type(self).__name__,
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

    @abstractmethod
    async def query(self, prompt: str) -> None:
        """Send a prompt to the LLM to start a response stream."""

    @abstractmethod
    def receive_response(self) -> AsyncIterator[Any]:
        """Return an async iterable of message objects produced by the LLM."""

    @abstractmethod
    async def __aenter__(self) -> BaseLLMProvider:
        """Enter the provider context (connect, initialise session, etc.)."""

    @abstractmethod
    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit the provider context (disconnect, cleanup, etc.)."""


# ---------------------------------------------------------------------------
# Factory (imported after BaseLLMProvider to avoid circular imports)
# ---------------------------------------------------------------------------

from .factory import (  # noqa: E402
    get_provider,
    get_qa_llm_provider,
    list_provider_aliases,
    list_providers,
)

# ---------------------------------------------------------------------------
# Re-export public symbols
# ---------------------------------------------------------------------------

__all__ = [
    # Abstract base
    "BaseLLMProvider",
    # Message protocol types
    "AssistantMessage",
    "TextBlock",
    "ToolUseBlock",
    "ToolResultBlock",
    "UserMessage",
    # Factory
    "get_provider",
    "get_qa_llm_provider",
    "list_providers",
    "list_provider_aliases",
]
