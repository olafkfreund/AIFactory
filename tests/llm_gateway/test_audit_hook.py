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
for _root in (REPO_ROOT / 'apps' / 'web-server', REPO_ROOT / 'apps' / 'backend'):
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
                org_id='org-acme',
                user_id='user-alice',
                model='gpt-4o-mini',
                input_tokens=1200,
                output_tokens=450,
                cost_usd=0.0234,
                latency_ms=1847,
                prompt_text='hello',
                response_text='hi there',
                litellm_request_id='req-1',
                action=ACTION_LLM_CALL,
            )

            from sqlalchemy import select
            async with SessionLocal() as db:
                rows = (await db.execute(select(AuditLog))).scalars().all()
                return rows

        rows = _run(_go())
        assert len(rows) == 1
        row = rows[0]
        assert row.action == 'llm.call'
        assert row.resource_type == 'llm'
        assert row.resource_id == 'gpt-4o-mini'
        assert row.org_id == 'org-acme'
        assert row.user_id == 'user-alice'
        # Design §5 — confidential tier.
        assert row.classification == 'confidential'

        details = json.loads(row.details_json)
        assert details['model'] == 'gpt-4o-mini'
        assert details['input_tokens'] == 1200
        assert details['output_tokens'] == 450
        assert details['cost_usd'] == 0.0234
        assert details['cost_source'] == 'litellm_estimate'
        assert details['latency_ms'] == 1847
        assert details['prompt_truncated'] == 'hello'
        assert details['response_truncated'] == 'hi there'
        assert details['litellm_request_id'] == 'req-1'
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
                org_id='org-acme',
                user_id='agent',
                model='gpt-4o-mini',
                prompt_text='hello',
                response_text='partial',
                action=ACTION_LLM_CALL_ABANDONED,
                error='asyncio.CancelledError',
            )

            from sqlalchemy import select
            async with SessionLocal() as db:
                return (await db.execute(select(AuditLog))).scalars().all()

        rows = _run(_go())
        assert len(rows) == 1
        assert rows[0].action == 'llm.call.abandoned'
        details = json.loads(rows[0].details_json)
        # Design §5 — abandoned rows carry the truncated flag so
        # operators can filter `details_json->>'truncated' = true`.
        assert details['truncated'] is True
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
                org_id='org-acme',
                user_id='agent',
                model='gpt-4o-mini',
                prompt_text='hello',
                response_text='',
                action=ACTION_LLM_CALL_FAILED,
                error='provider returned HTTP 503',
            )

            from sqlalchemy import select
            async with SessionLocal() as db:
                return (await db.execute(select(AuditLog))).scalars().all()

        rows = _run(_go())
        assert len(rows) == 1
        assert rows[0].action == 'llm.call.failed'
        details = json.loads(rows[0].details_json)
        assert details['error'] == 'provider returned HTTP 503'
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
                org_id='org-acme',
                user_id='user-alice',
                model='gpt-4o-mini',
                prompt_text='User alice@example.com asked about SSN 123-45-6789',
                response_text='Contact (555) 123-4567 to verify.',
            )
            from sqlalchemy import select
            async with SessionLocal() as db:
                return (await db.execute(select(AuditLog))).scalars().one()

        row = _run(_go())
        details = json.loads(row.details_json)
        assert 'alice@example.com' not in details['prompt_truncated']
        assert '123-45-6789' not in details['prompt_truncated']
        assert '[REDACTED_EMAIL]' in details['prompt_truncated']
        assert '[REDACTED_SSN]' in details['prompt_truncated']
        assert '(555) 123-4567' not in details['response_truncated']
        assert '[REDACTED_PHONE]' in details['response_truncated']
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

        big = 'A' * 10_000  # 10 KB
        async def _go():
            await write_llm_call_audit(
                org_id='org-acme',
                user_id='user-alice',
                model='gpt-4o-mini',
                prompt_text=big,
                response_text=big,
            )
            from sqlalchemy import select
            async with SessionLocal() as db:
                return (await db.execute(select(AuditLog))).scalars().one()

        row = _run(_go())
        details = json.loads(row.details_json)
        assert len(details['prompt_truncated'].encode('utf-8')) <= 4 * 1024
        assert len(details['response_truncated'].encode('utf-8')) <= 4 * 1024
        assert details.get('prompt_truncated_to_max') is True
        assert details.get('response_truncated_to_max') is True
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
            raise RuntimeError('synthetic DB failure')

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
                org_id='org-acme', user_id='agent',
                model='gpt-4o-mini',
                prompt_text='hi', response_text='hello',
            )

        # If this raises, the test fails.
        _run(_go())
    finally:
        engine_module.async_session_factory = original
