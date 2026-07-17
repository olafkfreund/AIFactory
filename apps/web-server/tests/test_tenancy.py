"""Multi-tenancy (#925): tenant resolution, creation stamping, list scoping,
and the optional tenant on handoff payloads + completion envelopes.

Flag-off (no AIFACTORY_MULTI_TENANT) behavior must be byte-identical to
single-tenant AIFactory: no stamps written, nothing filtered, no envelope field.
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from server import tenancy
from server.routes import from_issue  # also puts apps/backend on sys.path


class _Req:
    """Minimal stand-in for a FastAPI Request (headers mapping + empty state)."""

    def __init__(self, headers: dict | None = None):
        self.headers = headers or {}
        self.state = type("S", (), {})()


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("AIFACTORY_MULTI_TENANT", "true")


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.delenv("AIFACTORY_MULTI_TENANT", raising=False)


# ---------------------------------------------------------------- resolution


def test_resolve_tenant_flag_off_ignores_header(flag_off):
    assert tenancy.resolve_tenant(_Req({"X-Tenant-Id": "acme"})) == "default"
    assert tenancy.resolve_tenant(None) == "default"


def test_resolve_tenant_flag_on(flag_on):
    assert tenancy.resolve_tenant(_Req({"X-Tenant-Id": "acme"})) == "acme"
    assert tenancy.resolve_tenant(_Req({"X-Tenant-Id": "  "})) == "default"
    assert tenancy.resolve_tenant(_Req()) == "default"
    assert tenancy.resolve_tenant(None) == "default"


# ------------------------------------------------------------------ stamping


def _intake_spec(project_path, tenant):
    issue = {"title": "Demo", "body": "Do the thing", "number": 7, "labels": []}
    return from_issue._write_spec(
        project_path, "001-demo", issue, {}, "low", tenant=tenant
    )


def test_intake_stamps_tenant_when_enabled(flag_on, tmp_path):
    spec_dir = _intake_spec(tmp_path / "repo", "acme")
    meta = json.loads((spec_dir / "task_metadata.json").read_text())
    assert meta["tenant_id"] == "acme"


def test_intake_flag_off_writes_no_stamp(flag_off, tmp_path):
    spec_dir = _intake_spec(tmp_path / "repo", "acme")
    tm_file = spec_dir / "task_metadata.json"
    meta = json.loads(tm_file.read_text()) if tm_file.exists() else {}
    assert "tenant_id" not in meta


def test_spec_tenant_missing_means_default(tmp_path):
    assert tenancy.read_spec_tenant(tmp_path) is None
    assert tenancy.spec_tenant(tmp_path) == "default"


# ------------------------------------------------------------- list scoping


def _seed_project(tmp_path):
    """Two specs: one stamped for tenant 'acme', one unstamped (=> default)."""
    project_path = tmp_path / "repo"
    for spec_id, tenant in [("001-a", "acme"), ("002-b", None)]:
        d = project_path / ".aifactory" / "specs" / spec_id
        d.mkdir(parents=True)
        (d / "requirements.json").write_text(
            json.dumps(
                {
                    "title": spec_id,
                    "description": "x",
                    "created_at": "2026-07-17T00:00:00",
                }
            )
        )
        if tenant:
            (d / "task_metadata.json").write_text(json.dumps({"tenant_id": tenant}))
    return project_path


async def _noop_overlay(tasks):
    return None


def _list_tasks(project_path, headers):
    from server.routes import tasks as tasks_mod

    with (
        patch.object(
            tasks_mod,
            "load_projects",
            return_value={"p1": {"path": str(project_path)}},
        ),
        patch.object(tasks_mod, "overlay_durable_status", new=_noop_overlay),
    ):
        return asyncio.run(
            tasks_mod.list_tasks(
                project_id=None, status=None, request=_Req(headers), db=None
            )
        )


def test_list_tasks_filters_by_tenant_when_enabled(flag_on, tmp_path):
    project_path = _seed_project(tmp_path)
    acme = _list_tasks(project_path, {"X-Tenant-Id": "acme"})
    assert [t.id for t in acme.tasks] == ["p1:001-a"]
    default = _list_tasks(project_path, {})
    assert [t.id for t in default.tasks] == ["p1:002-b"]


def test_list_tasks_flag_off_sees_everything(flag_off, tmp_path):
    project_path = _seed_project(tmp_path)
    result = _list_tasks(project_path, {"X-Tenant-Id": "acme"})
    assert sorted(t.id for t in result.tasks) == ["p1:001-a", "p1:002-b"]


def _list_projects(data, headers):
    from server.routes import projects as projects_mod

    with (
        patch.object(projects_mod, "load_projects", return_value=data),
        patch(
            "server.routes.project_authz.accessible_org_ids",
            new=AsyncMock(return_value=None),
        ),
    ):
        return asyncio.run(projects_mod.list_projects(request=_Req(headers), db=None))


def test_list_projects_filters_by_tenant_when_enabled(flag_on, tmp_path):
    p1 = tmp_path / "p1"
    p2 = tmp_path / "p2"
    p1.mkdir()
    p2.mkdir()
    data = {
        "p1": {"path": str(p1), "name": "a", "tenant_id": "acme"},
        "p2": {"path": str(p2), "name": "b"},  # unstamped => default tenant
    }
    acme = _list_projects(data, {"X-Tenant-Id": "acme"})
    assert [p["name"] for p in acme] == ["a"]
    default = _list_projects(data, {})
    assert [p["name"] for p in default] == ["b"]

    # Flag off: everything visible regardless of header.
    import os

    os.environ.pop("AIFACTORY_MULTI_TENANT", None)
    both = _list_projects(data, {"X-Tenant-Id": "acme"})
    assert sorted(p["name"] for p in both) == ["a", "b"]


# ------------------------------------------------------- handoff + envelope


def test_ingest_payload_carries_tenant_optionally(tmp_path, monkeypatch):
    import pfactory.tfactory_client as tc

    monkeypatch.setenv("TFACTORY_PROJECT_ID", "tf-proj")
    spec_dir = tmp_path / "042-demo"
    spec_dir.mkdir()
    (spec_dir / "spec.md").write_text("## Acceptance Criteria\n- AC#1\n")

    payload = tc.build_ingest_payload(spec_dir, "042-demo")
    assert "tenant_id" not in payload  # unstamped spec: field absent

    (spec_dir / "task_metadata.json").write_text(json.dumps({"tenant_id": "acme"}))
    payload = tc.build_ingest_payload(spec_dir, "042-demo")
    assert payload["tenant_id"] == "acme"


def test_completion_event_tenant_optional(tmp_path):
    from server.services import completion

    ev = completion.build_completion_event(
        task_id="p:001-a", spec_id="001-a", status="failed", issue_number=None
    )
    assert "tenant_id" not in ev

    ev = completion.build_completion_event(
        task_id="p:001-a",
        spec_id="001-a",
        status="failed",
        issue_number=None,
        tenant_id="acme",
    )
    assert ev["tenant_id"] == "acme"

    # The emit path reads the stamp from the spec's task_metadata.json.
    spec_dir = tmp_path / "001-a"
    spec_dir.mkdir()
    assert completion._read_tenant_id(spec_dir) is None
    (spec_dir / "task_metadata.json").write_text(json.dumps({"tenant_id": "acme"}))
    assert completion._read_tenant_id(spec_dir) == "acme"
