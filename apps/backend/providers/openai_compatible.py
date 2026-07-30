"""
OpenAICompatibleProvider — Adapter for any OpenAI-compatible chat endpoint
=========================================================================

Implements ``BaseLLMProvider`` by calling the standard OpenAI
``POST /v1/chat/completions`` endpoint exposed by services such as:

* OpenAI (api.openai.com)
* LM Studio
* vLLM
* OpenRouter
* Together AI
* Groq
* LocalAI
* Anyscale Endpoints
* Ollama's OpenAI-compatible shim
* Any FastAPI/Flask service that speaks the OpenAI protocol

Uses Python's standard-library ``urllib`` — no extra dependencies.

The adapter sends the prompt as a single user message, waits for the
(non-streaming) response, and wraps the plain-text reply in the
message-protocol types expected by the rest of the codebase.

Usage::

    from providers.openai_compatible import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        model="gpt-4o-mini",
        base_url="https://api.openai.com",
        api_key="sk-...",
    )
    async with provider:
        await provider.query(prompt)
        async for msg in provider.receive_response():
            ...

API call shape::

    POST <base_url>/v1/chat/completions
    Authorization: Bearer <api_key>     (optional)
    Content-Type: application/json

    {
        "model": "<model>",
        "messages": [{"role": "user", "content": "<prompt>"}],
        "stream": false,
        "temperature": 0,
        "max_tokens": 4096
    }

Response::

    {
        "choices": [
            {"message": {"role": "assistant", "content": "..."}}
        ]
    }
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

from providers import BaseLLMProvider, _env_scrub_outbound_default
from providers._openai_headers import OpenAICompatibleHeadersMixin
from providers.types import AssistantMessage, TextBlock

logger = logging.getLogger(__name__)


class ModelNotAllowedError(RuntimeError):
    """The requested model is not in the org's allowlist (Epic #35 #38 PR-2b).

    Raised at provider construction (fail-fast — easier to debug than
    a 400 from LiteLLM mid-call). Carries the org-level allowlist +
    the rejected model name so the operator log line is self-contained.

    A wildcard ``["*"]`` allowlist (the schema default from PR-2a)
    bypasses enforcement. ``None`` (caller passed no allowlist)
    also bypasses — the enforcement plane is opt-in per provider call.
    """

    def __init__(
        self,
        model: str,
        allowed_models: list[str],
        org_id: str | None = None,
    ) -> None:
        org_label = f"org={org_id}" if org_id else "org (unspecified)"
        super().__init__(
            f"Model {model!r} is not in the allowlist for {org_label}. "
            f"Permitted patterns: {allowed_models!r}. "
            f"Update organizations.allowed_models to grant access "
            f"(takes effect on next reconcile)."
        )
        self.model = model
        self.allowed_models = allowed_models
        self.org_id = org_id


def _model_allowed(model: str, allowed_models: list[str] | None) -> bool:
    """True when ``model`` matches any pattern in ``allowed_models``.

    - ``None`` (no allowlist supplied) → True (caller opted out).
    - ``["*"]`` → True (schema default from PR-2a; backward compat).
    - Otherwise: fnmatch.fnmatchcase per pattern. ``"claude-*"`` matches
      ``"claude-opus-4-7"``; ``"gpt-4o"`` exact-matches that only.
    """
    if allowed_models is None:
        return True
    if "*" in allowed_models:
        return True
    return any(fnmatch.fnmatchcase(model, pattern) for pattern in allowed_models)


# ---------------------------------------------------------------------------
# Module-level defaults
# ---------------------------------------------------------------------------

_DEFAULT_BASE_URL: str = "https://api.openai.com"
_DEFAULT_MODEL: str = "gpt-4o-mini"
_DEFAULT_TIMEOUT: int = 300  # seconds

_PATH_CHAT: str = "/v1/chat/completions"
_PATH_MODELS: str = "/v1/models"


class OpenAICompatibleProvider(OpenAICompatibleHeadersMixin, BaseLLMProvider):  # type: ignore[misc]  # providers.* re-exports are untyped (Any base); mixin is real at runtime
    """LLM provider backed by any OpenAI-compatible ``/v1/chat/completions`` endpoint.

    The adapter sends the prompt as a single ``user`` message and yields the
    assistant's reply as a single ``AssistantMessage`` containing one
    ``TextBlock``.

    Args:
        model: Model identifier for the target server (e.g. ``"gpt-4o-mini"``,
            ``"llama-3.3-70b-instruct"``, ``"mistral-large"``).
        base_url: Base URL of the server (no trailing ``/v1``).  The provider
            appends ``/v1/chat/completions`` itself.
        api_key: Optional bearer token.  Skip or pass an empty string when
            targeting local servers (LM Studio, vLLM) that don't require auth.
        timeout: Maximum seconds to wait for the HTTP response.  Defaults
            to 300 (5 minutes).
        extra_headers: Optional dict of additional headers to send with each
            request (e.g. ``{"HTTP-Referer": "..."}`` for OpenRouter).
        extra_options: Optional dict merged into the JSON body
            (e.g. ``{"temperature": 0.2, "max_tokens": 8192,
            "top_p": 1, "frequency_penalty": 0}``).
    """

    def __init__(
        self,
        model: str = _DEFAULT_MODEL,
        base_url: str = _DEFAULT_BASE_URL,
        api_key: str | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
        extra_headers: dict[str, str] | None = None,
        extra_options: dict[str, Any] | None = None,
        # Epic #35 #38 PR-2b — per-org enforcement plane.
        allowed_models: list[str] | None = None,
        org_id: str | None = None,
        user_id: str | None = None,
        # v1.2 #210 — opt-in pre-send PII scrub. None defers to env var
        # (LITELLM_AUDIT_SCRUB_OUTBOUND) so a single deployment-wide
        # toggle applies; explicit True/False overrides env (lets tests
        # pin behaviour without poking environ).
        scrub_outbound: bool | None = None,
    ) -> None:
        # Fail-fast allowlist check: raise BEFORE any HTTP setup so
        # the operator sees a clear ModelNotAllowedError, not a 400
        # bouncing off LiteLLM mid-call.
        if not _model_allowed(model, allowed_models):
            raise ModelNotAllowedError(
                model=model,
                allowed_models=allowed_models or [],
                org_id=org_id,
            )

        self._model = model
        # Epic #35 #38 PR-1 — when LITELLM_GATEWAY_URL is set, all
        # provider HTTP calls route through the gateway for per-tenant
        # budget / rate-limit / allowlist / audit. Zero behavior change
        # when the env var is unset (operator hasn't opted in via Helm).
        from ._gateway import resolve_base_url

        self._base_url = resolve_base_url(base_url).rstrip("/")
        self._api_key = api_key or None  # treat empty string as None
        self._timeout = timeout
        self._extra_headers: dict[str, str] = extra_headers or {}
        self._extra_options: dict[str, Any] = extra_options or {}
        self._pending_prompt: str | None = None

        # Audit-hook context (Epic #35 #38 PR-2b). Stored for the
        # success / cancellation / failure paths in _run_request().
        self._allowed_models = allowed_models
        self._org_id = org_id
        self._user_id = user_id

        # v1.2 #210 — resolve scrubBeforeSend mode once. Explicit ctor
        # arg wins; otherwise inherit the deployment-wide env default.
        self._scrub_outbound: bool = (
            scrub_outbound
            if scrub_outbound is not None
            else _env_scrub_outbound_default()
        )

        logger.debug(
            "OpenAICompatibleProvider created",
            extra={
                "model": model,
                "base_url": self._base_url,
                "timeout": timeout,
                "has_api_key": bool(self._api_key),
                "org_id": org_id,
            },
        )

    # ------------------------------------------------------------------
    # BaseLLMProvider interface
    # ------------------------------------------------------------------

    async def query(self, prompt: str) -> None:
        """Store the prompt for execution when ``receive_response()`` is called."""
        self._pending_prompt = prompt
        logger.debug("OpenAICompatibleProvider: prompt stored (length=%d)", len(prompt))

    def receive_response(self) -> AsyncIterator[Any]:
        """Return an async generator that calls ``/v1/chat/completions``."""
        return self._run_request()

    async def _run_request(self) -> AsyncGenerator[Any, None]:
        """Async generator: call the chat endpoint and yield the assistant message.

        Yields:
            ``AssistantMessage(content=[TextBlock(text=<response>)])``

        Audit (Epic #35 #38 PR-2b): every call produces exactly one
        audit row via ``llm_audit_hook.write_llm_call_audit``:
        - success → ``action="llm.call"`` with token counts + cost.
        - cancellation (``asyncio.CancelledError``) → ``action="llm.call.abandoned"``.
        - HTTP / runtime failure → ``action="llm.call.failed"`` with the
          error message.
        The hook is itself failure-safe so a broken audit pipeline
        cannot fail the user's task.
        """
        if not self._pending_prompt:
            logger.warning(
                "OpenAICompatibleProvider.receive_response() called before "
                "query() — no prompt to send"
            )
            return

        # v1.2 #210 scrubBeforeSend: the OUTBOUND prompt was already run
        # through the redactor by ``BaseLLMProvider.query`` (#1128 lifted
        # the scrub there so every adapter gets it, not just this one), so
        # ``_pending_prompt`` is what actually goes on the wire. The raw
        # prompt is still captured for the audit row so operators can see
        # what the user typed AND what we actually sent. The
        # `prompt_outbound_scrubbed` flag is True only when redaction
        # actually changed the text — operators query
        # ``details_json->>'prompt_outbound_scrubbed' = true`` for
        # "show me every call where PII was scrubbed before send".
        prompt_text = (
            getattr(self, "_outbound_prompt_raw", None) or self._pending_prompt
        )
        prompt_outbound_scrubbed = bool(
            getattr(self, "_outbound_prompt_scrubbed", False)
        )

        payload = self._build_payload(self._pending_prompt)
        url = f"{self._base_url}{_PATH_CHAT}"

        logger.debug("OpenAICompatibleProvider: POST %s model=%r", url, self._model)

        # Track latency from request issue → response complete; this is
        # what an external observer (the agent) would measure.
        started = time.monotonic()
        response_text = ""
        response_data: dict[str, Any] = {}
        audit_action = "llm.call"
        audit_error: str | None = None

        try:
            try:
                response_data = await asyncio.wait_for(
                    asyncio.to_thread(self._http_post, url, payload),
                    timeout=float(self._timeout),
                )
            except asyncio.TimeoutError:
                audit_action = "llm.call.failed"
                audit_error = f"timeout after {self._timeout}s"
                raise asyncio.TimeoutError(
                    f"OpenAI-compatible API request timed out after {self._timeout}s. "
                    "Increase timeout= or reduce prompt size."
                )
            except RuntimeError as exc:
                # _http_post raises RuntimeError on HTTP / URL / JSON errors.
                audit_action = "llm.call.failed"
                audit_error = str(exc)
                raise

            response_text = self._extract_content(response_data)

            logger.debug(
                "OpenAICompatibleProvider: response received content_len=%d",
                len(response_text),
            )

            yield AssistantMessage(content=[TextBlock(text=response_text)])
        except asyncio.CancelledError:
            # Client disconnected / task cancelled mid-stream.
            # Per design §5 audit variant #2.
            audit_action = "llm.call.abandoned"
            audit_error = "asyncio.CancelledError"
            raise
        finally:
            latency_ms = int((time.monotonic() - started) * 1000)
            # Hook is itself failure-safe; we still wrap to defend
            # against the import failing in odd test contexts.
            try:
                await self._write_audit(
                    action=audit_action,
                    prompt_text=prompt_text,
                    response_text=response_text,
                    response_data=response_data,
                    latency_ms=latency_ms,
                    error=audit_error,
                    prompt_outbound_scrubbed=prompt_outbound_scrubbed,
                )
            except Exception:
                logger.warning(
                    "OpenAICompatibleProvider: audit hook raised "
                    "unexpectedly; swallowed",
                    exc_info=True,
                )

    async def _write_audit(
        self,
        *,
        action: str,
        prompt_text: str,
        response_text: str,
        response_data: dict[str, Any],
        latency_ms: int,
        error: str | None,
        prompt_outbound_scrubbed: bool = False,
    ) -> None:
        """Best-effort call into the web-server's LLM audit writer.

        The audit writer lives in the web-server package and uses the
        web-server's SQLAlchemy engine. In CLI / agent contexts where
        the web-server isn't importable, the ImportError is caught
        and logged at DEBUG — the audit is the web-server's
        responsibility for HTTP-mode calls, and CLI-mode operators
        get the existing local logging.
        """
        try:
            from server.services.llm_audit_hook import write_llm_call_audit
        except ImportError:
            logger.debug(
                "OpenAICompatibleProvider: web-server audit hook not "
                "importable (CLI mode?); skipping audit row",
            )
            return

        usage = (
            response_data.get("usage") or {}
            if isinstance(
                response_data,
                dict,
            )
            else {}
        )
        input_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        output_tokens = (
            usage.get("completion_tokens") if isinstance(usage, dict) else None
        )

        # LiteLLM adds these to the response when the gateway is in
        # the path. Both fields are optional — present on success
        # paths through the gateway, absent for direct-provider
        # mode or abandoned/failed paths.
        cost_usd = None
        litellm_request_id = None
        if isinstance(response_data, dict):
            cost_usd = response_data.get("response_cost") or response_data.get(
                "_response_cost",
            )
            # LiteLLM exposes its internal request id either at the
            # top level or under a `_litellm` envelope.
            litellm_request_id = response_data.get("id") or response_data.get(
                "litellm_request_id"
            )

        await write_llm_call_audit(
            org_id=self._org_id,
            user_id=self._user_id,
            model=self._model,
            input_tokens=int(input_tokens) if input_tokens is not None else None,
            output_tokens=int(output_tokens) if output_tokens is not None else None,
            cost_usd=float(cost_usd) if cost_usd is not None else None,
            latency_ms=latency_ms,
            prompt_text=prompt_text,
            response_text=response_text,
            litellm_request_id=str(litellm_request_id) if litellm_request_id else None,
            action=action,
            error=error,
            prompt_outbound_scrubbed=prompt_outbound_scrubbed,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_payload(self, prompt: str) -> dict[str, Any]:
        """Construct the JSON request body for ``/v1/chat/completions``."""
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        # Sensible default — most callers want deterministic output for QA-style use
        body.setdefault("temperature", 0)
        body.update(self._extra_options)
        return body

    def _http_post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Synchronous HTTP POST — run inside ``asyncio.to_thread()``.

        Raises:
            RuntimeError: On HTTP errors or JSON decode failures.
        """
        body_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body_bytes,
            headers=self._build_headers(),
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            error_body = ""
            try:
                error_body = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            raise RuntimeError(
                f"OpenAI-compatible API HTTP error {exc.code}: {exc.reason}. "
                f"Response body: {error_body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Cannot reach OpenAI-compatible server at '{self._base_url}': "
                f"{exc.reason}. Verify base_url and that the server is running."
            ) from exc

        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"OpenAI-compatible API returned invalid JSON: {exc}"
            ) from exc

    @staticmethod
    def _extract_content(response_data: dict[str, Any]) -> str:
        """Extract the assistant reply text from an OpenAI-shape response.

        Expected response shape::

            {
                "choices": [
                    {"message": {"role": "assistant", "content": "..."}}
                ]
            }

        Raises:
            RuntimeError: If the expected keys are absent.
        """
        choices = response_data.get("choices")
        if not isinstance(choices, list) or not choices:
            # Some servers wrap errors in {"error": {...}} — surface that.
            err = response_data.get("error")
            if err:
                raise RuntimeError(f"OpenAI-compatible API returned error: {err}")
            raise RuntimeError(
                f"OpenAI-compatible API response missing 'choices'. "
                f"Got keys: {list(response_data.keys())}"
            )

        first = choices[0]
        if not isinstance(first, dict):
            raise RuntimeError("OpenAI-compatible API: 'choices[0]' is not an object")

        message = first.get("message")
        if not isinstance(message, dict):
            raise RuntimeError(
                "OpenAI-compatible API response missing 'choices[0].message'"
            )

        content = message.get("content")
        if content is None:
            raise RuntimeError(
                "OpenAI-compatible API response missing 'choices[0].message.content'"
            )

        return str(content).strip() or "(no output from server)"

    # ------------------------------------------------------------------
    # Health check (used by __aenter__)
    # ------------------------------------------------------------------

    def _verify_connection(self) -> None:
        """Synchronous health check — verify the server is reachable.

        Issues a lightweight ``GET /v1/models``.  Some compat servers don't
        implement this endpoint (return 404), in which case we accept the
        404 as proof the server is reachable but ``/v1/models`` is unsupported.

        Raises:
            RuntimeError: If the server is unreachable entirely.
        """
        url = f"{self._base_url}{_PATH_MODELS}"
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        headers.update(self._extra_headers)

        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            # 404/405 → endpoint exists, /v1/models just not implemented. OK.
            if exc.code in (404, 405):
                logger.debug(
                    "OpenAICompatibleProvider: /v1/models returned %d "
                    "(endpoint may not be implemented; continuing)",
                    exc.code,
                )
                return
            raise RuntimeError(
                f"OpenAI-compatible server health check failed — "
                f"HTTP {exc.code}: {exc.reason}."
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Cannot reach OpenAI-compatible server at '{self._base_url}': "
                f"{exc.reason}."
            ) from exc

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> OpenAICompatibleProvider:
        logger.debug(
            "OpenAICompatibleProvider: verifying connection to %s",
            self._base_url,
        )
        await asyncio.to_thread(self._verify_connection)
        logger.debug("OpenAICompatibleProvider: connection verified")
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._pending_prompt = None


__all__ = ["ModelNotAllowedError", "OpenAICompatibleProvider"]
