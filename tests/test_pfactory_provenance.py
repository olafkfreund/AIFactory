"""Tests for PFactory provenance persistence on create-and-run (#332).

Drives the real ``create_and_run_task`` route with spec creation stubbed, and
asserts that upstream provenance (session_id / issue#) lands in the spec's
``requirements.json`` — and that omitting it leaves the spec untouched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server.routes import execution as exec_mod  # noqa: E402
from server.routes.execution import (  # noqa: E402
    CreateAndRunRequest,
    TaskProvenance,
)


class _StubAgentService:
    async def start_spec_creation(self, **kwargs):
        return None


def _setup(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    (project / ".aifactory" / "specs").mkdir(parents=True)
    monkeypatch.setattr(
        exec_mod, "load_projects", lambda: {"p": {"path": str(project)}}
    )
    monkeypatch.setattr(exec_mod, "get_agent_service", lambda: _StubAgentService())
    return project


def _requirements_for(result: dict, project: Path) -> dict:
    spec_id = result["task_id"].split(":", 1)[1]
    return json.loads(
        (project / ".aifactory" / "specs" / spec_id / "requirements.json").read_text()
    )


async def test_provenance_is_persisted(tmp_path, monkeypatch):
    project = _setup(tmp_path, monkeypatch)
    req = CreateAndRunRequest(
        provenance=TaskProvenance(
            session_id="sess-abc", issue_number=42, source="pfactory", repo="o/r"
        )
    )
    result = await exec_mod.create_and_run_task(
        project_id="p", title="Add health", description="do it", request=req
    )
    prov = _requirements_for(result, project)["provenance"]
    assert prov == {
        "session_id": "sess-abc",
        "issue_number": 42,
        "repo": "o/r",
        "source": "pfactory",
    }


async def test_partial_provenance_drops_none_fields(tmp_path, monkeypatch):
    project = _setup(tmp_path, monkeypatch)
    req = CreateAndRunRequest(provenance=TaskProvenance(session_id="s", issue_number=7))
    result = await exec_mod.create_and_run_task(
        project_id="p", title="T", description="D", request=req
    )
    prov = _requirements_for(result, project)["provenance"]
    assert prov == {"session_id": "s", "issue_number": 7}  # repo/source omitted


async def test_no_provenance_leaves_requirements_clean(tmp_path, monkeypatch):
    project = _setup(tmp_path, monkeypatch)
    result = await exec_mod.create_and_run_task(
        project_id="p", title="T", description="D", request=CreateAndRunRequest()
    )
    assert "provenance" not in _requirements_for(result, project)


async def test_empty_provenance_object_persists_nothing(tmp_path, monkeypatch):
    project = _setup(tmp_path, monkeypatch)
    req = CreateAndRunRequest(provenance=TaskProvenance())  # all None
    result = await exec_mod.create_and_run_task(
        project_id="p", title="T", description="D", request=req
    )
    assert "provenance" not in _requirements_for(result, project)
