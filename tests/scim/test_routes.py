"""Integration tests for SCIM 2.0 CRUD routes (Epic #35 #41 PR-1b3).

Setup mirrors the SAML routes test pattern (PR-1b2):
  - In-memory SQLite via aiosqlite with a fresh schema per test.
  - FastAPI TestClient with ``app.dependency_overrides[get_db]``.
  - ``monkeypatch`` for env vars so tests can't pollute each other.
  - ``autouse`` fixtures reset the SCIM_BEARER_TOKEN between tests.

Coverage
--------
  - Bearer auth: 401 without header, 401 wrong token, 503 if env unset.
  - Users POST: happy path 201, email collision 409, userName/email mismatch 400.
  - Users GET list: paging, excludes soft-deleted by default,
    active eq false includes soft-deleted, userName filter, externalId filter,
    unsupported filter operator 400 + invalidFilter.
  - Users GET single: 404 missing, 404 soft-deleted.
  - Users PATCH Add (capitalised, Azure AD compat), Replace scalar, Remove scalar.
  - Users PUT whole-resource replace.
  - Users DELETE: 204, sets active=false. DELETE then GET: 404.
  - Groups POST: happy path 201.
  - Groups GET list / GET single / 404 on soft-deleted.
  - Groups PATCH Add on members → append (NOT replace).
  - Groups PATCH Replace on displayName.
  - Groups PATCH Remove member.
  - Groups PUT whole-resource replace.
  - Groups DELETE: 204, GET after → 404.
  - If-Match header accepted but not enforced.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_SERVER_ROOT = REPO_ROOT / "apps" / "web-server"
if str(WEB_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(WEB_SERVER_ROOT))

_BEARER = "test-bearer-token-abc123"
_HEADERS = {"Authorization": f"Bearer {_BEARER}"}


# ---------------------------------------------------------------------------
# DB fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_db(tmp_path):
    """Per-test file-backed SQLite. Returns (engine, SessionLocal).

    Uses a tmp_path file (not ``mode=memory&cache=shared``) for two reasons:
    1. ``cache=shared`` in-memory dbs require the init connection to stay
       alive — closing the init event loop tears down aiosqlite background
       threads in a way that interferes with later ``asyncio.run()`` calls
       in CI (where every test is collected up-front + pytest's plugin
       layer creates its own loops). File-backed is bulletproof.
    2. ``tmp_path`` is per-test and auto-cleaned by pytest, so isolation
       is guaranteed without nonce gymnastics.
    """
    from server.database.models import Base

    db_path = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())

    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    return engine, SessionLocal


@pytest.fixture(autouse=True)
def _set_token_env(monkeypatch):
    """Ensure SCIM_BEARER_TOKEN is set for every test (overrideable)."""
    monkeypatch.setenv("SCIM_BEARER_TOKEN", _BEARER)
    yield


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _make_app(fresh_db) -> FastAPI:
    """Build a FastAPI app with the SCIM router + DB session override."""
    from server.database.engine import get_db
    from server.scim import routes as scim_routes

    _engine, SessionLocal = fresh_db

    app = FastAPI()
    app.include_router(scim_routes.router)

    async def _override_get_db():
        async with SessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    return app


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_user(
    SessionLocal,
    email: str,
    *,
    name: str = "Test User",
    active: bool = True,
) -> str:
    """Insert a minimal User row and return its id."""
    import uuid

    from server.database.models import User

    async with SessionLocal() as s:
        user = User(
            id=str(uuid.uuid4()),
            email=email.lower(),
            name=name,
            password_hash="scim-provisioned",
            is_active=active,
        )
        s.add(user)
        await s.commit()
        return user.id


async def _seed_group(
    SessionLocal,
    display_name: str,
    *,
    external_id: str | None = None,
    active: bool = True,
) -> str:
    """Insert a ScimGroup row and return its id."""
    import uuid

    from server.database.models import ScimGroup

    async with SessionLocal() as s:
        group = ScimGroup(
            id=str(uuid.uuid4()),
            display_name=display_name,
            external_id=external_id,
            active=active,
        )
        s.add(group)
        await s.commit()
        return group.id


async def _seed_group_member(SessionLocal, group_id: str, user_id: str) -> None:
    """Add a member to an existing ScimGroup."""
    import uuid

    from server.database.models import ScimGroupMember

    async with SessionLocal() as s:
        s.add(
            ScimGroupMember(
                id=str(uuid.uuid4()),
                group_id=group_id,
                user_id=user_id,
                display=None,
            )
        )
        await s.commit()


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


def test_401_without_auth_header(fresh_db):
    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.get("/scim/v2/Users")
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"


def test_401_with_wrong_token(fresh_db):
    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.get("/scim/v2/Users", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401
    assert "Invalid bearer token" in resp.json()["detail"]["detail"]


def test_503_when_token_env_unset(fresh_db, monkeypatch):
    monkeypatch.delenv("SCIM_BEARER_TOKEN", raising=False)
    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.get("/scim/v2/Users", headers=_HEADERS)
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Users POST
# ---------------------------------------------------------------------------


def test_create_user_happy_path(fresh_db):
    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.post(
            "/scim/v2/Users",
            headers=_HEADERS,
            json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "userName": "alice@example.com",
                "emails": [{"value": "alice@example.com", "primary": True}],
                "active": True,
            },
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["userName"] == "alice@example.com"
    assert body["active"] is True
    assert "id" in body
    assert body["meta"]["resourceType"] == "User"


def test_create_user_409_on_email_collision(fresh_db):
    asyncio.run(_seed_user(fresh_db[1], "bob@example.com"))
    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.post(
            "/scim/v2/Users",
            headers=_HEADERS,
            json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "userName": "bob@example.com",
                "emails": [{"value": "bob@example.com", "primary": True}],
            },
        )
    assert resp.status_code == 409
    body = resp.json()
    assert body["scimType"] == "uniqueness"


def test_create_user_400_on_username_email_mismatch(fresh_db):
    """Design decision #6: userName must equal emails[0].value."""
    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.post(
            "/scim/v2/Users",
            headers=_HEADERS,
            json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "userName": "alice",  # not an email
                "emails": [{"value": "alice@example.com", "primary": True}],
            },
        )
    assert resp.status_code == 400
    body = resp.json()
    assert body["scimType"] == "invalidValue"


# ---------------------------------------------------------------------------
# Users GET list
# ---------------------------------------------------------------------------


def test_list_users_paging(fresh_db):
    """startIndex + count paging per RFC 7644."""
    _, SessionLocal = fresh_db
    asyncio.run(_seed_user(SessionLocal, "u1@example.com"))
    asyncio.run(_seed_user(SessionLocal, "u2@example.com"))
    asyncio.run(_seed_user(SessionLocal, "u3@example.com"))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.get("/scim/v2/Users?startIndex=1&count=2", headers=_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["totalResults"] == 3
    assert body["itemsPerPage"] == 2
    assert len(body["Resources"]) == 2

    with TestClient(app) as c:
        resp2 = c.get("/scim/v2/Users?startIndex=3&count=2", headers=_HEADERS)
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert len(body2["Resources"]) == 1


def test_list_users_excludes_soft_deleted_by_default(fresh_db):
    _, SessionLocal = fresh_db
    asyncio.run(_seed_user(SessionLocal, "active@example.com", active=True))
    asyncio.run(_seed_user(SessionLocal, "deleted@example.com", active=False))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.get("/scim/v2/Users", headers=_HEADERS)
    assert resp.status_code == 200
    emails = [r["userName"] for r in resp.json()["Resources"]]
    assert "active@example.com" in emails
    assert "deleted@example.com" not in emails


def test_list_users_filter_active_eq_false_includes_soft_deleted(fresh_db):
    _, SessionLocal = fresh_db
    asyncio.run(_seed_user(SessionLocal, "active@example.com", active=True))
    asyncio.run(_seed_user(SessionLocal, "deleted@example.com", active=False))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.get("/scim/v2/Users?filter=active+eq+false", headers=_HEADERS)
    assert resp.status_code == 200
    emails = [r["userName"] for r in resp.json()["Resources"]]
    assert "deleted@example.com" in emails
    assert "active@example.com" not in emails


def test_list_users_filter_username(fresh_db):
    _, SessionLocal = fresh_db
    asyncio.run(_seed_user(SessionLocal, "alice@example.com"))
    asyncio.run(_seed_user(SessionLocal, "bob@example.com"))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.get(
            '/scim/v2/Users?filter=userName+eq+"alice@example.com"',
            headers=_HEADERS,
        )
    assert resp.status_code == 200
    resources = resp.json()["Resources"]
    assert len(resources) == 1
    assert resources[0]["userName"] == "alice@example.com"


def test_list_users_filter_external_id_returns_empty(fresh_db):
    """externalId filter on Users is not supported in v1.1 — returns empty."""
    _, SessionLocal = fresh_db
    asyncio.run(_seed_user(SessionLocal, "alice@example.com"))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.get(
            '/scim/v2/Users?filter=externalId+eq+"ext-001"',
            headers=_HEADERS,
        )
    assert resp.status_code == 200
    assert resp.json()["totalResults"] == 0


def test_list_users_unsupported_filter_operator_returns_400(fresh_db):
    """co / pr / gt / lt / etc. → 400 + scimType: invalidFilter."""
    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.get(
            '/scim/v2/Users?filter=userName+co+"alice"',
            headers=_HEADERS,
        )
    assert resp.status_code == 400
    assert resp.json()["scimType"] == "invalidFilter"


# ---------------------------------------------------------------------------
# Users GET single
# ---------------------------------------------------------------------------


def test_get_user_404_on_missing(fresh_db):
    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.get("/scim/v2/Users/nonexistent-id", headers=_HEADERS)
    assert resp.status_code == 404


def test_get_user_404_on_soft_deleted(fresh_db):
    _, SessionLocal = fresh_db
    uid = asyncio.run(_seed_user(SessionLocal, "gone@example.com", active=False))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.get(f"/scim/v2/Users/{uid}", headers=_HEADERS)
    assert resp.status_code == 404


def test_get_user_happy_path(fresh_db):
    _, SessionLocal = fresh_db
    uid = asyncio.run(_seed_user(SessionLocal, "alice@example.com", name="Alice Smith"))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.get(f"/scim/v2/Users/{uid}", headers=_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == uid
    assert body["userName"] == "alice@example.com"


# ---------------------------------------------------------------------------
# Users PATCH
# ---------------------------------------------------------------------------


def test_patch_user_add_capitalised_active(fresh_db):
    """Azure AD sends 'Add' (capitalised). Must be accepted and applied."""
    _, SessionLocal = fresh_db
    uid = asyncio.run(_seed_user(SessionLocal, "alice@example.com", active=True))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.patch(
            f"/scim/v2/Users/{uid}",
            headers=_HEADERS,
            json={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "Add", "path": "active", "value": False}],
            },
        )
    assert resp.status_code == 200
    assert resp.json()["active"] is False


def test_patch_user_replace_display_name(fresh_db):
    _, SessionLocal = fresh_db
    uid = asyncio.run(_seed_user(SessionLocal, "alice@example.com", name="Old Name"))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.patch(
            f"/scim/v2/Users/{uid}",
            headers=_HEADERS,
            json={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {"op": "Replace", "path": "displayName", "value": "New Name"}
                ],
            },
        )
    assert resp.status_code == 200
    assert resp.json()["displayName"] == "New Name"


def test_patch_user_remove_display_name(fresh_db):
    _, SessionLocal = fresh_db
    uid = asyncio.run(_seed_user(SessionLocal, "alice@example.com", name="Alice"))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.patch(
            f"/scim/v2/Users/{uid}",
            headers=_HEADERS,
            json={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "Remove", "path": "displayName"}],
            },
        )
    assert resp.status_code == 200
    # displayName should be null/absent after Remove
    assert resp.json().get("displayName") is None


def test_patch_user_whole_resource_without_path(fresh_db):
    """PATCH without path: value dict applied to active + displayName."""
    _, SessionLocal = fresh_db
    uid = asyncio.run(_seed_user(SessionLocal, "alice@example.com", active=True))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.patch(
            f"/scim/v2/Users/{uid}",
            headers=_HEADERS,
            json={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {
                        "op": "Replace",
                        "value": {"active": False, "displayName": "Deactivated"},
                    }
                ],
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] is False
    assert body["displayName"] == "Deactivated"


# ---------------------------------------------------------------------------
# Users PUT
# ---------------------------------------------------------------------------


def test_put_user_whole_resource_replace(fresh_db):
    _, SessionLocal = fresh_db
    uid = asyncio.run(_seed_user(SessionLocal, "alice@example.com", name="Old Name"))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.put(
            f"/scim/v2/Users/{uid}",
            headers=_HEADERS,
            json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "userName": "alice@example.com",
                "emails": [{"value": "alice@example.com", "primary": True}],
                "displayName": "Alice Updated",
                "active": True,
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["displayName"] == "Alice Updated"
    assert body["active"] is True


def test_put_user_404_on_soft_deleted(fresh_db):
    _, SessionLocal = fresh_db
    uid = asyncio.run(_seed_user(SessionLocal, "gone@example.com", active=False))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.put(
            f"/scim/v2/Users/{uid}",
            headers=_HEADERS,
            json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "userName": "gone@example.com",
                "emails": [{"value": "gone@example.com"}],
                "active": True,
            },
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Users DELETE
# ---------------------------------------------------------------------------


def test_delete_user_returns_204(fresh_db):
    _, SessionLocal = fresh_db
    uid = asyncio.run(_seed_user(SessionLocal, "alice@example.com"))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.delete(f"/scim/v2/Users/{uid}", headers=_HEADERS)
    assert resp.status_code == 204


def test_delete_user_sets_active_false(fresh_db):
    _, SessionLocal = fresh_db
    uid = asyncio.run(_seed_user(SessionLocal, "alice@example.com"))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        c.delete(f"/scim/v2/Users/{uid}", headers=_HEADERS)
        # GET after delete must return 404 (Azure AD sync compat).
        resp = c.get(f"/scim/v2/Users/{uid}", headers=_HEADERS)
    assert resp.status_code == 404


def test_delete_then_get_returns_404(fresh_db):
    """Explicit regression for Azure AD DELETE-then-GET sync pattern."""
    _, SessionLocal = fresh_db
    uid = asyncio.run(_seed_user(SessionLocal, "alice@example.com"))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        del_resp = c.delete(f"/scim/v2/Users/{uid}", headers=_HEADERS)
        assert del_resp.status_code == 204
        get_resp = c.get(f"/scim/v2/Users/{uid}", headers=_HEADERS)
        assert get_resp.status_code == 404


def test_delete_user_404_on_missing(fresh_db):
    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.delete("/scim/v2/Users/does-not-exist", headers=_HEADERS)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# If-Match header accepted but not enforced
# ---------------------------------------------------------------------------


def test_if_match_accepted_but_not_enforced(fresh_db):
    """Sending a stale If-Match value must NOT cause 412. Last-writer-wins."""
    _, SessionLocal = fresh_db
    uid = asyncio.run(_seed_user(SessionLocal, "alice@example.com"))

    app = _make_app(fresh_db)
    headers = {**_HEADERS, "If-Match": '"old-etag-value"'}
    with TestClient(app) as c:
        resp = c.put(
            f"/scim/v2/Users/{uid}",
            headers=headers,
            json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "userName": "alice@example.com",
                "emails": [{"value": "alice@example.com"}],
                "active": True,
            },
        )
    # Must succeed — never 412.
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Groups POST
# ---------------------------------------------------------------------------


def test_create_group_happy_path(fresh_db):
    _, SessionLocal = fresh_db
    uid = asyncio.run(_seed_user(SessionLocal, "alice@example.com"))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.post(
            "/scim/v2/Groups",
            headers=_HEADERS,
            json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                "displayName": "Engineering",
                "members": [{"value": uid, "display": "Alice"}],
            },
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["displayName"] == "Engineering"
    assert len(body["members"]) == 1
    assert body["members"][0]["value"] == uid


def test_create_group_without_members(fresh_db):
    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.post(
            "/scim/v2/Groups",
            headers=_HEADERS,
            json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                "displayName": "Empty Group",
            },
        )
    assert resp.status_code == 201
    assert resp.json()["members"] == []


# ---------------------------------------------------------------------------
# Groups GET list
# ---------------------------------------------------------------------------


def test_list_groups_excludes_soft_deleted(fresh_db):
    _, SessionLocal = fresh_db
    asyncio.run(_seed_group(SessionLocal, "Active Group", active=True))
    asyncio.run(_seed_group(SessionLocal, "Deleted Group", active=False))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.get("/scim/v2/Groups", headers=_HEADERS)
    assert resp.status_code == 200
    names = [r["displayName"] for r in resp.json()["Resources"]]
    assert "Active Group" in names
    assert "Deleted Group" not in names


def test_list_groups_filter_external_id(fresh_db):
    _, SessionLocal = fresh_db
    asyncio.run(_seed_group(SessionLocal, "Group A", external_id="ext-001"))
    asyncio.run(_seed_group(SessionLocal, "Group B", external_id="ext-002"))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.get(
            '/scim/v2/Groups?filter=externalId+eq+"ext-001"',
            headers=_HEADERS,
        )
    assert resp.status_code == 200
    resources = resp.json()["Resources"]
    assert len(resources) == 1
    assert resources[0]["externalId"] == "ext-001"


def test_list_groups_unsupported_filter_returns_400(fresh_db):
    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.get(
            '/scim/v2/Groups?filter=displayName+co+"Eng"',
            headers=_HEADERS,
        )
    assert resp.status_code == 400
    assert resp.json()["scimType"] == "invalidFilter"


# ---------------------------------------------------------------------------
# Groups GET single
# ---------------------------------------------------------------------------


def test_get_group_404_on_missing(fresh_db):
    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.get("/scim/v2/Groups/no-such-group", headers=_HEADERS)
    assert resp.status_code == 404


def test_get_group_404_on_soft_deleted(fresh_db):
    _, SessionLocal = fresh_db
    gid = asyncio.run(_seed_group(SessionLocal, "Gone Group", active=False))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.get(f"/scim/v2/Groups/{gid}", headers=_HEADERS)
    assert resp.status_code == 404


def test_get_group_happy_path(fresh_db):
    _, SessionLocal = fresh_db
    uid = asyncio.run(_seed_user(SessionLocal, "bob@example.com"))
    gid = asyncio.run(_seed_group(SessionLocal, "Backend"))
    asyncio.run(_seed_group_member(SessionLocal, gid, uid))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.get(f"/scim/v2/Groups/{gid}", headers=_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["displayName"] == "Backend"
    assert any(m["value"] == uid for m in body["members"])


# ---------------------------------------------------------------------------
# Groups PATCH — array-append semantics (Azure AD compat)
# ---------------------------------------------------------------------------


def test_patch_group_add_member_appends(fresh_db):
    """op: Add on members MUST append, not replace. This is the most
    common SCIM bug; Azure AD relies on append for incremental sync."""
    _, SessionLocal = fresh_db
    uid1 = asyncio.run(_seed_user(SessionLocal, "alice@example.com"))
    uid2 = asyncio.run(_seed_user(SessionLocal, "bob@example.com"))
    gid = asyncio.run(_seed_group(SessionLocal, "Eng"))
    asyncio.run(_seed_group_member(SessionLocal, gid, uid1))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.patch(
            f"/scim/v2/Groups/{gid}",
            headers=_HEADERS,
            json={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {
                        "op": "Add",
                        "path": "members",
                        "value": [{"value": uid2, "display": "Bob"}],
                    }
                ],
            },
        )
    assert resp.status_code == 200
    members = resp.json()["members"]
    member_ids = {m["value"] for m in members}
    # BOTH original member and new member must be present.
    assert uid1 in member_ids
    assert uid2 in member_ids


def test_patch_group_remove_member(fresh_db):
    _, SessionLocal = fresh_db
    uid = asyncio.run(_seed_user(SessionLocal, "alice@example.com"))
    gid = asyncio.run(_seed_group(SessionLocal, "Eng"))
    asyncio.run(_seed_group_member(SessionLocal, gid, uid))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.patch(
            f"/scim/v2/Groups/{gid}",
            headers=_HEADERS,
            json={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {
                        "op": "Remove",
                        "path": "members",
                        "value": [{"value": uid}],
                    }
                ],
            },
        )
    assert resp.status_code == 200
    assert resp.json()["members"] == []


def test_patch_group_replace_display_name(fresh_db):
    _, SessionLocal = fresh_db
    gid = asyncio.run(_seed_group(SessionLocal, "Old Name"))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.patch(
            f"/scim/v2/Groups/{gid}",
            headers=_HEADERS,
            json={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {"op": "Replace", "path": "displayName", "value": "New Name"}
                ],
            },
        )
    assert resp.status_code == 200
    assert resp.json()["displayName"] == "New Name"


# ---------------------------------------------------------------------------
# Groups PUT
# ---------------------------------------------------------------------------


def test_put_group_replaces_members(fresh_db):
    """PUT replaces the members list entirely (not append)."""
    _, SessionLocal = fresh_db
    uid1 = asyncio.run(_seed_user(SessionLocal, "alice@example.com"))
    uid2 = asyncio.run(_seed_user(SessionLocal, "bob@example.com"))
    gid = asyncio.run(_seed_group(SessionLocal, "Eng"))
    asyncio.run(_seed_group_member(SessionLocal, gid, uid1))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.put(
            f"/scim/v2/Groups/{gid}",
            headers=_HEADERS,
            json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                "displayName": "Engineering",
                "members": [{"value": uid2, "display": "Bob"}],
            },
        )
    assert resp.status_code == 200
    member_ids = {m["value"] for m in resp.json()["members"]}
    assert uid2 in member_ids
    # Original member replaced — uid1 must be gone.
    assert uid1 not in member_ids


def test_put_group_404_on_soft_deleted(fresh_db):
    _, SessionLocal = fresh_db
    gid = asyncio.run(_seed_group(SessionLocal, "Gone", active=False))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.put(
            f"/scim/v2/Groups/{gid}",
            headers=_HEADERS,
            json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                "displayName": "Try to resurrect",
                "members": [],
            },
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Groups DELETE
# ---------------------------------------------------------------------------


def test_delete_group_returns_204(fresh_db):
    _, SessionLocal = fresh_db
    gid = asyncio.run(_seed_group(SessionLocal, "Temp Group"))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.delete(f"/scim/v2/Groups/{gid}", headers=_HEADERS)
    assert resp.status_code == 204


def test_delete_group_then_get_returns_404(fresh_db):
    """Azure AD sync compat: DELETE then GET must be 404."""
    _, SessionLocal = fresh_db
    gid = asyncio.run(_seed_group(SessionLocal, "Temp Group"))

    app = _make_app(fresh_db)
    with TestClient(app) as c:
        c.delete(f"/scim/v2/Groups/{gid}", headers=_HEADERS)
        resp = c.get(f"/scim/v2/Groups/{gid}", headers=_HEADERS)
    assert resp.status_code == 404


def test_delete_group_404_on_missing(fresh_db):
    app = _make_app(fresh_db)
    with TestClient(app) as c:
        resp = c.delete("/scim/v2/Groups/nonexistent", headers=_HEADERS)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Groups If-Match
# ---------------------------------------------------------------------------


def test_group_if_match_accepted_but_not_enforced(fresh_db):
    _, SessionLocal = fresh_db
    gid = asyncio.run(_seed_group(SessionLocal, "Eng"))

    app = _make_app(fresh_db)
    headers = {**_HEADERS, "If-Match": '"stale-etag"'}
    with TestClient(app) as c:
        resp = c.put(
            f"/scim/v2/Groups/{gid}",
            headers=headers,
            json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                "displayName": "Engineering Updated",
                "members": [],
            },
        )
    # Must not return 412.
    assert resp.status_code == 200
