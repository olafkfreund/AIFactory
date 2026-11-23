"""OIDC role synchronization and backfilled team grants (#2198)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from server.database.models import Base, OrgMember, TeamMembership, User
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


def test_highest_group_role_wins_regardless_of_order(groups):
    assert _role_for_userinfo({"groups": ["readers", "admins"]}) == "admin"
    assert _role_for_userinfo({"groups": ["admins", "readers"]}) == "admin"


@pytest.mark.asyncio
async def test_backfill_upgrades_manual_preserved_and_group_removal_lowers(
    tmp_path, groups
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

        from server.database.models import AuditLog

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
            "INSERT INTO users(id,email,password_hash,role,is_active,oidc_sub) VALUES('u','u@test','x','member',1,'sub')"
        )
        db.execute(
            "INSERT INTO users(id,email,password_hash,role,is_active,oidc_sub) VALUES('u2','u2@test','x','admin',1,'sub2')"
        )
        db.execute(
            "INSERT INTO organizations(id,name,slug,owner_id,plan,allowed_models) VALUES('o','Org','default','u','free','[\"*\"]')"
        )
        db.execute(
            "INSERT INTO org_members(id,org_id,user_id,role) VALUES('om','o','u','member')"
        )
        db.execute(
            "INSERT INTO teams(id,org_id,name,slug,is_default) VALUES('t','o','Default','default',1)"
        )
        db.execute(
            "INSERT INTO team_memberships(id,team_id,user_id,role,provenance) VALUES('tm','t','u','member','manual')"
        )
        db.execute(
            "INSERT INTO team_memberships(id,team_id,user_id,role,provenance) VALUES('grant','t','u2','admin','manual')"
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
            == "manual"
        )
    command.downgrade(config, "9c4e2a6d1b83")
    with sqlite3.connect(path) as db:
        assert (
            db.execute(
                "SELECT provenance FROM team_memberships WHERE id='tm'"
            ).fetchone()[0]
            == "manual"
        )
