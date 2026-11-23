"""OIDC role synchronization and backfilled team grants (#2198)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
import server.database.engine as database_engine
from alembic import command
from alembic.config import Config
from server import project_store
from server.database.models import AuditLog, Base, OrgMember, TeamMembership, User
from server.oidc.provisioning import _role_for_userinfo, jit_provision_user
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
def groups(monkeypatch):
    monkeypatch.setenv("APP_OIDC_DEFAULT_ROLE", "member")
    monkeypatch.setenv(
        "APP_OIDC_GROUP_TO_ROLE", json.dumps({"admins": "admin", "readers": "viewer"})
    )
    monkeypatch.setenv("APP_OIDC_DEFAULT_ORG_SLUG", "default")


@pytest.mark.usefixtures("groups")
def test_highest_group_role_wins_regardless_of_order():
    assert _role_for_userinfo({"groups": ["readers", "admins"]}) == "admin"
    assert _role_for_userinfo({"groups": ["admins", "readers"]}) == "admin"


@pytest.mark.usefixtures("groups")
def test_invalid_default_role_and_noniterable_groups_fall_back(monkeypatch, caplog):
    monkeypatch.setenv("APP_OIDC_DEFAULT_ROLE", "superuser")
    assert _role_for_userinfo({"groups": ["unknown"]}) == "member"
    assert "APP_OIDC_DEFAULT_ROLE is invalid" in caplog.text
    assert _role_for_userinfo({"groups": 42}) == "member"


@pytest.mark.asyncio
async def test_startup_backfill_marks_only_oidc_users(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'seed.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        session.add(User(id="local", email="local@test", password_hash="x"))  # noqa: S106
        session.add(
            User(id="oidc", email="oidc@test", password_hash="x", oidc_sub="sub")  # noqa: S106
        )
        await session.commit()
    monkeypatch.setattr(database_engine, "async_session_factory", sessions)
    monkeypatch.setattr(database_engine, "_backfill_project_orgs", lambda: None)

    async def no_import():
        return None

    monkeypatch.setattr(project_store, "aimport_pending", no_import)
    await database_engine.seed_tenant_defaults(True)
    async with sessions() as session:
        members = (await session.execute(select(TeamMembership))).scalars().all()
        assert {member.user_id: member.provenance for member in members} == {
            "default": "manual",
            "local": "manual",
            "oidc": "backfill",
        }
        assert next(m for m in members if m.user_id == "default").role == "owner"
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.usefixtures("groups")
async def test_backfill_upgrades_manual_preserved_and_group_removal_lowers(
    tmp_path,
):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'roles.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        claims = {"sub": "subject", "email": "user@test.invalid", "groups": ["admins"]}
        user = await jit_provision_user(db, claims)
        team_member = (await db.execute(select(TeamMembership))).scalar_one()
        team_member.provenance = "backfill"
        team_member.role = "member"
        await db.commit()
        await jit_provision_user(db, claims)
        assert team_member.role == "admin"
        assert user.role == "admin"
        assert (await db.execute(select(OrgMember))).scalar_one().role == "admin"

        team_member.provenance = "manual"
        team_member.role = "owner"
        await db.commit()
        claims["groups"] = []
        await jit_provision_user(db, claims)
        assert team_member.role == "owner"
        assert user.role == "member"
        assert (await db.execute(select(OrgMember))).scalar_one().role == "member"

        audits = (await db.execute(select(AuditLog))).scalars().all()
        assert any(
            a.action == "member.role_change" and a.resource_type == "user"
            for a in audits
        )
        assert any(
            a.action == "member.role_change" and a.resource_type == "team_membership"
            for a in audits
        )
    await engine.dispose()


def test_backfill_migration_up_and_down_on_sqlite(tmp_path, monkeypatch):
    path = tmp_path / "migration.db"
    url = f"sqlite+aiosqlite:///{path}"
    monkeypatch.setenv("DATABASE_URL", url)
    server = Path(__file__).resolve().parents[1]
    config = Config(str(server / "alembic.ini"))
    config.set_main_option(
        "script_location", str(server / "server" / "database" / "alembic")
    )
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "9c4e2a6d1b83")
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO users(id,email,password_hash,role,is_active,oidc_sub) "
            "VALUES('u','u@test','x','member',1,'sub')"
        )
        db.execute(
            "INSERT INTO users(id,email,password_hash,role,is_active,oidc_sub) "
            "VALUES('u2','u2@test','x','admin',1,'sub2')"
        )
        db.execute(
            "INSERT INTO users(id,email,password_hash,role,is_active,oidc_sub) "
            "VALUES('default','default@test','x','owner',1,'default-sub')"
        )
        db.execute(
            "INSERT INTO organizations(id,name,slug,owner_id,plan,allowed_models) "
            "VALUES('o','Org','default','u','free','[\"*\"]')"
        )
        db.execute(
            "INSERT INTO org_members(id,org_id,user_id,role) VALUES('om','o','u','member')"
        )
        db.execute(
            "INSERT INTO org_members(id,org_id,user_id,role) VALUES('om2','o','u2','admin')"
        )
        db.execute(
            "INSERT INTO teams(id,org_id,name,slug,is_default) "
            "VALUES('t','o','Default','default',1)"
        )
        db.execute(
            "INSERT INTO team_memberships(id,team_id,user_id,role,provenance) "
            "VALUES('tm','t','u','member','manual')"
        )
        db.execute(
            "INSERT INTO team_memberships(id,team_id,user_id,role,provenance) "
            "VALUES('grant','t','u2','member','manual')"
        )
        db.execute(
            "INSERT INTO team_memberships(id,team_id,user_id,role,provenance) "
            "VALUES('owner','t','default','owner','manual')"
        )
    command.upgrade(config, "6d2f0a9b8c41")
    with sqlite3.connect(path) as db:
        assert (
            db.execute(
                "SELECT provenance FROM team_memberships WHERE id='tm'"
            ).fetchone()[0]
            == "backfill"
        )
        assert (
            db.execute(
                "SELECT provenance FROM team_memberships WHERE id='grant'"
            ).fetchone()[0]
            == "backfill"
        )
        assert (
            db.execute(
                "SELECT provenance FROM team_memberships WHERE id='owner'"
            ).fetchone()[0]
            == "manual"
        )

    monkeypatch.setenv("APP_OIDC_DEFAULT_ROLE", "member")
    monkeypatch.setenv("APP_OIDC_GROUP_TO_ROLE", json.dumps({"owners": "owner"}))

    async def login_after_migration() -> None:
        engine = create_async_engine(url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            await jit_provision_user(
                session,
                {"sub": "sub2", "email": "u2@test", "groups": ["owners"]},
            )
            team_member = await session.get(TeamMembership, "grant")
            assert team_member is not None
            assert team_member.role == "owner"
            org_member = await session.get(OrgMember, "om2")
            assert org_member is not None
            assert org_member.role == "owner"
        await engine.dispose()

    asyncio.run(login_after_migration())
    command.downgrade(config, "9c4e2a6d1b83")
    with sqlite3.connect(path) as db:
        assert (
            db.execute(
                "SELECT provenance FROM team_memberships WHERE id='tm'"
            ).fetchone()[0]
            == "manual"
        )
