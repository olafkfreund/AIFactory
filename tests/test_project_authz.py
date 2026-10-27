"""Tests for project object-level authorization rules (epic #318, #319).

Covers the pure ``check_project_access`` decision + ``is_service_principal``:
service/M2M bypass, missing project/org, and per-org membership + role levels.
No DB required — ``membership`` only needs a ``.role`` attribute.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server.routes.project_authz import (  # noqa: E402
    check_project_access,
    is_service_principal,
)

_PROJECT = {"org_id": "org-1", "name": "p"}


def _member(role: str):
    return SimpleNamespace(role=role)


def _denied(code: int, **kwargs):
    with pytest.raises(HTTPException) as exc:
        check_project_access(**kwargs)
    assert exc.value.status_code == code


# ── is_service_principal ───────────────────────────────────────────────────


def test_service_principal_detection():
    assert is_service_principal({"is_service": True}) is True
    assert is_service_principal({"role": "admin"}) is True
    assert is_service_principal({"role": "user", "id": "u1"}) is False
    assert is_service_principal(None) is False
    assert is_service_principal("nope") is False


# ── service / dev bypass ───────────────────────────────────────────────────


def test_service_principal_allowed_without_membership():
    # Legacy API_TOKEN (siblings + local UI): no project/membership needed.
    check_project_access({"is_service": True}, None, None, "owner")  # no raise


def test_auth_disabled_admin_allowed():
    check_project_access({"role": "admin", "id": "default"}, None, None, "owner")


# ── API keys are bound to their own org (#1913) ────────────────────────────


def test_key_principal_other_org_is_403_even_when_member():
    user = {"id": "u1", "role": "user", "api_key_id": "k", "org_id": "org-2"}
    _denied(403, user=user, project=_PROJECT, membership=_member("owner"))


def test_key_principal_own_org_allowed():
    user = {"id": "u1", "role": "user", "api_key_id": "k", "org_id": "org-1"}
    check_project_access(user, _PROJECT, _member("member"), "viewer")  # no raise


def _request(user):
    return SimpleNamespace(state=SimpleNamespace(user=user))


class _Db:
    async def execute(self, _stmt):
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(
                all=lambda: ["org-1", "org-2"], first=lambda: "org-2"
            )
        )


def test_accessible_org_ids_and_owner_org_bound_to_key(monkeypatch):
    import asyncio

    from server.routes import project_authz as pa

    monkeypatch.setattr(pa, "_auth_disabled", lambda: False)
    key = {"id": "u1", "role": "user", "api_key_id": "k", "org_id": "org-1"}
    human = {"id": "u1", "role": "user"}
    run = asyncio.run
    assert run(pa.accessible_org_ids(_request(key), _Db())) == {"org-1"}
    assert run(pa.accessible_org_ids(_request(human), _Db())) == {"org-1", "org-2"}
    assert run(pa.resolve_owner_org_id(_request(key), _Db())) == "org-1"
    assert run(pa.resolve_owner_org_id(_request(human), _Db())) == "org-2"


# ── unauthenticated ────────────────────────────────────────────────────────


def test_no_user_is_401():
    _denied(401, user=None, project=_PROJECT, membership=_member("owner"))


# ── human users: org membership required ───────────────────────────────────


def test_member_of_owning_org_allowed():
    user = {"id": "u1", "role": "user"}
    check_project_access(user, _PROJECT, _member("member"), "viewer")  # no raise


def test_missing_project_is_404():
    _denied(404, user={"id": "u1", "role": "user"}, project=None, membership=None)


def test_unowned_project_is_403():
    _denied(
        403,
        user={"id": "u1", "role": "user"},
        project={"name": "legacy"},  # no org_id
        membership=None,
    )


def test_non_member_is_403():
    _denied(403, user={"id": "u2", "role": "user"}, project=_PROJECT, membership=None)


def test_insufficient_role_is_403():
    # viewer cannot perform an action requiring 'admin'.
    _denied(
        403,
        user={"id": "u1", "role": "user"},
        project=_PROJECT,
        membership=_member("viewer"),
        minimum_role="admin",
    )


def test_sufficient_role_allowed():
    user = {"id": "u1", "role": "user"}
    check_project_access(user, _PROJECT, _member("admin"), "admin")  # no raise
    check_project_access(user, _PROJECT, _member("owner"), "admin")  # owner > admin
