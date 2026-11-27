"""The manual PR route refuses source tasks paused for review."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from server.routes import github, pr
from server.services.task_control import write_control

TASK_ID = "p:001-task"
URL = f"/api/tasks/{TASK_ID}/worktree/create-pr"
PAUSED = json.loads(
    (
        Path(__file__).resolve().parents[3] / "tests/fixtures/questions_api.json"
    ).read_text()
)["errors"]["task_paused"]


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(pr, "load_projects", lambda: {"p": {"path": str(tmp_path)}})
    app = FastAPI()
    app.include_router(pr.router, prefix="/api/tasks")
    route = next(route for route in app.routes if isinstance(route, APIRoute))
    access = route.dependant.dependencies[0].call
    assert access is not None
    app.dependency_overrides[access] = lambda: {}
    return TestClient(app)


@pytest.mark.parametrize("reason", ["needs_input", "approval_rejected", "plan_review"])
def test_paused_task_refuses_manual_pr_before_git_or_github(
    client: TestClient, reason: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = tmp_path / ".aifactory/specs/001-task"
    spec.mkdir(parents=True)
    write_control(spec, status="human_review", review_reason=reason)
    unexpected = Mock(side_effect=AssertionError("PR work started on a paused task"))
    monkeypatch.setattr("server.services.github_app.multi_installation", unexpected)
    monkeypatch.setattr(pr, "subprocess", Mock(run=unexpected))
    monkeypatch.setattr(pr, "resolve_task_branch_fetching", unexpected)
    monkeypatch.setattr(pr, "find_pr", unexpected)
    monkeypatch.setattr(pr, "_upload_and_push", unexpected)
    monkeypatch.setattr(github, "run_gh_command", unexpected)

    response = client.post(URL)

    assert response.status_code == PAUSED["status"]
    expected = (
        PAUSED["body"]
        if reason == "needs_input"
        else {"detail": f"task is paused: {reason}"}
    )
    assert response.json() == expected
    unexpected.assert_not_called()


def test_unpaused_task_can_find_existing_pr(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = tmp_path / ".aifactory/specs/001-task"
    spec.mkdir(parents=True)
    write_control(spec, status="human_review", review_reason="awaiting_merge")
    monkeypatch.setattr("server.services.github_app.multi_installation", lambda: False)
    monkeypatch.setattr(pr, "task_repo_dir", lambda *_args: None)
    monkeypatch.setattr(
        pr, "resolve_task_branch_fetching", lambda **_kw: ("aifactory/001-task", None)
    )
    monkeypatch.setattr(github, "_use_provider_api", lambda _project: False)
    lookup = Mock(return_value=(5, "MERGED", "https://example.test/o/r/pull/5"))
    monkeypatch.setattr(pr, "find_pr", lookup)

    response = client.post(URL, json={"baseBranch": "main"})

    assert response.status_code == 200
    assert response.json()["data"]["existing"] is True
    lookup.assert_called_once_with(tmp_path, "aifactory/001-task", None)
