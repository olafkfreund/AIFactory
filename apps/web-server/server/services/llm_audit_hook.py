"""LLM-call audit writer (Epic #35 #38 PR-2b).

Hooks providers' response-completion / cancellation / failure paths to
write an immutable audit row per LLM call. Per design §5 there are
three action variants — every LLM-call attempt produces exactly ONE
audit row of some shape, giving SOC2 CC7.2 / ISO 27001 A.12.4.1
coverage even for cancelled or errored streams.

Action variants
---------------

- ``llm.call``           — response complete; full token counts + cost.
- ``llm.call.abandoned`` — client disconnect / ``asyncio.CancelledError``
                          mid-stream; ``details_json.truncated = True``;
                          token counts are what the provider had so far.
- ``llm.call.failed``    — provider returned 5xx or raised RuntimeError;
                          ``details_json.error`` carries the message.

PII redaction
-------------

Prompt + response strings are passed through ``PiiRedactor`` BEFORE
being stored. Redaction applies to the audit row ONLY — the LLM has
already seen the unredacted text by the time this hook fires
(intrinsic to LLM use; see design §4).

Bounded row size
----------------

Both prompt and response are truncated to ``_MAX_TEXT_BYTES`` (4 KB)
after redaction. The audit row stays bounded to ~10 KB total. Full
prompt/response capture is the operator's opt-in
``litellm.audit.fullTextCapture=true`` mode (out of scope for this
PR; v1.1 stores truncated only).

Failure-safe
------------

The whole hook is wrapped in a try/except. An audit-write failure
logs WARNING but does NOT raise — the calling provider's task has
already succeeded; we will not fail it just because audit broke.
Same pattern as ``audit_service.log_audit_event``.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any

# The PII redactor lives in apps/backend/services. The web-server
# imports from it via PYTHONPATH (the e2e + dev runners both put
# apps/backend on sys.path). Import lazily inside the function so
# that environments where the path isn't set up (some unit-test
# entry points) can still load this module without ImportError.

logger = logging.getLogger(__name__)


# 4 KB per text field. The audit row caps at ~10 KB total (metadata
# overhead + two 4 KB text fields + JSON wrapping). Matches design §5.
_MAX_TEXT_BYTES: int = 4 * 1024

# 13-month retention matches the existing audit_service default
# (SOC2 12-month evidence + 1-month buffer).
_RETENTION_DAYS: int = 395

ACTION_LLM_CALL: str = "llm.call"
ACTION_LLM_CALL_ABANDONED: str = "llm.call.abandoned"
ACTION_LLM_CALL_FAILED: str = "llm.call.failed"

# Per design §5 — distinguishes "approximate" from "authoritative" in
# chargeback queries. v1.1 estimates come from LiteLLM's internal
# pricing table; provider invoices are the only authoritative source.
_COST_SOURCE: str = "litellm_estimate"


def _truncate(text: str, max_bytes: int = _MAX_TEXT_BYTES) -> tuple[str, bool]:
    """Truncate by UTF-8 byte length. Returns ``(text, was_truncated)``.

    Uses byte length (not char length) so a row's worst-case size
    stays bounded regardless of multibyte content.
    """
    if not text:
        return text, False
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    # Decode back the safe prefix, ignoring any partial multibyte
    # sequence at the cut boundary.
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return truncated, True


def _build_redactor(extra_patterns: list[tuple[str, str]] | None):
    """Lazy-import the redactor + construct it.

    Constructed per-call rather than cached because operator-supplied
    extras can change at config reload (and the per-call cost is
    negligible — pattern compilation is microseconds).
    """
    try:
        from services.llm_pii_redactor import PiiRedactor
    except ImportError:
        # WHY: the redactor lives in apps/backend/services; if the
        # web-server's PYTHONPATH doesn't include that path, the
        # hook still produces an audit row, just without redaction.
        # Failure-safe: log + return a passthrough redactor.
        logger.warning(
            "llm_audit_hook: cannot import PiiRedactor (PYTHONPATH "
            "missing apps/backend?); proceeding WITHOUT redaction. "
            "PII may land in the audit row.",
        )
        return _PassthroughRedactor()
    return PiiRedactor(extra_patterns=extra_patterns)


class _PassthroughRedactor:
    """Fallback redactor used when the real one can't be imported.

    Never raises; never mutates. Documented in the WARNING log line
    so operators see "PII may be in the audit row" clearly.
    """

    def redact(self, text: str) -> str:
        return text


def _get_extra_patterns() -> list[tuple[str, str]] | None:
    """Read operator-supplied patterns from env (JSON-encoded).

    Format: ``LITELLM_AUDIT_EXTRA_PATTERNS='[["regex","replacement"]]'``.
    Empty / unset / unparseable -> ``None``. Helm operator wiring is
    out of scope for this PR (lands in PR-3); env support keeps the
    hook testable in isolation.
    """
    raw = os.environ.get("LITELLM_AUDIT_EXTRA_PATTERNS", "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return None
        result: list[tuple[str, str]] = []
        for entry in parsed:
            if isinstance(entry, list) and len(entry) == 2:
                result.append((str(entry[0]), str(entry[1])))
        return result or None
    except (json.JSONDecodeError, ValueError):
        logger.warning(
            "llm_audit_hook: LITELLM_AUDIT_EXTRA_PATTERNS is not valid "
            "JSON; ignoring operator patterns",
        )
        return None


async def write_llm_call_audit(
    *,
    org_id: str | None,
    user_id: str | None,
    model: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
    latency_ms: int | None = None,
    prompt_text: str = "",
    response_text: str = "",
    litellm_request_id: str | None = None,
    action: str = ACTION_LLM_CALL,
    error: str | None = None,
    extra_details: dict[str, Any] | None = None,
    prompt_outbound_scrubbed: bool = False,
) -> None:
    """Write an audit row for a single LLM call. Failure-safe.

    Parameters
    ----------
    org_id:
        Owning organization UUID. ``None`` is permitted but discouraged —
        without an org binding the row can't drive per-tenant chargeback.
    user_id:
        Acting user UUID, or ``"agent"`` for autonomous calls. ``None``
        is permitted (matches existing audit_service permissiveness).
    model:
        The model name as the provider requested it (e.g.
        ``"gpt-4o-mini"``). Stored as ``resource_id`` on the audit row.
    input_tokens / output_tokens:
        Token counts from the provider response. ``None`` for
        ``llm.call.abandoned`` / ``.failed`` when the provider didn't
        return them.
    cost_usd:
        LiteLLM's estimated cost. Stored verbatim with
        ``cost_source: "litellm_estimate"`` so chargeback queries can
        distinguish approximate from authoritative.
    latency_ms:
        Wall-clock latency the caller observed.
    prompt_text / response_text:
        Free-form user input + LLM response. PII-redacted then
        truncated to 4 KB before storage.
    litellm_request_id:
        LiteLLM's internal request ID, for cross-referencing with
        LiteLLM-side logs / metrics.
    action:
        One of ``"llm.call"`` / ``"llm.call.abandoned"`` /
        ``"llm.call.failed"``.
    error:
        Optional error message for the ``.failed`` variant.
    extra_details:
        Optional dict merged into ``details_json``. Goes through the
        redactor too (in case the caller passed user-controllable
        strings).
    prompt_outbound_scrubbed:
        v1.2 #210 — True iff the provider's scrubBeforeSend pass
        actually changed the outbound prompt before the LLM call.
        Surfaces in ``details_json.prompt_outbound_scrubbed`` so
        operators can run "show me every call where PII was scrubbed
        before send" reports
        (``details_json->>'prompt_outbound_scrubbed' = 'true'``).
        Default False preserves v1.1 row shape for callers that don't
        opt in.
    """
    try:
        # Lazy import: keeps this module loadable in tests that
        # haven't initialised the SQLAlchemy engine yet.
        from sqlalchemy import select as _select

        from ..database import AuditLog
        from ..database.engine import async_session_factory
        from .audit_chain import GENESIS, compute_hash, row_as_mapping

        redactor = _build_redactor(_get_extra_patterns())

        # Redact then truncate. Order matters: redacting AFTER
        # truncation could cut a pattern in half + miss it.
        redacted_prompt = redactor.redact(prompt_text or "")
        redacted_response = redactor.redact(response_text or "")
        prompt_truncated, prompt_was_truncated = _truncate(redacted_prompt)
        response_truncated, response_was_truncated = _truncate(redacted_response)

        details: dict[str, Any] = {
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
            "cost_source": _COST_SOURCE,
            "latency_ms": latency_ms,
            "prompt_truncated": prompt_truncated,
            "response_truncated": response_truncated,
            "litellm_request_id": litellm_request_id,
            # v1.2 #210 — always emit the flag so JSON queries can rely
            # on its presence. False (default) for callers that haven't
            # opted into scrubBeforeSend; True iff the provider's
            # pre-send pass actually changed the prompt.
            "prompt_outbound_scrubbed": bool(prompt_outbound_scrubbed),
        }

        # Streaming-audit signalling (design §5 reviewer finding #3).
        # `truncated: True` here means the STREAM was cut short, not
        # that the text fields hit the 4 KB cap (those use different
        # `*_was_truncated` flags). Operators querying for the gap
        # case key on `details_json->>'truncated' = true`.
        if action == ACTION_LLM_CALL_ABANDONED:
            details["truncated"] = True
        elif action == ACTION_LLM_CALL_FAILED and error is not None:
            details["error"] = error

        if prompt_was_truncated:
            details["prompt_truncated_to_max"] = True
        if response_was_truncated:
            details["response_truncated_to_max"] = True

        if extra_details:
            # Redact operator-passed extras too (defence in depth —
            # callers shouldn't be the source of audit-row PII).
            details.update(redactor.redact_dict(extra_details) if hasattr(
                redactor, "redact_dict",
            ) else extra_details)

        async with async_session_factory() as session:
            # Hash-chain wiring per audit_service.log_audit_event.
            last = await session.execute(
                _select(AuditLog).order_by(AuditLog.created_at.desc()).limit(1),
            )
            last_row = last.scalar_one_or_none()
            prev_hash_value = (
                compute_hash(last_row.prev_hash, row_as_mapping(last_row))
                if last_row is not None
                else GENESIS
            )

            entry = AuditLog(
                user_id=user_id,
                org_id=org_id,
                action=action,
                resource_type="llm",
                resource_id=model,
                details_json=json.dumps(details, default=str),
                retention_until=datetime.utcnow() + timedelta(days=_RETENTION_DAYS),
                prev_hash=prev_hash_value,
                # Per design §5 — LLM prompts/responses are
                # confidential-tier (the LLM has seen secret data in
                # the worst case, even if we redacted on the way out).
                classification="confidential",
            )
            session.add(entry)
            await session.commit()
    except Exception:
        # WHY: an audit-write failure must NOT propagate. The
        # underlying LLM call already happened; failing the task
        # post-hoc would corrupt the user's session for no security
        # gain. This matches the failure-safe contract from #43.
        logger.warning(
            "llm_audit_hook: failed to write LLM-call audit "
            "(action=%s model=%s); the LLM call already completed",
            action, model, exc_info=True,
        )


__all__ = [
    "ACTION_LLM_CALL",
    "ACTION_LLM_CALL_ABANDONED",
    "ACTION_LLM_CALL_FAILED",
    "write_llm_call_audit",
]
