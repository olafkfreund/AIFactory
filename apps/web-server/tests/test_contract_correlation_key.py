"""Tests for contract correlation key wiring in task_service and agent_service (#2266)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "apps" / "backend"))

from server.routes import task_service  # noqa: E402
from server.services.agent_service import AgentService  # noqa: E402


def _make_spec(
    project: Path,
    spec_id: str = "001-trusted",
    *,
    requirements: dict[str, Any] | None = None,
    contract_key: Any = None,
) -> Path:
    spec_dir = project / ".aifactory" / "specs" / spec_id
    spec_dir.mkdir(parents=True, exist_ok=True)
    reqs = requirements if requirements is not None else {"title": "Trusted task"}
    (spec_dir / "requirements.json").write_text(json.dumps(reqs))
    if contract_key is not None:
        ctx = spec_dir / "context"
        ctx.mkdir(parents=True, exist_ok=True)
        (ctx / "task_contract.json").write_text(
            json.dumps({"correlation_key": contract_key})
        )
    return spec_dir


def test_task_to_dict_uses_contract_correlation_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "proj"
    spec_dir = _make_spec(project, "001-trusted", contract_key="pf-337-x")

    monkeypatch.setattr(
        task_service,
        "load_projects",
        lambda: {"p1": {"path": str(project)}},
    )
    monkeypatch.setattr(task_service, "resolve_project_path", lambda _pid: project)

    task = task_service.spec_to_task("p1", spec_dir)
    payload = task_service.task_to_dict(task)
    assert payload["correlationKey"] == "pf-337-x"


def test_job_state_key_stays_s3_safe_for_issue_tasks(tmp_path: Path) -> None:
    # #2266 review: the job-state key is an S3 scope/path id (`job_s3_scope._BAD_ID`
    # rejects "/"), so it must stay the bare issue number, never `<repo>#<n>`.
    spec = tmp_path / ".aifactory" / "specs" / "001-x"
    (spec / "context").mkdir(parents=True)
    (spec / "requirements.json").write_text(
        json.dumps({"provenance": {"issue_number": 5, "repo": "o/r"}})
    )
    (spec / "context" / "task_contract.json").write_text(
        json.dumps({"correlation_key": "pf-337-x"})
    )
    svc = AgentService.__new__(AgentService)
    assert svc._read_correlation_key(tmp_path, "001-x") == "5"


@pytest.mark.parametrize(
    ("requirements", "contract_key", "expected"),
    [
        ({"title": "t"}, "pf-337-x", "pf-337-x"),
        (
            {"title": "t", "githubIssue": {"number": 5, "repo": "o/r"}},
            "pf-337-x",
            None,
        ),
        ({"title": "t"}, None, "af-p1:001-trusted"),
        (
            {"title": "t", "provenance": {"issue_number": 5, "repo": "o/r"}},
            "5",
            "af-p1:001-trusted",
        ),
    ],
)
def test_task_list_carries_the_event_key_without_slash_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requirements: dict[str, Any],
    contract_key: Any,
    expected: str | None,
) -> None:
    # #2272: GET /api/tasks (spec_to_task) carries the key CFactory's poller joins on;
    # issue keys ("o/r#5") are left out because CFactory's single-segment routes 404 on "/".
    project = tmp_path / "proj"
    spec_dir = _make_spec(project, requirements=requirements, contract_key=contract_key)
    monkeypatch.setattr(task_service, "resolve_project_path", lambda _pid: project)
    monkeypatch.setattr(
        task_service, "load_projects", lambda: {"p1": {"path": str(project)}}
    )

    task = task_service.spec_to_task("p1", spec_dir)
    assert task.correlation_key == expected
    assert task.model_dump()["correlation_key"] == expected
    if expected is not None:
        assert task_service.task_to_dict(task)["correlationKey"] == expected
