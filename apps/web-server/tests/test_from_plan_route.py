"""Tests for ``POST /api/tasks/from-plan`` handoff identity adoption (#2172)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_BACKEND = Path(__file__).resolve().parents[2] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from server.routes import execution, from_issue, github  # noqa: E402
from server.services.task_branch import recorded_branch  # noqa: E402
from trusted_plan import APPROVAL_KEY, sign_plan  # noqa: E402

KEY = "super-secret-pfactory-key"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def handoff_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AIFACTORY_TRUSTED_PLAN_KEY_PFACTORY", KEY)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)  # noqa: S603, S607

    project_path = tmp_path / "proj"
    project_path.mkdir()
    (project_path / ".aifactory" / "specs").mkdir(parents=True)
    _git(project_path, "init", "-q", "-b", "dev")
    _git(project_path, "remote", "add", "origin", str(origin))
    _git(
        project_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-qm",
        "base",
    )
    base_sha = _git(project_path, "rev-parse", "HEAD")
    _git(project_path, "push", "origin", "HEAD:refs/heads/dev")

    _git(
        project_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-qm",
        "approved plan",
    )
    approved_sha = _git(project_path, "rev-parse", "HEAD")
    _git(
        project_path,
        "push",
        "origin",
        "HEAD:refs/heads/factory/2172-approved-branch-handoff",
    )
    _git(project_path, "reset", "--hard", base_sha)

    monkeypatch.setattr(
        from_issue,
        "_project_repos",
        lambda _pid, _path: from_issue._OwnRepo(
            provider="github",
            paths=frozenset({"olafkfreund/aifactory"}),
            hosts=frozenset({"github.com"}),
            base=("", ""),
        ),
    )
    return "p1", project_path, base_sha, approved_sha


def _signed_plan(
    base_sha: str,
    approved_sha: str,
    *,
    repo: str = "olafkfreund/AIFactory",
    base_ref: str = "dev",
    source_branch: str = "factory/2172-approved-branch-handoff",
) -> dict:
    plan: dict = {
        "feature": "Approved branch handoff",
        "workflow_type": "feature",
        "correlation_key": "2172",
        "baseline": {"commit": base_sha},
        "provenance": {
            "project_id": "p1",
            "provider": "github",
            "repo": repo,
            "base_ref": base_ref,
            "source_branch": source_branch,
            "approved_head_sha": approved_sha,
            "pr_number": 88,
        },
        "phases": [
            {
                "id": "p1",
                "name": "Handoff",
                "parallel_safe": True,
                "subtasks": [
                    {
                        "id": "st1",
                        "description": "Adopt branch",
                        "status": "pending",
                        "files_to_create": [
                            "apps/web-server/server/routes/execution.py"
                        ],
                    }
                ],
            }
        ],
    }
    plan[APPROVAL_KEY] = sign_plan(
        plan,
        key=KEY,
        approved_by="pfactory",
        approval_timestamp="2026-11-20T12:00:00Z",
    )
    return plan


def _post_from_plan(
    project_id: str,
    project_path: Path,
    body: dict,
    *,
    fake_service: MagicMock | None = None,
):
    fake_service = fake_service or MagicMock(
        start_task_execution=AsyncMock(return_value=None)
    )
    app = FastAPI()
    app.include_router(execution.router, prefix="/api/tasks")
    with (
        patch.object(
            execution,
            "load_projects",
            return_value={
                project_id: {
                    "path": str(project_path),
                    "settings": {"gitRepo": "olafkfreund/AIFactory"},
                }
            },
        ),
        patch.object(execution, "resolve_project_id", return_value=project_id),
        patch.object(execution, "get_agent_service", return_value=fake_service),
        patch.object(execution, "emit_task_status", AsyncMock()),
        patch.object(
            execution,
            "TrustedContractStore",
            return_value=MagicMock(put=AsyncMock()),
        ),
    ):
        client = TestClient(app)
        resp = client.post(
            "/api/tasks/from-plan",
            params={
                "project_id": project_id,
                "title": "Approved branch handoff",
                "description": "signed contract",
            },
            json=body,
        )
    return resp, fake_service


def test_from_plan_adopts_provenance_records_branch_and_transitions_label(
    handoff_project, monkeypatch: pytest.MonkeyPatch
):
    project_id, project_path, base_sha, approved_sha = handoff_project
    plan = _signed_plan(base_sha, approved_sha)

    removed_calls: list[tuple[int, list[str]]] = []
    applied_calls: list[tuple[int, list[str]]] = []

    async def _remove_labels(num: int, labels: list[str]) -> None:
        removed_calls.append((num, labels))

    async def _apply_labels(num: int, labels: list[str]) -> None:
        applied_calls.append((num, labels))

    monkeypatch.setattr(
        github,
        "_get_project_provider",
        lambda *_a, **_k: SimpleNamespace(
            remove_labels=_remove_labels,
            apply_labels=_apply_labels,
        ),
    )

    resp, fake_service = _post_from_plan(project_id, project_path, {"plan": plan})

    assert resp.status_code == 200, resp.text
    spec_id = resp.json()["task_id"].split(":", 1)[1]
    assert (
        recorded_branch(project_path, spec_id) == "factory/2172-approved-branch-handoff"
    )
    fake_service.start_task_execution.assert_awaited_once()
    call_kwargs = fake_service.start_task_execution.await_args.kwargs
    assert call_kwargs["base_branch"] == "dev"

    spec_dir = project_path / ".aifactory" / "specs" / spec_id
    meta = json.loads((spec_dir / "task_metadata.json").read_text())
    assert meta["base_branch"] == "dev"
    assert meta["source_branch"] == "factory/2172-approved-branch-handoff"
    assert meta["approved_head_sha"] == approved_sha
    assert meta["pr_number"] == 88

    assert applied_calls == [(2172, ["factory:building"])]
    assert removed_calls and removed_calls[0][0] == 2172
    assert "factory:plan" in removed_calls[0][1]


@pytest.mark.parametrize(
    ("override", "field_name"),
    [
        ({"base_ref": "main"}, "base_ref"),
        ({"baseBranch": "main"}, "base_ref"),
        ({"source_branch": "factory/9999-other"}, "source_branch"),
        ({"approved_head_sha": "b" * 40}, "approved_head_sha"),
        ({"pr_number": 999}, "pr_number"),
        ({"provenance": {"source_branch": "factory/9999-other"}}, "source_branch"),
        ({"provenance": {"repo": "other/repo"}}, "repo"),
    ],
)
def test_from_plan_rejects_mismatched_request_handoff_fields_with_422(
    handoff_project, override: dict, field_name: str
):
    project_id, project_path, base_sha, approved_sha = handoff_project
    plan = _signed_plan(base_sha, approved_sha)

    resp, fake_service = _post_from_plan(
        project_id, project_path, {"plan": plan, **override}
    )

    assert resp.status_code == 422, resp.text
    assert any(field_name in r for r in resp.json()["detail"]["reasons"])
    fake_service.start_task_execution.assert_not_awaited()
    specs_dir = project_path / ".aifactory" / "specs"
    assert [d for d in specs_dir.iterdir() if d.is_dir()] == []


@pytest.mark.parametrize(
    "field_name", ["source_branch", "approved_head_sha", "pr_number"]
)
@pytest.mark.parametrize("in_provenance", [False, True])
def test_from_plan_rejects_unsigned_handoff_fields(
    handoff_project, field_name: str, in_provenance: bool
):
    project_id, project_path, base_sha, approved_sha = handoff_project
    plan = _signed_plan(base_sha, approved_sha)
    value = plan["provenance"].pop(field_name)
    if field_name == "approved_head_sha":
        plan["provenance"].pop("source_branch")
    plan[APPROVAL_KEY] = sign_plan(
        plan,
        key=KEY,
        approved_by="pfactory",
        approval_timestamp="2026-11-20T12:00:00Z",
    )
    override = (
        {"provenance": {field_name: value}} if in_provenance else {field_name: value}
    )

    resp, fake_service = _post_from_plan(
        project_id, project_path, {"plan": plan, **override}
    )

    assert resp.status_code == 422, resp.text
    assert (
        resp.json()["detail"]
        == "handoff fields must be inside the signed plan provenance"
    )
    fake_service.start_task_execution.assert_not_awaited()
    assert list((project_path / ".aifactory" / "specs").iterdir()) == []


def test_from_plan_accepts_matching_request_handoff_fields(handoff_project):
    project_id, project_path, base_sha, approved_sha = handoff_project
    plan = _signed_plan(base_sha, approved_sha)

    resp, fake_service = _post_from_plan(
        project_id,
        project_path,
        {
            "plan": plan,
            "base_ref": "dev",
            "source_branch": "factory/2172-approved-branch-handoff",
            "approved_head_sha": approved_sha,
            "pr_number": 88,
            "provenance": {
                "repo": "olafkfreund/AIFactory",
                "base_ref": "dev",
                "source_branch": "factory/2172-approved-branch-handoff",
                "approved_head_sha": approved_sha,
                "pr_number": 88,
            },
        },
    )

    assert resp.status_code == 200, resp.text
    fake_service.start_task_execution.assert_awaited_once()


def test_from_plan_rejects_foreign_declared_repo(handoff_project):
    project_id, project_path, base_sha, approved_sha = handoff_project
    plan = _signed_plan(base_sha, approved_sha, repo="attacker/evil-repo")

    resp, fake_service = _post_from_plan(project_id, project_path, {"plan": plan})

    assert resp.status_code == 400, resp.text
    assert "not this project's repository" in resp.json()["detail"]
    fake_service.start_task_execution.assert_not_awaited()


def test_apply_correction_request_callback_url_docstring_notes_ignored():
    field_info = execution.ApplyCorrectionRequest.model_fields["tfactory_callback_url"]
    assert "TFACTORY_BASE_URL" in (field_info.description or "")
    assert "ignored" in (field_info.description or "").lower()
