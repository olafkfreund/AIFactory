"""The project-identity revision on a populated database (#2157)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

_SERVER = Path(__file__).resolve().parents[1] / "server"
PREV = "7a1f3c9e2b50"
REV = "3c8e5a1d7f42"


def _cfg(url: str) -> Config:
    cfg = Config(str(_SERVER.parent / "alembic.ini"))
    cfg.set_main_option("script_location", str(_SERVER / "database" / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _cols(db: Path, table: str) -> set[str]:
    with sqlite3.connect(db) as c:
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{path}"
    monkeypatch.setenv("DATABASE_URL", url)
    return path, _cfg(url)


def test_upgrade_keeps_rows_and_fks_then_downgrade(db):
    path, cfg = db
    command.upgrade(cfg, PREV)
    with sqlite3.connect(path) as c:
        c.execute(
            "insert into users(id,email,password_hash,role,is_active) values('u','u@x','x','user',1)"
        )
        c.execute(
            "insert into organizations(id,name,slug,owner_id,plan,allowed_models) values('o','o','o','u','free','[\"*\"]')"
        )
        c.execute(
            "insert into projects(id,org_id,name,path) values('p1','o','n','/tmp/p1')"
        )
        c.execute(
            "insert into tasks(id,project_id,title,status) values('t1','p1','t','backlog')"
        )

    command.upgrade(cfg, REV)
    assert {"slug", "repo_identity", "team_id", "archived_at"} <= _cols(
        path, "projects"
    )
    with sqlite3.connect(path) as c:
        assert c.execute("select id,name,slug from projects").fetchall() == [
            ("p1", "n", None)
        ]
        assert c.execute("select project_id from tasks").fetchall() == [("p1",)]
        assert c.execute("pragma foreign_key_check").fetchall() == []
        # slug is unique per org
        c.execute("update projects set slug='a' where id='p1'")
        c.execute(
            "insert into projects(id,org_id,name,path,slug) values('p2','o','n2','/tmp/p2','b')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(
                "insert into projects(id,org_id,name,path,slug) values('p3','o','n3','/tmp/p3','a')"
            )

    command.downgrade(cfg, PREV)
    assert "slug" not in _cols(path, "projects")
    assert not _cols(path, "teams")
    with sqlite3.connect(path) as c:
        assert c.execute("select count(*) from projects").fetchone()[0] >= 1
        assert c.execute("select count(*) from tasks").fetchone()[0] == 1
