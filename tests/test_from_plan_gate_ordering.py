#!/usr/bin/env python3
"""Regression test for issue #1108.

``POST /api/tasks/from-plan`` used to allocate a spec id and write
``requirements.json`` BEFORE running the trusted-plan gate, so every rejected
plan left an orphan spec directory behind and advanced ``.counter``. The 422 was
always correct; the side effects were not. Any caller with ``member`` access
could inflate the counter and litter the workspace by POSTing tampered plans in
a loop.

The gate (``verify_trusted_plan``) is pure — signature + completeness, no
filesystem — so it must run before anything is allocated.
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from trusted_plan import APPROVAL_KEY, sign_plan

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

from server.routes import execution as execution_routes  # noqa: E402

KEY = "super-secret-cfactory-key"


@pytest.fixture
def project(tmp_path):
    project_path = tmp_path / "proj"
    (project_path / ".aifactory" / "specs").mkdir(parents=True)
    return "p1", project_path


def _post_untrusted_plan(project_id: str, project_path: Path):
    app = FastAPI()
    app.include_router(execution_routes.router, prefix="/api/tasks")

    with (
        patch.object(
            execution_routes,
            "load_projects",
            return_value={project_id: {"path": str(project_path)}},
        ),
        patch.object(execution_routes, "resolve_project_id", return_value=project_id),
        patch.object(execution_routes, "get_agent_service", return_value=MagicMock()),
    ):
        client = TestClient(app)
        return client.post(
            "/api/tasks/from-plan",
            params={
                "project_id": project_id,
                "title": "Signing key rotation status endpoint",
                "description": "tampered contract",
            },
            # Unsigned: no `approval` envelope, so the signature gate rejects it.
            json={"plan": {"feature": "Signing key rotation status endpoint"}},
        )


def test_rejected_plan_leaves_no_orphan_spec_and_burns_no_id(project):
    project_id, project_path = project
    specs_dir = project_path / ".aifactory" / "specs"

    resp = _post_untrusted_plan(project_id, project_path)

    assert resp.status_code == 422, resp.text

    # No orphan spec directory.
    assert [d.name for d in specs_dir.iterdir() if d.is_dir()] == []
    # No burned spec id: the counter must not have been touched at all.
    assert not (specs_dir / ".counter").exists()


def test_repeated_rejections_do_not_advance_the_counter(project):
    """The abuse shape from #1108: a loop of rejected POSTs must be inert."""
    project_id, project_path = project
    specs_dir = project_path / ".aifactory" / "specs"

    for _ in range(4):
        assert _post_untrusted_plan(project_id, project_path).status_code == 422

    assert list(specs_dir.iterdir()) == []


def test_signed_plan_still_allocates_and_builds(project, monkeypatch):
    """The other half of the mutation check: gating first must not gate MORE.

    A trusted-complete plan takes the same fast path it always did — spec dir
    allocated, plan installed, build started.
    """
    project_id, project_path = project
    monkeypatch.setenv("AIFACTORY_TRUSTED_PLAN_KEY_CFACTORY", KEY)

    plan = {
        "feature": "Signing key rotation status endpoint",
        "workflow_type": "feature",
        "phases": [
            {
                "id": "p1",
                "name": "Endpoint",
                "parallel_safe": True,
                "subtasks": [
                    {
                        "id": "st1",
                        "description": "status endpoint",
                        "status": "pending",
                        "files_to_create": ["app/routers/status.py"],
                    }
                ],
            }
        ],
    }
    plan[APPROVAL_KEY] = sign_plan(
        plan,
        key=KEY,
        approved_by="cfactory",
        approval_timestamp="2026-06-06T10:00:00Z",
    )

    fake_service = MagicMock()
    fake_service.start_task_execution = AsyncMock(return_value=None)

    app = FastAPI()
    app.include_router(execution_routes.router, prefix="/api/tasks")

    with (
        patch.object(
            execution_routes,
            "load_projects",
            return_value={project_id: {"path": str(project_path)}},
        ),
        patch.object(execution_routes, "resolve_project_id", return_value=project_id),
        patch.object(execution_routes, "get_agent_service", return_value=fake_service),
        patch.object(execution_routes, "emit_task_status", AsyncMock()),
    ):
        client = TestClient(app)
        resp = client.post(
            "/api/tasks/from-plan",
            params={
                "project_id": project_id,
                "title": "Signing key rotation status endpoint",
                "description": "signed contract",
            },
            json={"plan": plan},
        )

    assert resp.status_code == 200, resp.text
    spec_id = resp.json()["task_id"].split(":", 1)[1]
    spec_dir = project_path / ".aifactory" / "specs" / spec_id
    installed = json.loads((spec_dir / "implementation_plan.json").read_text())
    assert installed["feature"] == "Signing key rotation status endpoint"
    fake_service.start_task_execution.assert_awaited_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
