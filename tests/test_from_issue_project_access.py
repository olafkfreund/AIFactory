"""#1898: POST /api/tasks/from-issue requires member on the target project.

Drives the real route over HTTP (``from_issue.router`` mounted on a bare app)
with the real ``authorize_project_for_user`` against seeded SQLite. A stub
middleware stands in for ``TokenAuthMiddleware`` and installs the principal
each credential resolves to.
"""

from __future__ import annotations

import asyncio
import secrets
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from intake.poller import IntakeIssue, RepoConfig  # noqa: E402
from pfactory.tiers import Tier  # noqa: E402
from server.auth import _acw_principal  # noqa: E402
from server.database.engine import get_db  # noqa: E402
from server.routes import from_issue  # noqa: E402
from server.services import intake_poller  # noqa: E402


def _acw(user_id: str, org_id: str) -> dict:
    key = SimpleNamespace(user_id=user_id, org_id=org_id, key_id=f"k-{user_id}")
    return _acw_principal(key, scoped_service_tokens_enabled=False)


# The principal TokenAuthMiddleware installs for each credential.
PRINCIPALS = {
    "member-key": _acw("alice", "org-1"),
    "foreign-key": _acw("bob", "org-2"),
    "legacy-token": {
        "id": "default",
        "email": None,
        "role": "user",
        "is_service": True,
    },
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    from server.database.models import Base, Organization, OrgMember, User
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(
        f"sqlite+aiosqlite:///file:fi-{secrets.token_hex(8)}?mode=memory&cache=shared&uri=true"
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def _seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with sessions() as s:
            s.add_all(
                [
                    User(
                        id="alice",
                        email="a@x",
                        name="A",
                        password_hash="x",
                        role="user",
                    ),
                    User(
                        id="bob", email="b@x", name="B", password_hash="x", role="user"
                    ),
                    Organization(id="org-1", name="O1", slug="o1", owner_id="alice"),
                    Organization(id="org-2", name="O2", slug="o2", owner_id="bob"),
                    OrgMember(org_id="org-1", user_id="alice", role="member"),
                    OrgMember(org_id="org-2", user_id="bob", role="owner"),
                ]
            )
            await s.commit()

    asyncio.run(_seed())

    # CI runs with DISABLE_AUTH=true; these tests are about enforcement.
    monkeypatch.setattr("server.routes.project_authz._auth_disabled", lambda: False)
    monkeypatch.setattr(
        "server.project_registry.load_projects",
        # clonedFrom: the poller declares o/r, which #1900 binds to the project.
        lambda: {
            "p1": {
                "org_id": "org-1",
                "name": "p1",
                "clonedFrom": "https://github.com/o/r.git",
            }
        },
    )
    monkeypatch.setattr(from_issue, "_resolve_project_path", lambda _pid: tmp_path)

    async def _fetch(_pid, number, *, repo_ref=None):
        return {"number": number, "title": "Issue", "body": "", "labels": []}

    async def _no_build(**_kwargs):
        pass

    monkeypatch.setattr(from_issue, "_fetch_issue_via_provider", _fetch)
    monkeypatch.setattr(from_issue, "_start_build", _no_build)

    app = FastAPI()

    @app.middleware("http")
    async def _principal(request: Request, call_next):
        request.state.user = PRINCIPALS.get(request.headers.get("X-Credential", ""))
        return await call_next(request)

    async def _db():
        async with sessions() as s:
            yield s

    app.dependency_overrides[get_db] = _db
    app.include_router(from_issue.router, prefix="/api/tasks")
    with TestClient(app) as c:
        yield c, tmp_path


def _specs(tmp_path: Path) -> list[Path]:
    specs = tmp_path / ".aifactory" / "specs"
    return [d for d in specs.iterdir() if d.is_dir()] if specs.is_dir() else []


def _post(c, credential: str):
    return c.post(
        "/api/tasks/from-issue",
        json={"project_id": "p1", "issue_number": 7},
        headers={"X-Credential": credential},
    )


def test_foreign_acw_key_is_403(client):
    c, tmp_path = client
    res = _post(c, "foreign-key")
    assert res.status_code == 403
    assert _specs(tmp_path) == []


@pytest.mark.parametrize("credential", ["member-key", "legacy-token"])
def test_member_key_and_legacy_token_create_the_task(client, credential):
    c, tmp_path = client
    res = _post(c, credential)
    assert res.status_code == 200, res.text
    assert len(_specs(tmp_path)) == 1


def test_poller_path_still_creates_tasks(client, monkeypatch):
    """The poller POSTs its own body shape with the legacy token (_api_token)."""
    c, tmp_path = client
    sent = {}

    def _post_json(url, payload, timeout=10.0, token=None):
        res = c.post(url, json=payload, headers={"X-Credential": "legacy-token"})
        sent["status"] = res.status_code

    monkeypatch.setenv("AIFACTORY_URL", str(c.base_url))
    monkeypatch.setattr(intake_poller, "_post_json", _post_json)
    intake_poller._route_low_medium(
        RepoConfig(provider="github", repo="o/r", project_id="p1"),
        IntakeIssue(number=7, labels=["factory:low"]),
        Tier.LOW,
    )
    assert sent["status"] == 200
    assert len(_specs(tmp_path)) == 1
