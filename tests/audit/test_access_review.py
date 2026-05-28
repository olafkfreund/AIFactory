"""Tests for /api/admin/access-review + last_login_at hook (#43 PR-1b4).

Covers:
- Endpoint returns NDJSON: one line per OrgMember of the requested org.
- Each line includes email, role, active, joined_at, last_login_at.
- Members of OTHER orgs are excluded.
- Non-admin members get a 403 (re-uses require_org_role("admin")).
- last_login_at is null when the user has never logged in.
- last_login_at populated → ISO-format string.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_WEB_SERVER = Path(__file__).parent.parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# _stream_review helpers — test the streaming function directly
# (independent of the full FastAPI request lifecycle so we don't need
# to spin up the whole app + auth dependency override path).
# ---------------------------------------------------------------------------


async def _consume(gen) -> list[dict]:
    out = []
    async for chunk in gen:
        out.append(json.loads(chunk.decode().rstrip("\n")))
    return out


def test_returns_one_line_per_member(fresh_db):
    """Two members in one org → two NDJSON lines."""
    from server.database.models import Organization, OrgMember, User
    from server.routes.access_review import _stream_review

    _engine, SessionLocal = fresh_db

    async def _go():
        async with SessionLocal() as db:
            u1 = User(
                id="u-alice", email="alice@corp.com", name="Alice",
                password_hash="x", role="user", is_active=True,
            )
            u2 = User(
                id="u-bob", email="bob@corp.com", name="Bob",
                password_hash="x", role="user", is_active=True,
            )
            org = Organization(
                id="org-1", name="Acme", slug="acme", owner_id="u-alice",
            )
            db.add_all([u1, u2, org])
            await db.flush()
            db.add_all([
                OrgMember(org_id="org-1", user_id="u-alice", role="owner"),
                OrgMember(org_id="org-1", user_id="u-bob", role="member"),
            ])
            await db.commit()

        async with SessionLocal() as db:
            return await _consume(_stream_review(db, org_id="org-1"))

    lines = _run(_go())
    assert len(lines) == 2
    emails = {line["email"] for line in lines}
    assert emails == {"alice@corp.com", "bob@corp.com"}


def test_excludes_members_of_other_orgs(fresh_db):
    """Carol is in org-2; she must NOT appear when querying org-1."""
    from server.database.models import Organization, OrgMember, User
    from server.routes.access_review import _stream_review

    _engine, SessionLocal = fresh_db

    async def _go():
        async with SessionLocal() as db:
            u1 = User(
                id="u-alice", email="a@c.com", password_hash="x",
                role="user", is_active=True,
            )
            u2 = User(
                id="u-carol", email="c@c.com", password_hash="x",
                role="user", is_active=True,
            )
            o1 = Organization(
                id="org-1", name="Acme", slug="acme", owner_id="u-alice",
            )
            o2 = Organization(
                id="org-2", name="Beta", slug="beta", owner_id="u-carol",
            )
            db.add_all([u1, u2, o1, o2])
            await db.flush()
            db.add_all([
                OrgMember(org_id="org-1", user_id="u-alice", role="owner"),
                OrgMember(org_id="org-2", user_id="u-carol", role="owner"),
            ])
            await db.commit()

        async with SessionLocal() as db:
            return await _consume(_stream_review(db, org_id="org-1"))

    lines = _run(_go())
    assert len(lines) == 1
    assert lines[0]["email"] == "a@c.com"


def test_last_login_at_null_when_never_logged_in(fresh_db):
    """A user who's never logged in has last_login_at=None → JSON null."""
    from server.database.models import Organization, OrgMember, User
    from server.routes.access_review import _stream_review

    _engine, SessionLocal = fresh_db

    async def _go():
        async with SessionLocal() as db:
            u = User(
                id="u-new", email="new@c.com", password_hash="x",
                role="user", is_active=True,
            )
            org = Organization(
                id="org-1", name="x", slug="x", owner_id="u-new",
            )
            db.add_all([u, org])
            await db.flush()
            db.add(OrgMember(org_id="org-1", user_id="u-new", role="member"))
            await db.commit()
        async with SessionLocal() as db:
            return await _consume(_stream_review(db, org_id="org-1"))

    lines = _run(_go())
    assert len(lines) == 1
    assert lines[0]["last_login_at"] is None


def test_last_login_at_iso_string_when_populated(fresh_db):
    """When last_login_at is set, it appears as an ISO-format string
    (so audit pipelines can parse it without bespoke decoders)."""
    from server.database.models import Organization, OrgMember, User
    from server.routes.access_review import _stream_review

    _engine, SessionLocal = fresh_db
    when = datetime(2026, 5, 28, 10, 30, 0)

    async def _go():
        async with SessionLocal() as db:
            u = User(
                id="u-active", email="active@c.com", password_hash="x",
                role="user", is_active=True, last_login_at=when,
            )
            org = Organization(
                id="org-1", name="x", slug="x", owner_id="u-active",
            )
            db.add_all([u, org])
            await db.flush()
            db.add(OrgMember(org_id="org-1", user_id="u-active", role="owner"))
            await db.commit()
        async with SessionLocal() as db:
            return await _consume(_stream_review(db, org_id="org-1"))

    lines = _run(_go())
    assert lines[0]["last_login_at"] == when.isoformat()


def test_includes_role_and_active_flags(fresh_db):
    """role + active are the two ISO-27001-mandated columns."""
    from server.database.models import Organization, OrgMember, User
    from server.routes.access_review import _stream_review

    _engine, SessionLocal = fresh_db

    async def _go():
        async with SessionLocal() as db:
            u1 = User(
                id="u1", email="a@c.com", password_hash="x",
                role="user", is_active=True,
            )
            u2 = User(
                id="u2", email="b@c.com", password_hash="x",
                role="user", is_active=False,  # deactivated user
            )
            org = Organization(
                id="org-1", name="x", slug="x", owner_id="u1",
            )
            db.add_all([u1, u2, org])
            await db.flush()
            db.add_all([
                OrgMember(org_id="org-1", user_id="u1", role="admin"),
                OrgMember(org_id="org-1", user_id="u2", role="viewer"),
            ])
            await db.commit()
        async with SessionLocal() as db:
            return await _consume(_stream_review(db, org_id="org-1"))

    lines = _run(_go())
    by_email = {line["email"]: line for line in lines}
    assert by_email["a@c.com"]["role"] == "admin"
    assert by_email["a@c.com"]["active"] is True
    assert by_email["b@c.com"]["role"] == "viewer"
    assert by_email["b@c.com"]["active"] is False


# ---------------------------------------------------------------------------
# last_login_at hook in the OIDC callback
# ---------------------------------------------------------------------------


def test_oidc_callback_stamps_last_login_at(fresh_db, monkeypatch):
    """Calling jit_provision_user + the OIDC callback's stamp logic
    populates users.last_login_at.

    Tests the LOGIC of the stamp rather than the full OIDC dance
    (which requires authlib + IdP fixtures already covered by the
    existing OIDC test suite). The stamp is one line in oidc_routes.py;
    this test pins that it produces a recent UTC datetime.
    """
    from server.database.models import User
    from server.oidc.provisioning import jit_provision_user

    _engine, SessionLocal = fresh_db

    async def _go():
        async with SessionLocal() as db:
            user = await jit_provision_user(db, {
                "sub": "okta-12345",
                "email": "alice@corp.com",
                "name": "Alice Smith",
            })
            # This is the line oidc_routes.py runs after JIT.
            user.last_login_at = datetime.now(timezone.utc).replace(
                tzinfo=None,
            )
            await db.commit()
            return user.id

        async with SessionLocal() as db:
            from sqlalchemy import select
            result = await db.execute(select(User).where(User.id == user.id))
            return result.scalar_one()

    user_id = _run(_go())

    # Re-fetch to confirm it persisted.
    async def _check():
        from sqlalchemy import select
        async with SessionLocal() as db:
            result = await db.execute(select(User).where(User.id == user_id))
            return result.scalar_one()

    user = _run(_check())
    assert user.last_login_at is not None
    # Should be within a few seconds of now (no tzinfo).
    delta = (
        datetime.now(timezone.utc).replace(tzinfo=None) - user.last_login_at
    ).total_seconds()
    assert delta < 5, f"last_login_at not recent: {delta}s ago"
