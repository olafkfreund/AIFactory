"""Attribution survives intake and appears on task GET responses."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from runners.github.providers.protocol import ProviderType  # noqa: E402
from server.routes import (  # noqa: E402
    execution,
    from_issue,
    github,
    projects,
    task_service,
    tasks,
)
from server.routes.from_issue import FromIssueRequest  # noqa: E402
from server.routes.github import ImportIssuesRequest  # noqa: E402

mcp_router = importlib.import_module("server.mcp_stdio.router")


def _requirements(project: Path, task_id: str) -> dict:
    spec_id = task_id.split(":", 1)[-1]
    return json.loads(
        (project / ".aifactory" / "specs" / spec_id / "requirements.json").read_text()
    )


@pytest.mark.parametrize(
    ("principal", "expected"),
    [
        ({"id": "u1", "api_key_id": "k1"}, "u1"),
        ({"id": "default", "is_service": True}, None),
        ({"id": "default"}, None),
    ],
)
async def test_mcp_create_and_run_records_principal(
    tmp_path, monkeypatch, principal, expected
):
    class AgentService:
        async def start_spec_creation(self, **_kwargs):
            pass

    class Request:
        async def body(self):
            return b"{}"

        client = None

    async def no_audit(*_args, **_kwargs):
        pass

    monkeypatch.setattr(
        execution, "load_projects", lambda: {"p": {"path": str(tmp_path)}}
    )
    monkeypatch.setattr(execution, "get_agent_service", AgentService)
    monkeypatch.setattr(execution, "audit_task_action", no_audit)
    monkeypatch.setattr(mcp_router, "acw_principal", lambda _key: principal)
    monkeypatch.setattr(mcp_router, "_audit_mcp_write", no_audit)

    result = await mcp_router.proxy_create_and_run_task(
        Request(), "p", "Attribution", "Build it", SimpleNamespace(key_id="k1")
    )
    requirements = _requirements(tmp_path, result["task_id"])
    assert requirements["originator"] == expected
    assert requirements["agent_identity"] == (
        "agent:aifactory/k1" if principal.get("api_key_id") else None
    )


@pytest.mark.parametrize("author", ["octocat", ""])
async def test_provider_issue_records_author(tmp_path, monkeypatch, author):
    async def fetch_issue(_number):
        return SimpleNamespace(
            number=42,
            title="Issue",
            body="",
            url="",
            state="open",
            labels=[],
            author=author,
        )

    async def no_build(**_kwargs):
        pass

    monkeypatch.setattr(from_issue, "_resolve_project_path", lambda _id: tmp_path)
    monkeypatch.setattr(
        github,
        "_get_project_provider",
        lambda *_a, **_k: SimpleNamespace(
            fetch_issue=fetch_issue, provider_type=ProviderType.GITHUB
        ),
    )
    monkeypatch.setattr(from_issue, "_start_build", no_build)
    result = await from_issue.create_from_issue(
        FromIssueRequest(project_id="p", issue_number=42)
    )
    requirements = _requirements(tmp_path, result["spec_id"])
    assert requirements["originator"] == ("github:octocat" if author else None)


@pytest.mark.parametrize("attributed", [True, False])
async def test_get_task_surfaces_attribution(tmp_path, monkeypatch, attributed):
    spec_dir = tmp_path / ".aifactory" / "specs" / "001-task"
    spec_dir.mkdir(parents=True)
    requirements = {"title": "Task", "description": "Details"}
    if attributed:
        requirements.update(originator="u1", agent_identity="agent:aifactory/k1")
    (spec_dir / "requirements.json").write_text(json.dumps(requirements))

    monkeypatch.setattr(tasks, "load_projects", lambda: {"p": {"path": str(tmp_path)}})
    monkeypatch.setattr(task_service, "load_projects", tasks.load_projects)

    async def no_overlay(_tasks):
        pass

    monkeypatch.setattr(tasks, "overlay_durable_status", no_overlay)
    result = await tasks.get_task("p:001-task", _access={})
    assert result["originator"] == ("u1" if attributed else None)
    assert result["agentIdentity"] == ("agent:aifactory/k1" if attributed else None)


@pytest.mark.parametrize(
    ("principal", "expected"),
    [({"id": "u1", "api_key_id": "k1"}, "u1"), ({"id": "default"}, None)],
)
async def test_project_task_create_records_principal(
    tmp_path, monkeypatch, principal, expected
):
    """POST /api/projects/{id}/tasks: the web UI's create path."""
    (tmp_path / ".aifactory" / "specs").mkdir(parents=True)
    monkeypatch.setattr(
        projects, "load_projects", lambda: {"p": {"path": str(tmp_path)}}
    )
    monkeypatch.setattr(tasks, "spec_to_task", lambda _pid, sd: sd)
    monkeypatch.setattr(tasks, "task_to_dict", lambda sd: {"spec_dir": str(sd)})

    async def no_audit(*_args, **_kwargs):
        pass

    monkeypatch.setattr(projects, "audit_task_action", no_audit)
    result = await projects.create_project_task(
        "p",
        projects.TaskCreateRequest(title="Attribution", description="Build it"),
        None,
        principal,
    )
    requirements = json.loads(
        (Path(result["spec_dir"]) / "requirements.json").read_text()
    )
    assert requirements["originator"] == expected
    assert requirements["agent_identity"] == (
        "agent:aifactory/k1" if principal.get("api_key_id") else None
    )


@pytest.mark.parametrize(
    ("author", "expected"), [("octocat", "github:octocat"), (None, None)]
)
async def test_github_import_records_author(tmp_path, monkeypatch, author, expected):
    """POST /api/projects/{id}/github/import: gh read the author from GitHub."""
    (tmp_path / ".aifactory" / "specs").mkdir(parents=True)
    issue = {"number": 7, "title": "Issue", "body": "", "state": "open", "labels": []}
    if author:
        issue["author"] = {"login": author}
    monkeypatch.setattr(github, "_resolve_project_path", lambda _pid: tmp_path)
    monkeypatch.setattr(
        github,
        "run_gh_command",
        lambda *_a, **_k: {"success": True, "output": json.dumps(issue)},
    )
    result = await github.import_github_issues(
        "p", ImportIssuesRequest(issueNumbers=[7])
    )
    spec_id = result["data"]["issues"][0]["specId"]
    assert _requirements(tmp_path, spec_id)["originator"] == expected
