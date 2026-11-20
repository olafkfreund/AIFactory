"""Scoped key and legacy token principal contracts across auth surfaces."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from server import auth, project_store
from server.database import engine as database_engine
from server.database.models import (
    Base,
    Organization,
    OrgMember,
    Team,
    TeamMembership,
)
from server.mcp_remote.auth import AuthenticatedKey
from server.mcp_stdio.auth import _LegacyAdminKey, acw_principal
from server.oidc.provisioning import jit_provision_user
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

WILDCARD = "wildcard-secret-token"
# Not a real secret — the acw_/wildcard test tokens are never valid JWTs, so the
# JWT decode step just fails regardless of this value. Kept off a literal call
# arg to avoid ruff S106 (hardcoded-secret) in test source.
_JWT_SECRET = "unit-test-" + "secret"


def _settings(*, scoped_flag: bool) -> SimpleNamespace:
    """Minimal stand-in for the app Settings used by the auth middleware."""
    return SimpleNamespace(
        DISABLE_AUTH=False,
        API_TOKEN=WILDCARD,
        JWT_SECRET=_JWT_SECRET,
        JWT_ALGORITHM="HS256",
        SCOPED_SERVICE_TOKENS_ENABLED=scoped_flag,
    )


def _make_request(token: str) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/tasks",
        "raw_path": b"/api/tasks",
        "query_string": b"",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }
    return Request(scope)


async def _next(_request):
    return "OK"


def _dispatch(token: str, *, scoped_flag: bool, key: AuthenticatedKey | None):
    """Run the middleware for ``token`` and return the resolved principal.

    ``key`` is what the patched ``acw_`` validator returns (or None to make the
    ``acw_`` lookup fail, exercising the wildcard/JWT branches only).
    """
    request = _make_request(token)
    mw = auth.TokenAuthMiddleware(app=None)

    async def _fake_authenticate(_header: str) -> AuthenticatedKey:
        if key is None:
            raise RuntimeError("no acw_ key")
        return key

    with (
        patch.object(
            auth, "get_settings", return_value=_settings(scoped_flag=scoped_flag)
        ),
        patch("server.mcp_remote.auth.authenticate", _fake_authenticate),
    ):
        result = asyncio.run(mw.dispatch(request, _next))
    return request.state.user, result


_SERVICE_KEY = AuthenticatedKey(
    key_id="key-123",
    scopes=frozenset({"tasks:read", "deploy:read"}),
    user_id=None,  # no owning user => a service (M2M) key
    org_id="org-pfactory",
)


def test_scoped_service_token_carries_only_its_scopes():
    """Flag on: the service principal exposes exactly the key's scopes."""
    user, result = _dispatch("acw_svc", scoped_flag=True, key=_SERVICE_KEY)
    assert result == "OK"
    assert user["is_service"] is False
    assert user["scoped_service"] is True
    assert user["scopes"] == ["deploy:read", "tasks:read"]  # sorted, only its own
    assert user["org_id"] == "org-pfactory"
    assert user["api_key_id"] == "key-123"


def test_scopes_present_with_flag_off():
    user, result = _dispatch("acw_svc", scoped_flag=False, key=_SERVICE_KEY)
    assert result == "OK"
    assert user["is_service"] is False
    assert user["scopes"] == ["deploy:read", "tasks:read"]
    assert user["scoped_service"] is True


def test_wildcard_marked_with_flag_on():
    user, result = _dispatch(WILDCARD, scoped_flag=True, key=None)
    assert result == "OK"
    assert user == {
        "id": "default",
        "email": None,
        "role": "user",
        "is_service": False,
        "is_legacy_shared_token": True,
    }


def test_wildcard_marked_with_flag_off():
    user, result = _dispatch(WILDCARD, scoped_flag=False, key=None)
    assert result == "OK"
    assert user == {
        "id": "default",
        "email": None,
        "role": "user",
        "is_service": False,
        "is_legacy_shared_token": True,
    }


def test_stdio_principals_match_http_contract():
    assert acw_principal(_SERVICE_KEY) == auth._acw_principal(_SERVICE_KEY)
    assert acw_principal(_LegacyAdminKey()) == {
        "id": "default",
        "role": "user",
        "is_service": False,
        "is_legacy_shared_token": True,
    }


def test_websocket_legacy_principal_and_warning(caplog):
    socket = SimpleNamespace(
        headers={"authorization": f"Bearer {WILDCARD}"}, query_params={}
    )

    with (
        patch.object(auth, "get_settings", return_value=_settings(scoped_flag=False)),
        caplog.at_level("WARNING", logger=auth.logger.name),
    ):
        principal = asyncio.run(auth.authenticate_websocket(socket))
    assert principal["is_legacy_shared_token"] is True
    assert principal["is_service"] is False
    assert "DEPRECATED" in caplog.text
    assert WILDCARD not in caplog.text


@pytest.mark.asyncio
async def test_default_team_backfill_and_oidc_membership(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'authz.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(database_engine, "async_session_factory", session_factory)
    monkeypatch.setattr(database_engine, "_backfill_project_orgs", lambda: None)

    async def no_import():
        return None

    monkeypatch.setattr(project_store, "aimport_pending", no_import)
    try:
        await database_engine.seed_tenant_defaults(True)
        async with session_factory() as db:
            team = (
                await db.execute(select(Team).where(Team.is_default.is_(True)))
            ).scalar_one()
            owner = (
                await db.execute(
                    select(TeamMembership).where(
                        TeamMembership.team_id == team.id,
                        TeamMembership.user_id == "default",
                    )
                )
            ).scalar_one()
            assert owner.role == "owner"
            user = await jit_provision_user(
                db, {"sub": "oidc-user", "email": "user@example.test"}
            )
            member = (
                await db.execute(
                    select(TeamMembership).where(
                        TeamMembership.team_id == team.id,
                        TeamMembership.user_id == user.id,
                    )
                )
            ).scalar_one()
            assert member.role == "member"
            assert member.provenance == "sso"
            db.add(
                Organization(
                    id="other-org", name="Other", slug="other", owner_id=user.id
                )
            )
            db.add(OrgMember(org_id="other-org", user_id=user.id, role="admin"))
            await db.commit()
        await database_engine.seed_tenant_defaults(True)
        async with session_factory() as db:
            teams = (await db.execute(select(Team))).scalars().all()
            assert len(teams) == 2
            assert all(team.is_default for team in teams)
            other_team = next(team for team in teams if team.org_id == "other-org")
            other_member = (
                await db.execute(
                    select(TeamMembership).where(
                        TeamMembership.team_id == other_team.id,
                        TeamMembership.user_id == user.id,
                    )
                )
            ).scalar_one()
            assert other_member.role == "admin"
            assert len((await db.execute(select(TeamMembership))).scalars().all()) == 3
    finally:
        await engine.dispose()
