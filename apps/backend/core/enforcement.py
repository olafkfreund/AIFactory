"""Claude enforcement wrapper (Epic #35 v1.2 / #207).

Closes the per-tenant budget / allowlist / audit gap left open in v1.1
for the native Claude Agent SDK path.  Option A (in-process wrapper)
was chosen over Option B (LiteLLM Anthropic passthrough) due to four
open upstream LiteLLM bugs (#28562, #28228, #26749, #27512) that corrupt
audit-row integrity on AIFactory's exact code path.

Design doc: docs/plans/2026-05-29-claude-litellm-wrapper-design.md

Public API
----------

- ClaudeEnforcementContext  — per-call enforcement state (allowlist + budget).
- BudgetProvider            — protocol for budget reads (injectable in tests).
- LiteLLMBudgetProvider     — concrete production impl via LiteLLMAdminClient.
- ClaudeUsageSnapshot       — token counts + cost estimate from SDK result.
- BudgetExceededError       — raised pre-call when tenant is over budget.
- BudgetCheckUnavailableError — raised pre-call when budget read fails AND
                                failure_mode='closed'.
- _EnforcedClaudeSDKClient  — private adapter returned by create_client()
                               when org_id is supplied.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from providers.openai_compatible import ModelNotAllowedError, _model_allowed

logger = logging.getLogger(__name__)

# Warn-once set: tracks org_ids for which we've already emitted the
# "no virtual key configured" warning.  Avoids log spam when many
# calls arrive for the same org in budget-absent deployments.
_NO_KEY_WARNED: set[str] = set()

# ---------------------------------------------------------------------------
# Pricing table  (decision §6)
# Per-million-token rates in USD, as of 2026-05-29 Anthropic pricing page.
# Four-way breakdown: input, output, cache_read, cache_creation.
# Requires a manual bump when Anthropic publishes new models or rate changes.
# The test_claude_enforcement/test_wrapper_model_allowlist.py asserts that
# every model used in phase_config.py has an entry here.
# ---------------------------------------------------------------------------

_CLAUDE_PRICING: dict[str, dict[str, float]] = {
    # Opus 4.8 — current flagship. NOTE: pricing assumed identical to 4.7's
    # flagship tier; verify against the official price list and adjust if it
    # differs. (Cost reporting only — does not affect build correctness.)
    "claude-opus-4-8": {
        "input": 15.0,
        "output": 75.0,
        "cache_read": 1.50,  # 0.1x input rate
        "cache_creation": 18.75,  # 1.25x input rate
    },
    # Opus 4.7 — previous flagship; adaptive thinking.
    "claude-opus-4-7": {
        "input": 15.0,
        "output": 75.0,
        "cache_read": 1.50,  # 0.1x input rate
        "cache_creation": 18.75,  # 1.25x input rate
    },
    # Opus 4.6 — legacy alias; 1M context.
    "claude-opus-4-6": {
        "input": 15.0,
        "output": 75.0,
        "cache_read": 1.50,
        "cache_creation": 18.75,
    },
    # Opus 4.5 — pinned version used in some phase configs.
    "claude-opus-4-5-20251101": {
        "input": 15.0,
        "output": 75.0,
        "cache_read": 1.50,
        "cache_creation": 18.75,
    },
    # Sonnet 4.6 — default model for coding + QA phases.
    "claude-sonnet-4-6": {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.30,
        "cache_creation": 3.75,
    },
    # Sonnet 4.5 — prior model; kept for operators who pinned it.
    "claude-sonnet-4-5-20250929": {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.30,
        "cache_creation": 3.75,
    },
    # Haiku 4.5 — utility model for fast/cheap single-turn ops.
    "claude-haiku-4-5-20251001": {
        "input": 0.80,
        "output": 4.0,
        "cache_read": 0.08,
        "cache_creation": 1.0,
    },
}


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


class BudgetExceededError(RuntimeError):
    """Tenant has exhausted their daily LLM budget.

    Raised BEFORE the SDK subprocess spawns so no tokens are consumed.
    Carries org_id + remaining_usd for operator log-line clarity.
    """

    def __init__(self, org_id: str, remaining_usd: float) -> None:
        super().__init__(
            f"Claude call blocked: org={org_id!r} has ${remaining_usd:.4f} "
            f"remaining (budget exhausted). Refills on the next daily window. "
            f"Adjust organizations.allowed_models or LiteLLM virtual-key "
            f"max_budget to extend the limit."
        )
        self.org_id = org_id
        self.remaining_usd = remaining_usd


class BudgetCheckUnavailableError(RuntimeError):
    """Budget pre-check could not complete AND failure_mode='closed'.

    Raised when the LiteLLM admin API is unreachable AND the operator
    has set AIFACTORY_CLAUDE_ENFORCEMENT_FAILURE_MODE=closed.  In
    failure_mode='open' (default), the wrapper logs WARNING + proceeds.
    """

    def __init__(self, org_id: str, cause: str) -> None:
        super().__init__(
            f"Claude call blocked: budget pre-check unavailable for "
            f"org={org_id!r} (failure_mode=closed). Cause: {cause}. "
            f"Set AIFACTORY_CLAUDE_ENFORCEMENT_FAILURE_MODE=open to allow "
            f"calls when the budget service is unreachable (trade-off: "
            f"potential over-spend in the unreachable window)."
        )
        self.org_id = org_id
        self.cause = cause


# ---------------------------------------------------------------------------
# ClaudeUsageSnapshot
# ---------------------------------------------------------------------------


@dataclass
class ClaudeUsageSnapshot:
    """Token counts extracted from the SDK result; cost estimated in-process.

    The SDK's ResultMessage carries a ``usage`` field with input_tokens,
    output_tokens, and (on cache hits) cache_read_tokens /
    cache_creation_tokens.  We capture these at stream-end and compute
    the cost from _CLAUDE_PRICING.

    Partial usage is captured on CancelledError (streaming-abandoned case)
    — input_tokens may be set while output_tokens is still 0.
    """

    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None

    def estimated_cost_usd(self) -> float | None:
        """Estimate cost from the in-process pricing table.

        Returns None when the model is unknown or no token counts are
        available.  The audit row's cost_source='litellm_estimate' field
        signals that this is approximate (same disclosure as non-Claude
        path per design §6).
        """
        rate = _CLAUDE_PRICING.get(self.model)
        if rate is None or self.input_tokens is None:
            return None
        return (
            self.input_tokens * rate["input"]
            + (self.output_tokens or 0) * rate["output"]
            + (self.cache_read_tokens or 0) * rate["cache_read"]
            + (self.cache_creation_tokens or 0) * rate["cache_creation"]
        ) / 1_000_000  # rates are per-million-tokens


# ---------------------------------------------------------------------------
# BudgetProvider protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class BudgetProvider(Protocol):
    """Reads remaining-budget for an org from the authoritative store.

    Production: wraps LiteLLMAdminClient.get_virtual_key_info().
    Tests: injected fake returning a fixed remaining_usd float.

    None return means "no budget configured" (unrestricted — org has
    not been given a daily cap).
    """

    async def remaining_usd(self, org_id: str) -> float | None: ...


class LiteLLMBudgetProvider:
    """Concrete budget provider backed by LiteLLM's admin API.

    Reads the virtual key's spend + max_budget to compute remaining
    headroom.  When LiteLLM is not deployed (litellm.enabled=false),
    construction raises ValueError at the LiteLLMAdminClient level;
    the factory catches this and passes a _NoBudgetProvider instead.
    """

    def __init__(self, admin_client: Any) -> None:
        # admin_client is LiteLLMAdminClient; avoid circular import
        # by accepting Any here and letting mypy validate at call sites.
        self._client = admin_client

    async def remaining_usd(self, org_id: str) -> float | None:
        """Compute remaining USD from LiteLLM key info.

        Returns None when the org has no virtual key (treated as
        'no budget configured = unlimited').  Raises
        LiteLLMAdminUnavailableError on network failure so the
        caller can apply failure_mode logic.
        """
        info = await self._client.get_virtual_key_info(org_id)
        if info is None:
            # No virtual key — budget enforcement not yet configured.
            # Warn once per org so operators know they're running without
            # budget enforcement but still with allowlist + audit.
            if org_id not in _NO_KEY_WARNED:
                _NO_KEY_WARNED.add(org_id)
                logger.warning(
                    "Claude budget enforcement skipped for org=%r — no "
                    "LiteLLM virtual key configured.  Per-call audit + "
                    "allowlist remain active.  Enable the LiteLLM gateway "
                    "and run the tenant reconciler for per-tenant budget "
                    "enforcement.",
                    org_id,
                )
            return None  # None = unlimited
        max_budget = info.get("max_budget")
        spend = info.get("spend", 0.0) or 0.0
        if max_budget is None or max_budget <= 0:
            return None  # 0 or None = unlimited (same as wildcard allowlist)
        return float(max_budget) - float(spend)


class _NoBudgetProvider:
    """Fallback when LiteLLM is not deployed.

    Always returns None (unlimited).  Audit + allowlist still run.
    """

    async def remaining_usd(self, org_id: str) -> float | None:
        return None


# ---------------------------------------------------------------------------
# ClaudeEnforcementContext
# ---------------------------------------------------------------------------


class ClaudeEnforcementContext:
    """Per-call enforcement state: allowlist + budget + audit wiring.

    Constructed by create_client() when org_id is supplied.  The noop()
    class-method returns a bypass instance for system tasks (CLI runs,
    spec-runner, runners that pass no org_id) — matching the
    OpenAICompatibleProvider(allowed_models=None) semantics.

    enforce_pre_call() is synchronous-ish for the allowlist check but
    wraps the budget check in asyncio.  Both checks complete before the
    SDK subprocess spawns — fail-fast at the enforcement boundary.
    """

    def __init__(
        self,
        org_id: str,
        user_id: str | None,
        model: str,
        allowed_models: list[str],
        failure_mode: str = "open",
        budget_provider: BudgetProvider | None = None,
    ) -> None:
        self._org_id = org_id
        self._user_id = user_id
        self._model = model
        self._allowed_models = allowed_models
        # failure_mode governs what happens when the budget service is DOWN:
        #   'open'   — log WARNING + proceed (default; matches non-Claude path).
        #   'closed' — raise BudgetCheckUnavailableError (strict deployments).
        self._failure_mode = failure_mode
        self._budget_provider = budget_provider or _NoBudgetProvider()
        self._is_noop = False

    @classmethod
    def noop(cls) -> ClaudeEnforcementContext:
        """Bypass all enforcement (system tasks, CLI mode, unspecified org).

        Callers that need the noop context should prefer passing no org_id
        to create_client() — this class-method is for internal use by the
        adapter to avoid if-None branches in hot paths.
        """
        ctx = cls.__new__(cls)
        ctx._org_id = ""
        ctx._user_id = None
        ctx._model = ""
        ctx._allowed_models = ["*"]
        ctx._failure_mode = "open"
        ctx._budget_provider = _NoBudgetProvider()
        ctx._is_noop = True
        return ctx

    def enforce_allowlist(self) -> None:
        """Raise ModelNotAllowedError when model is not in the allowlist.

        Fast-fail — runs synchronously before any async work so the
        caller sees the rejection immediately.  Mirrors
        OpenAICompatibleProvider.__init__ behaviour (allowlist at
        construction time, not call time).
        """
        if self._is_noop:
            return
        if not _model_allowed(self._model, self._allowed_models):
            raise ModelNotAllowedError(
                model=self._model,
                allowed_models=self._allowed_models,
                org_id=self._org_id,
            )

    async def enforce_budget(self) -> None:
        """Raise BudgetExceededError / BudgetCheckUnavailableError pre-call.

        WHY async: budget reads hit the LiteLLM admin HTTP API.  The
        enforcement context is built synchronously (allowlist check) and
        this method is awaited in the async path of the adapter.

        Race-window acknowledgement (design §5): two replicas can each
        read "$3 remaining" and both proceed.  The over-spend cap is ONE
        call per replica per budget window — bounded, self-correcting.
        """
        if self._is_noop:
            return
        try:
            remaining = await self._budget_provider.remaining_usd(self._org_id)
        except Exception as exc:
            # WHY catch-all: LiteLLMAdminUnavailableError + LiteLLMAdminError
            # plus any unexpected exception from the budget provider.
            if self._failure_mode == "closed":
                raise BudgetCheckUnavailableError(
                    org_id=self._org_id,
                    cause=str(exc),
                ) from exc
            logger.warning(
                "Claude enforcement: budget pre-check failed for org=%r "
                "(failure_mode=open — proceeding). Cause: %s",
                self._org_id,
                exc,
            )
            return
        if remaining is not None and remaining <= 0:
            raise BudgetExceededError(self._org_id, remaining)

    async def record_post_call(
        self,
        *,
        usage: ClaudeUsageSnapshot,
        prompt_text: str,
        response_text: str,
        action: str,
        latency_ms: int | None = None,
        error: str | None = None,
    ) -> None:
        """Write the audit row post-call.  Failure-safe.

        PII redaction applies inside write_llm_call_audit() (imported
        from the web-server's services module).  We pass the raw text;
        the hook redacts + truncates before storage (design §7).

        The audit hook's own try/except swallows write errors so this
        method never raises (design §8 clause 1).  If the import fails
        (e.g., PYTHONPATH doesn't include web-server) we log WARNING
        and return — matching the non-Claude path's graceful degradation.
        """
        if self._is_noop:
            return
        try:
            from server.services.llm_audit_hook import write_llm_call_audit
        except ImportError:
            logger.warning(
                "Claude enforcement: cannot import write_llm_call_audit "
                "(PYTHONPATH missing web-server?). Audit row NOT written "
                "for org=%r model=%r action=%r.",
                self._org_id,
                usage.model,
                action,
            )
            return

        try:
            await write_llm_call_audit(
                org_id=self._org_id,
                user_id=self._user_id,
                model=usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=usage.estimated_cost_usd(),
                latency_ms=latency_ms,
                prompt_text=prompt_text,
                response_text=response_text,
                litellm_request_id=None,  # no LiteLLM request ID on the Claude path
                action=action,
                error=error,
                # provider='claude_sdk' distinguishes Claude SDK from OpenAI-compat
                # path in chargeback queries (design §4 + recommendation #2).
                # cost_source is set by write_llm_call_audit from its _COST_SOURCE
                # constant; no need to duplicate it here.
                extra_details={"provider": "claude_sdk"},
            )
        except Exception:
            # WHY: audit-write failure MUST NOT propagate (design §8 clause 1).
            # The underlying Claude call already succeeded; failing the task
            # post-hoc would corrupt the user's session for no security gain.
            # Matches the failure-safe envelope from the non-Claude path (#43).
            logger.warning(
                "Claude enforcement: audit write failed for org=%r "
                "model=%r action=%r; the Claude call already completed.",
                self._org_id,
                usage.model,
                action,
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# _EnforcedClaudeSDKClient adapter
# ---------------------------------------------------------------------------
# WHY composition not subclass: ClaudeSDKClient is effectively final
# (extending it ties us to internal layout).  A thin adapter survives
# SDK minor-version bumps and makes the v1.3 Option-B swap a single-
# file change (design §1).


class _EnforcedClaudeSDKClient:
    """Transparent adapter around ClaudeSDKClient with enforcement.

    Intercepts query / receive_response / context-manager lifecycle to:
    1. Check the allowlist + budget BEFORE spawning the subprocess.
    2. Capture token usage from the stream.
    3. Write the audit row in __aexit__ (success, abandoned, or failed).

    Callers interact with this identically to ClaudeSDKClient; the
    enforcement is invisible on the happy path.
    """

    def __init__(
        self,
        underlying: Any,
        enforcement: ClaudeEnforcementContext,
    ) -> None:
        self._underlying = underlying
        self._enforcement = enforcement
        # Accumulated state for the audit row.
        self._prompt_text: str = ""
        self._response_text: str = ""
        self._usage = ClaudeUsageSnapshot(model=enforcement._model)
        self._start_time: float | None = None
        self._last_error: str | None = None

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> _EnforcedClaudeSDKClient:
        # Run allowlist check synchronously (fast-fail before any IO).
        self._enforcement.enforce_allowlist()
        # Budget check is async (HTTP to LiteLLM admin API).
        await self._enforcement.enforce_budget()
        await self._underlying.__aenter__()
        self._start_time = time.monotonic()
        return self

    async def __aexit__(
        self,
        exc_type: type | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool | None:
        """Write audit row on success, abandonment, or failure."""
        latency_ms: int | None = None
        if self._start_time is not None:
            latency_ms = int((time.monotonic() - self._start_time) * 1000)

        try:
            if exc_type is asyncio.CancelledError:
                action = "llm.call.abandoned"
            elif exc_type is not None:
                action = "llm.call.failed"
                self._last_error = str(exc_val) if exc_val else "unknown error"
            else:
                action = "llm.call"

            await self._enforcement.record_post_call(
                usage=self._usage,
                prompt_text=self._prompt_text,
                response_text=self._response_text,
                action=action,
                latency_ms=latency_ms,
                error=self._last_error,
            )
        except Exception:
            # WHY: a bug in record_post_call (or in the adapter itself)
            # MUST NOT crash the agent pod.  We catch every exception,
            # log it at ERROR level, and let the underlying call's
            # result/error propagate unmodified.
            logger.error(
                "Claude enforcement: unexpected error in __aexit__ for "
                "org=%r; the underlying Claude call is unaffected.",
                self._enforcement._org_id,
                exc_info=True,
            )

        return await self._underlying.__aexit__(exc_type, exc_val, exc_tb)

    # ------------------------------------------------------------------
    # query / receive_response passthrough with usage capture
    # ------------------------------------------------------------------

    async def query(self, prompt: str, **kwargs: Any) -> Any:
        """Intercept query to capture the prompt text."""
        self._prompt_text = prompt
        return await self._underlying.query(prompt, **kwargs)

    def receive_response(self) -> Any:
        """Intercept the response generator to accumulate usage tokens."""
        return self._WrappedResponseStream(
            self._underlying.receive_response(),
            self._usage,
            self,
        )

    # ------------------------------------------------------------------
    # Transparent attribute access for everything else
    # ------------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        return getattr(self._underlying, name)

    # ------------------------------------------------------------------
    # Inner class: async generator wrapper
    # ------------------------------------------------------------------

    class _WrappedResponseStream:
        """Wraps the SDK's async generator to capture usage + response text.

        The Claude SDK yields message objects; the final ResultMessage
        carries a ``usage`` attribute.  We extract it without modifying
        the message flow seen by the caller.
        """

        def __init__(
            self,
            inner: Any,
            usage: ClaudeUsageSnapshot,
            parent: _EnforcedClaudeSDKClient,
        ) -> None:
            self._inner = inner
            self._usage = usage
            self._parent = parent

        def __aiter__(self) -> _EnforcedClaudeSDKClient._WrappedResponseStream:
            return self

        async def __anext__(self) -> Any:
            msg = await self._inner.__anext__()
            self._capture(msg)
            return msg

        def _capture(self, msg: Any) -> None:
            """Extract tokens + response text from SDK message types."""
            try:
                # ResultMessage carries the final usage block.
                msg_type = getattr(msg, "type", None) or type(msg).__name__
                if hasattr(msg, "usage") and msg.usage is not None:
                    u = msg.usage
                    self._usage.input_tokens = getattr(u, "input_tokens", None)
                    self._usage.output_tokens = getattr(u, "output_tokens", None)
                    self._usage.cache_read_tokens = getattr(
                        u,
                        "cache_read_tokens",
                        None,
                    ) or getattr(u, "cache_read_input_tokens", None)
                    self._usage.cache_creation_tokens = getattr(
                        u,
                        "cache_creation_tokens",
                        None,
                    ) or getattr(u, "cache_write_input_tokens", None)

                # Collect assistant text blocks for the audit row.
                if hasattr(msg, "content"):
                    for block in msg.content or []:
                        if hasattr(block, "text") and isinstance(block.text, str):
                            self._parent._response_text += block.text
            except Exception:
                # WHY: capture failures must never crash the stream.
                logger.debug(
                    "Claude enforcement: usage capture failed on message "
                    "type=%r (ignoring)",
                    msg_type,
                    exc_info=True,
                )


# ---------------------------------------------------------------------------
# Factory helper — called by create_client() and create_simple_client()
# ---------------------------------------------------------------------------


def build_enforcement_context(
    org_id: str | None,
    user_id: str | None,
    model: str,
    allowed_models: list[str],
) -> ClaudeEnforcementContext:
    """Construct the right enforcement context for a create_client() call.

    When org_id is None or AIFACTORY_CLAUDE_ENFORCEMENT_ENABLED is not
    'true', returns a noop context (backward compat, system tasks).
    """
    enforcement_enabled = (
        os.environ.get("AIFACTORY_CLAUDE_ENFORCEMENT_ENABLED", "").lower() == "true"
    )
    if not enforcement_enabled or org_id is None:
        return ClaudeEnforcementContext.noop()

    failure_mode = os.environ.get(
        "AIFACTORY_CLAUDE_ENFORCEMENT_FAILURE_MODE",
        "open",
    ).lower()
    if failure_mode not in ("open", "closed"):
        logger.warning(
            "AIFACTORY_CLAUDE_ENFORCEMENT_FAILURE_MODE=%r is invalid; "
            'defaulting to "open".',
            failure_mode,
        )
        failure_mode = "open"

    # Attempt to build a LiteLLM budget provider; fall back gracefully
    # when LiteLLM is not deployed (litellm.enabled=false).
    budget_provider: BudgetProvider = _NoBudgetProvider()
    litellm_url = os.environ.get("LITELLM_GATEWAY_URL", "").strip()
    if litellm_url:
        try:
            from server.services.litellm_admin_client import LiteLLMAdminClient

            admin_client = LiteLLMAdminClient(base_url=litellm_url)
            budget_provider = LiteLLMBudgetProvider(admin_client)
        except Exception as exc:
            logger.warning(
                "Claude enforcement: cannot init LiteLLMBudgetProvider "
                "(%s); budget enforcement disabled for this session. "
                "Allowlist + audit remain active.",
                exc,
            )

    return ClaudeEnforcementContext(
        org_id=org_id,
        user_id=user_id,
        model=model,
        allowed_models=allowed_models,
        failure_mode=failure_mode,
        budget_provider=budget_provider,
    )


def wrap_client_if_enforced(
    client: Any,
    enforcement: ClaudeEnforcementContext,
) -> Any:
    """Return _EnforcedClaudeSDKClient if enforcement is active, else client.

    Keeps create_client() clean: one call to wrap + one return.
    """
    if enforcement._is_noop:
        return client
    return _EnforcedClaudeSDKClient(underlying=client, enforcement=enforcement)


__all__ = [
    "_CLAUDE_PRICING",
    "BudgetCheckUnavailableError",
    "BudgetExceededError",
    "BudgetProvider",
    "ClaudeEnforcementContext",
    "ClaudeUsageSnapshot",
    "LiteLLMBudgetProvider",
    "_EnforcedClaudeSDKClient",
    "build_enforcement_context",
    "wrap_client_if_enforced",
]
