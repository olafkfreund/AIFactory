"""Factory#313 — audit COMPLETENESS coverage for security-relevant rejections.

Proves that an authentication failure and an authorization denial each produce
a CHAINED audit record (prev_hash set, chain verifies), and that the background
writer — previously unchained — now chains its rows too.
"""

from __future__ import annotations

import asyncio

import pytest


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.mark.audit
def test_auth_failure_writes_chained_audit(fresh_db, monkeypatch) -> None:
    """A rejected credential (the 401 path in auth.py) writes a hash-chained
    ``auth.failure`` row via the now-chained background writer."""
    from server.database import engine as db_engine
    from server.database.models import AuditLog
    from server.services import audit_service
    from server.services.audit_chain import GENESIS, row_as_mapping, verify_chain
    from sqlalchemy import select

    _engine, SessionLocal = fresh_db
    # log_audit_event_bg opens its own session via async_session_factory;
    # point it at the in-memory test DB. audit_service reads
    # db_engine.async_session_factory live (Factory#import-of-mutable-attribute
    # fix), so the patch target is the defining module, not a frozen
    # import-time copy in the consumer.
    monkeypatch.setattr(db_engine, "async_session_factory", SessionLocal)

    class _Req:
        class _Client:
            host = "203.0.113.7"

        client = _Client()

        class url:  # noqa: N801 - mimic starlette request.url.path
            path = "/api/projects"

    from server.auth import _audit_auth_failure

    _run(_audit_auth_failure(_Req(), reason="invalid_token"))

    async def _fetch():
        async with SessionLocal() as s:
            result = await s.execute(
                select(AuditLog).order_by(AuditLog.created_at.asc())
            )
            return [row_as_mapping(r) for r in result.scalars()]

    rows = _run(_fetch())
    _require(len(rows) == 1, f"expected 1 audit row, got {len(rows)}")
    _require(
        rows[0]["action"] == audit_service.ACTION_AUTH_FAILURE,
        f"unexpected action {rows[0]['action']!r}",
    )
    # CHAINED: first row's prev_hash is the genesis sentinel (not NULL, as the
    # old unchained bg path left it), and the chain verifies.
    _require(
        rows[0]["prev_hash"] == GENESIS,
        f"auth-failure row not chained; prev_hash={rows[0]['prev_hash']!r}",
    )
    ok, bad, reason = verify_chain(rows)
    _require(ok, f"chain broke at {bad}: {reason}")


@pytest.mark.audit
def test_authz_denial_writes_chained_audit(fresh_db, monkeypatch) -> None:
    """A human user denied access to a project (403) writes a chained
    ``authz.denied`` row on the request-scoped session."""
    from fastapi import HTTPException
    from server.database.models import AuditLog
    from server.routes import project_authz
    from server.services import audit_service
    from server.services.audit_chain import GENESIS, row_as_mapping, verify_chain
    from sqlalchemy import select

    _engine, SessionLocal = fresh_db

    # The test suite runs with auth disabled (dev mode); force it ON so the
    # real authz rule runs instead of the DISABLE_AUTH short-circuit.
    monkeypatch.setattr(project_authz, "_auth_disabled", lambda: False)

    # A project owned by an org the user is NOT a member of → membership=None →
    # 403 authz.denied. Stub load_projects so no real project store is needed.
    project_authz_projects = {"proj-1": {"id": "proj-1", "org_id": "org-x"}}

    import server.project_registry as projects_mod

    monkeypatch.setattr(projects_mod, "load_projects", lambda: project_authz_projects)

    user = {"id": "user-9", "role": "user"}

    async def _go():
        async with SessionLocal() as s:
            raised = False
            try:
                await project_authz.authorize_project_for_user(
                    user, "proj-1", s, minimum_role="viewer"
                )
            except HTTPException as exc:
                raised = True
                _require(exc.status_code == 403, f"expected 403, got {exc.status_code}")
            await s.commit()
            _require(raised, "authz denial did not raise 403")

            result = await s.execute(
                select(AuditLog).order_by(AuditLog.created_at.asc())
            )
            return [row_as_mapping(r) for r in result.scalars()]

    rows = _run(_go())
    _require(len(rows) == 1, f"expected 1 audit row, got {len(rows)}")
    _require(
        rows[0]["action"] == audit_service.ACTION_AUTHZ_DENIED,
        f"unexpected action {rows[0]['action']!r}",
    )
    _require(rows[0]["resource_id"] == "proj-1", "resource_id should be the project id")
    _require(
        rows[0]["prev_hash"] == GENESIS,
        f"authz-denial row not chained; prev_hash={rows[0]['prev_hash']!r}",
    )
    ok, bad, reason = verify_chain(rows)
    _require(ok, f"chain broke at {bad}: {reason}")


def _require(cond: bool, msg: str) -> None:
    """assert-free check (ruff S101 bans bare assert in this tree)."""
    if not cond:
        raise AssertionError(msg)
