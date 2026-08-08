"""Tests for the LLM-call audit hook (Epic #35 #38 PR-2b).

Covers (design §5):
- ``llm.call`` happy-path row shape.
- ``llm.call.abandoned`` variant for cancelled streams.
- ``llm.call.failed`` variant for provider errors.
- PII redaction applied to prompt + response BEFORE storage.
- ``cost_source = 'litellm_estimate'`` always present.
- 4 KB truncation on prompt + response (audit row stays bounded).
- Failure-safe contract: a session.commit() that raises does NOT
  bubble out of write_llm_call_audit.
- ``classification = 'confidential'`` set on every row.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _root in (REPO_ROOT / "apps" / "web-server", REPO_ROOT / "apps" / "backend"):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Helpers — wire the audit hook to the per-test in-memory DB
# ---------------------------------------------------------------------------


def _bind_audit_hook_to_session(SessionLocal):
    """Patch the lazy import inside write_llm_call_audit so the hook
    writes into the per-test in-memory SQLite, not the default
    web-server engine."""
    # The hook lazy-imports async_session_factory; we replace the
    # module-level binding at import time by overriding the import
    # path via sys.modules.
    import server.database.engine as engine_module
    import server.services.llm_audit_hook as hook_module

    original = engine_module.async_session_factory
    engine_module.async_session_factory = SessionLocal

    def _restore():
        engine_module.async_session_factory = original

    return _restore


# ---------------------------------------------------------------------------
# Happy path — llm.call
# ---------------------------------------------------------------------------


def test_llm_call_writes_audit_row(fresh_db):
    _engine, SessionLocal = fresh_db
    restore = _bind_audit_hook_to_session(SessionLocal)
    try:
        from server.database.models import AuditLog
        from server.services.llm_audit_hook import (
            ACTION_LLM_CALL,
            write_llm_call_audit,
        )

        async def _go():
            await write_llm_call_audit(
                org_id="org-acme",
                user_id="user-alice",
                model="gpt-4o-mini",
                input_tokens=1200,
                output_tokens=450,
                cost_usd=0.0234,
                latency_ms=1847,
                prompt_text="hello",
                response_text="hi there",
                litellm_request_id="req-1",
                action=ACTION_LLM_CALL,
            )

            from sqlalchemy import select

            async with SessionLocal() as db:
                rows = (await db.execute(select(AuditLog))).scalars().all()
                return rows

        rows = _run(_go())
        assert len(rows) == 1
        row = rows[0]
        assert row.action == "llm.call"
        assert row.resource_type == "llm"
        assert row.resource_id == "gpt-4o-mini"
        assert row.org_id == "org-acme"
        assert row.user_id == "user-alice"
        # Design §5 — confidential tier.
        assert row.classification == "confidential"

        details = json.loads(row.details_json)
        assert details["model"] == "gpt-4o-mini"
        assert details["input_tokens"] == 1200
        assert details["output_tokens"] == 450
        assert details["cost_usd"] == 0.0234
        assert details["cost_source"] == "litellm_estimate"
        assert details["latency_ms"] == 1847
        assert details["prompt_truncated"] == "hello"
        assert details["response_truncated"] == "hi there"
        assert details["litellm_request_id"] == "req-1"
    finally:
        restore()


# ---------------------------------------------------------------------------
# Abandoned variant — llm.call.abandoned
# ---------------------------------------------------------------------------


def test_abandoned_call_sets_truncated_flag(fresh_db):
    _engine, SessionLocal = fresh_db
    restore = _bind_audit_hook_to_session(SessionLocal)
    try:
        from server.database.models import AuditLog
        from server.services.llm_audit_hook import (
            ACTION_LLM_CALL_ABANDONED,
            write_llm_call_audit,
        )

        async def _go():
            await write_llm_call_audit(
                org_id="org-acme",
                user_id="agent",
                model="gpt-4o-mini",
                prompt_text="hello",
                response_text="partial",
                action=ACTION_LLM_CALL_ABANDONED,
                error="asyncio.CancelledError",
            )

            from sqlalchemy import select

            async with SessionLocal() as db:
                return (await db.execute(select(AuditLog))).scalars().all()

        rows = _run(_go())
        assert len(rows) == 1
        assert rows[0].action == "llm.call.abandoned"
        details = json.loads(rows[0].details_json)
        # Design §5 — abandoned rows carry the truncated flag so
        # operators can filter `details_json->>'truncated' = true`.
        assert details["truncated"] is True
    finally:
        restore()


# ---------------------------------------------------------------------------
# Failed variant — llm.call.failed
# ---------------------------------------------------------------------------


def test_failed_call_records_error(fresh_db):
    _engine, SessionLocal = fresh_db
    restore = _bind_audit_hook_to_session(SessionLocal)
    try:
        from server.database.models import AuditLog
        from server.services.llm_audit_hook import (
            ACTION_LLM_CALL_FAILED,
            write_llm_call_audit,
        )

        async def _go():
            await write_llm_call_audit(
                org_id="org-acme",
                user_id="agent",
                model="gpt-4o-mini",
                prompt_text="hello",
                response_text="",
                action=ACTION_LLM_CALL_FAILED,
                error="provider returned HTTP 503",
            )

            from sqlalchemy import select

            async with SessionLocal() as db:
                return (await db.execute(select(AuditLog))).scalars().all()

        rows = _run(_go())
        assert len(rows) == 1
        assert rows[0].action == "llm.call.failed"
        details = json.loads(rows[0].details_json)
        assert details["error"] == "provider returned HTTP 503"
    finally:
        restore()


# ---------------------------------------------------------------------------
# PII redaction
# ---------------------------------------------------------------------------


def test_prompt_and_response_are_redacted(fresh_db):
    _engine, SessionLocal = fresh_db
    restore = _bind_audit_hook_to_session(SessionLocal)
    try:
        from server.database.models import AuditLog
        from server.services.llm_audit_hook import write_llm_call_audit

        async def _go():
            await write_llm_call_audit(
                org_id="org-acme",
                user_id="user-alice",
                model="gpt-4o-mini",
                prompt_text="User alice@example.com asked about SSN 123-45-6789",
                response_text="Contact (555) 123-4567 to verify.",
            )
            from sqlalchemy import select

            async with SessionLocal() as db:
                return (await db.execute(select(AuditLog))).scalars().one()

        row = _run(_go())
        details = json.loads(row.details_json)
        assert "alice@example.com" not in details["prompt_truncated"]
        assert "123-45-6789" not in details["prompt_truncated"]
        assert "[REDACTED_EMAIL]" in details["prompt_truncated"]
        assert "[REDACTED_SSN]" in details["prompt_truncated"]
        assert "(555) 123-4567" not in details["response_truncated"]
        assert "[REDACTED_PHONE]" in details["response_truncated"]
    finally:
        restore()


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


def test_prompt_over_4kb_is_truncated(fresh_db):
    _engine, SessionLocal = fresh_db
    restore = _bind_audit_hook_to_session(SessionLocal)
    try:
        from server.database.models import AuditLog
        from server.services.llm_audit_hook import write_llm_call_audit

        big = "A" * 10_000  # 10 KB

        async def _go():
            await write_llm_call_audit(
                org_id="org-acme",
                user_id="user-alice",
                model="gpt-4o-mini",
                prompt_text=big,
                response_text=big,
            )
            from sqlalchemy import select

            async with SessionLocal() as db:
                return (await db.execute(select(AuditLog))).scalars().one()

        row = _run(_go())
        details = json.loads(row.details_json)
        assert len(details["prompt_truncated"].encode("utf-8")) <= 4 * 1024
        assert len(details["response_truncated"].encode("utf-8")) <= 4 * 1024
        assert details.get("prompt_truncated_to_max") is True
        assert details.get("response_truncated_to_max") is True
    finally:
        restore()


# ---------------------------------------------------------------------------
# Failure-safe contract
# ---------------------------------------------------------------------------


def test_audit_write_failure_does_not_raise(fresh_db, monkeypatch):
    """Even when SQLAlchemy raises mid-commit, the hook swallows the
    exception (the LLM call already succeeded; failing the user task
    post-hoc is unacceptable)."""
    _engine, SessionLocal = fresh_db

    # Replace async_session_factory with one that raises on enter.
    class _BoomFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            raise RuntimeError("synthetic DB failure")

        async def __aexit__(self, *_a):
            return False

    import server.database.engine as engine_module

    original = engine_module.async_session_factory
    engine_module.async_session_factory = _BoomFactory()

    try:
        from server.services.llm_audit_hook import write_llm_call_audit

        async def _go():
            # Must NOT raise.
            await write_llm_call_audit(
                org_id="org-acme",
                user_id="agent",
                model="gpt-4o-mini",
                prompt_text="hi",
                response_text="hello",
            )

        # If this raises, the test fails.
        _run(_go())
    finally:
        engine_module.async_session_factory = original


# ---------------------------------------------------------------------------
# v1.2 #210 — scrubBeforeSend audit-row signalling + provider integration
# ---------------------------------------------------------------------------


def test_prompt_outbound_scrubbed_default_false(fresh_db):
    """v1.1 compat: when the caller doesn't pass
    ``prompt_outbound_scrubbed``, the audit row records False.
    Operators querying ``details_json->>'prompt_outbound_scrubbed'``
    always see a concrete bool, never null."""
    _engine, SessionLocal = fresh_db
    restore = _bind_audit_hook_to_session(SessionLocal)
    try:
        from server.database.models import AuditLog
        from server.services.llm_audit_hook import write_llm_call_audit

        async def _go():
            await write_llm_call_audit(
                org_id="org-acme",
                user_id="user-alice",
                model="gpt-4o-mini",
                prompt_text="hello",
                response_text="hi",
            )
            from sqlalchemy import select

            async with SessionLocal() as db:
                return (await db.execute(select(AuditLog))).scalars().one()

        row = _run(_go())
        details = json.loads(row.details_json)
        assert details["prompt_outbound_scrubbed"] is False
    finally:
        restore()


def test_prompt_outbound_scrubbed_true_when_provider_scrubbed(fresh_db):
    """v1.2 #210: when the provider scrubbed the prompt pre-send, the
    flag arrives at the audit hook + lands in details_json as True."""
    _engine, SessionLocal = fresh_db
    restore = _bind_audit_hook_to_session(SessionLocal)
    try:
        from server.database.models import AuditLog
        from server.services.llm_audit_hook import write_llm_call_audit

        async def _go():
            await write_llm_call_audit(
                org_id="org-acme",
                user_id="user-alice",
                model="gpt-4o-mini",
                prompt_text="User SSN: 123-45-6789",
                response_text="ack",
                prompt_outbound_scrubbed=True,
            )
            from sqlalchemy import select

            async with SessionLocal() as db:
                return (await db.execute(select(AuditLog))).scalars().one()

        row = _run(_go())
        details = json.loads(row.details_json)
        assert details["prompt_outbound_scrubbed"] is True
        # Audit-row redaction still applies regardless of scrub-before-send.
        assert "[REDACTED_SSN]" in details["prompt_truncated"]
    finally:
        restore()


def test_scrub_before_send_when_enabled(monkeypatch):
    """Provider constructed with ``scrub_outbound=True`` sends the
    redacted prompt to the LLM. The HTTP body that hits the server
    contains ``[REDACTED_SSN]`` rather than the raw SSN."""
    monkeypatch.delenv("LITELLM_GATEWAY_URL", raising=False)
    monkeypatch.delenv("LITELLM_AUDIT_SCRUB_OUTBOUND", raising=False)

    from providers.openai_compatible import OpenAICompatibleProvider

    captured: dict[str, dict] = {}

    def _fake_http_post(self, url, payload):
        captured["payload"] = payload
        return {
            "choices": [{"message": {"role": "assistant", "content": "ack"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        }

    monkeypatch.setattr(
        OpenAICompatibleProvider,
        "_http_post",
        _fake_http_post,
    )

    # Skip the real audit-row write — we're testing the outbound body.
    async def _noop_audit(self, **_kwargs):
        return None

    monkeypatch.setattr(
        OpenAICompatibleProvider,
        "_write_audit",
        _noop_audit,
    )

    async def _go():
        provider = OpenAICompatibleProvider(
            model="gpt-4o-mini",
            base_url="http://fake.invalid",
            api_key="sk-test",
            scrub_outbound=True,
        )
        await provider.query("User SSN: 123-45-6789, email alice@example.com")
        async for _ in provider.receive_response():
            pass

    import asyncio

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_go())
    finally:
        loop.close()

    sent = captured["payload"]["messages"][0]["content"]
    assert "123-45-6789" not in sent
    assert "[REDACTED_SSN]" in sent
    assert "alice@example.com" not in sent
    assert "[REDACTED_EMAIL]" in sent


def test_no_scrub_when_disabled(monkeypatch):
    """Kill-switch (``LITELLM_AUDIT_SCRUB_OUTBOUND=false``) → outbound
    HTTP body is the raw prompt. Pre-#320 audit-only behaviour is still
    reachable for operators who explicitly opt out."""
    monkeypatch.delenv("LITELLM_GATEWAY_URL", raising=False)
    monkeypatch.setenv("LITELLM_AUDIT_SCRUB_OUTBOUND", "false")

    from providers.openai_compatible import OpenAICompatibleProvider

    captured: dict[str, dict] = {}

    def _fake_http_post(self, url, payload):
        captured["payload"] = payload
        return {
            "choices": [{"message": {"role": "assistant", "content": "ack"}}],
        }

    monkeypatch.setattr(
        OpenAICompatibleProvider,
        "_http_post",
        _fake_http_post,
    )

    async def _noop_audit(self, **_kwargs):
        return None

    monkeypatch.setattr(
        OpenAICompatibleProvider,
        "_write_audit",
        _noop_audit,
    )

    async def _go():
        provider = OpenAICompatibleProvider(
            model="gpt-4o-mini",
            base_url="http://fake.invalid",
            api_key="sk-test",
            # scrub_outbound omitted; kill-switch env=false → disabled.
        )
        assert provider._scrub_outbound is False
        await provider.query("User SSN: 123-45-6789")
        async for _ in provider.receive_response():
            pass

    import asyncio

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_go())
    finally:
        loop.close()

    sent = captured["payload"]["messages"][0]["content"]
    # v1.1 contract: raw PII hits the wire.
    assert "123-45-6789" in sent


def test_env_var_enables_scrub_outbound(monkeypatch):
    """``LITELLM_AUDIT_SCRUB_OUTBOUND=true`` enables scrubBeforeSend
    deployment-wide without per-provider plumbing."""
    monkeypatch.delenv("LITELLM_GATEWAY_URL", raising=False)
    monkeypatch.setenv("LITELLM_AUDIT_SCRUB_OUTBOUND", "true")

    from providers.openai_compatible import OpenAICompatibleProvider

    p = OpenAICompatibleProvider(
        model="gpt-4o-mini",
        base_url="http://fake.invalid",
    )
    assert p._scrub_outbound is True


def test_explicit_false_overrides_env(monkeypatch):
    """Explicit ctor ``scrub_outbound=False`` beats env=true (lets
    tests + per-tenant overrides pin behaviour deterministically)."""
    monkeypatch.delenv("LITELLM_GATEWAY_URL", raising=False)
    monkeypatch.setenv("LITELLM_AUDIT_SCRUB_OUTBOUND", "true")

    from providers.openai_compatible import OpenAICompatibleProvider

    p = OpenAICompatibleProvider(
        model="gpt-4o-mini",
        base_url="http://fake.invalid",
        scrub_outbound=False,
    )
    assert p._scrub_outbound is False


def test_outbound_scrub_default_on_and_fails_closed(monkeypatch):
    """#320: outbound scrub defaults ON (env unset), and when the
    redactor cannot be loaded the provider fails CLOSED — it raises
    instead of silently sending the raw prompt to the LLM provider.

    #1139: the "default ON" half enumerates all FOUR built-in classes on
    the wire, not the two it originally checked. Which classes reach the
    wire is decided by ``_build_outbound_redactor``'s pattern set, and a
    pattern set is configuration — the input nobody thinks to mutate
    (Factory#523). With only SSN and email asserted, narrowing the
    outbound redactor to an SSN+email subset landed green while phone
    numbers and Luhn-valid card numbers started reaching the third-party
    LLM, the audit row still showing them redacted.

    The four are enumerated rather than counted on purpose: a count of
    "the built-in high-precision set" falls to the same silent scope loss
    it is supposed to catch.
    """
    monkeypatch.delenv("LITELLM_GATEWAY_URL", raising=False)
    monkeypatch.delenv("LITELLM_AUDIT_SCRUB_OUTBOUND", raising=False)

    from providers.openai_compatible import OpenAICompatibleProvider

    captured: dict[str, dict] = {}

    def _fake_http_post(self, url, payload):
        captured["payload"] = payload
        return {"choices": [{"message": {"role": "assistant", "content": "ack"}}]}

    monkeypatch.setattr(OpenAICompatibleProvider, "_http_post", _fake_http_post)

    async def _noop_audit(self, **_kwargs):
        return None

    monkeypatch.setattr(OpenAICompatibleProvider, "_write_audit", _noop_audit)

    import asyncio

    # 1) Default ON: env unset → high-precision PII scrubbed on the wire.
    async def _default_on():
        provider = OpenAICompatibleProvider(
            model="gpt-4o-mini",
            base_url="http://fake.invalid",
            api_key="sk-test",
        )
        assert provider._scrub_outbound is True
        # 4242424242424242 is Luhn-valid, so it exercises the CC path
        # rather than the raw-digit-run rejection.
        await provider.query(
            "SSN 123-45-6789 email bob@example.com "
            "phone 555-123-4567 card 4242424242424242"
        )
        async for _ in provider.receive_response():
            pass

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_default_on())
    finally:
        loop.close()

    sent = captured["payload"]["messages"][0]["content"]
    assert "123-45-6789" not in sent
    assert "[REDACTED_SSN]" in sent
    assert "bob@example.com" not in sent
    assert "[REDACTED_EMAIL]" in sent
    assert "555-123-4567" not in sent
    assert "[REDACTED_PHONE]" in sent
    assert "4242424242424242" not in sent
    assert "[REDACTED_CC]" in sent

    # 2) Fail CLOSED: redactor unavailable → RuntimeError, no HTTP call.
    captured.clear()

    def _boom(self):
        raise ImportError("redactor module not on PYTHONPATH")

    monkeypatch.setattr(OpenAICompatibleProvider, "_build_outbound_redactor", _boom)

    async def _fail_closed():
        provider = OpenAICompatibleProvider(
            model="gpt-4o-mini",
            base_url="http://fake.invalid",
            api_key="sk-test",
        )
        await provider.query("SSN 123-45-6789")
        async for _ in provider.receive_response():
            pass

    loop = asyncio.new_event_loop()
    try:
        raised = False
        try:
            loop.run_until_complete(_fail_closed())
        except RuntimeError:
            raised = True
        assert raised, "provider must fail closed when the redactor is unavailable"
        assert "payload" not in captured, (
            "no HTTP call may reach the provider when outbound scrub fails closed"
        )
    finally:
        loop.close()


def test_outbound_scrub_fails_closed_when_a_pattern_raises(monkeypatch):
    """#320: a redaction PASS that blows up must also fail closed.

    ``PiiRedactor.redact()`` is deliberately fail-open — for the audit
    row, a partially redacted row beats no row. On the outbound path
    that same contract silently puts the raw prompt on the wire to a
    third-party LLM. ``redact_outbound`` therefore runs strict, and the
    provider turns the raise into a refusal to send. Without the strict
    flag this test sees the un-redacted SSN in the HTTP payload.
    """
    monkeypatch.delenv("LITELLM_GATEWAY_URL", raising=False)
    monkeypatch.delenv("LITELLM_AUDIT_SCRUB_OUTBOUND", raising=False)

    import re

    from providers.openai_compatible import OpenAICompatibleProvider
    from services import llm_pii_redactor

    class _ExplodingPattern:
        pattern = "<exploding>"

        def sub(self, *_args, **_kwargs):
            raise re.error("simulated pathological backtrack")

    monkeypatch.setattr(
        llm_pii_redactor,
        "_BUILTIN_PATTERNS",
        [(_ExplodingPattern(), "[REDACTED]")],
    )

    captured: dict[str, dict] = {}

    def _fake_http_post(self, url, payload):
        captured["payload"] = payload
        return {"choices": [{"message": {"role": "assistant", "content": "ack"}}]}

    monkeypatch.setattr(OpenAICompatibleProvider, "_http_post", _fake_http_post)

    async def _noop_audit(self, **_kwargs):
        return None

    monkeypatch.setattr(OpenAICompatibleProvider, "_write_audit", _noop_audit)

    import asyncio

    async def _run():
        provider = OpenAICompatibleProvider(
            model="gpt-4o-mini",
            base_url="http://fake.invalid",
            api_key="sk-test",
        )
        assert provider._scrub_outbound is True
        await provider.query("SSN 123-45-6789")
        async for _ in provider.receive_response():
            pass

    loop = asyncio.new_event_loop()
    try:
        raised = False
        try:
            loop.run_until_complete(_run())
        except RuntimeError:
            raised = True
        assert raised, "a failed redaction pass must fail closed, not send raw"
        assert "payload" not in captured, (
            "no HTTP call may carry the prompt when a redaction pass failed"
        )
    finally:
        loop.close()
