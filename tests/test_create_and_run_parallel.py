"""Tests for create-and-run parallel persistence (#392).

The #376 wave executor reads parallel/workers from task_metadata.json via
AgentService._read_parallel_opts on the spec→plan→build auto-continue. Before
this fix, create-and-run never wrote task_metadata.json, so builds ran serial
(workers.max=1) even with parallel:true. These tests drive the real
``create_and_run_task`` route (spec creation stubbed) and assert the execution
options are persisted where the build handoff reads them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server.routes import execution as exec_mod  # noqa: E402
from server.routes.execution import CreateAndRunRequest  # noqa: E402
from server.services.agent_service import AgentService  # noqa: E402


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


def _task_metadata_for(result: dict, project: Path) -> Path:
    spec_id = result["task_id"].split(":", 1)[1]
    return project / ".aifactory" / "specs" / spec_id / "task_metadata.json"


async def test_parallel_and_workers_persisted(tmp_path, monkeypatch):
    project = _setup(tmp_path, monkeypatch)
    req = CreateAndRunRequest(parallel=True, workers=4)
    result = await exec_mod.create_and_run_task(
        project_id="p", title="Gateway", description="build it", request=req
    )
    meta_file = _task_metadata_for(result, project)
    assert meta_file.exists()
    meta = json.loads(meta_file.read_text())
    assert meta["parallel"] is True
    assert meta["workers"] == 4


async def test_read_parallel_opts_round_trip(tmp_path, monkeypatch):
    # The build handoff must read back exactly what create-and-run wrote.
    project = _setup(tmp_path, monkeypatch)
    req = CreateAndRunRequest(parallel=True, workers=3)
    result = await exec_mod.create_and_run_task(
        project_id="p", title="Gateway", description="build it", request=req
    )
    spec_id = result["task_id"].split(":", 1)[1]

    svc = AgentService.__new__(AgentService)  # avoid heavy __init__
    parallel, workers = svc._read_parallel_opts(project, spec_id)
    assert parallel is True
    assert workers == 3


async def test_mode_and_model_persisted(tmp_path, monkeypatch):
    project = _setup(tmp_path, monkeypatch)
    req = CreateAndRunRequest(parallel=True, workers=2, mode="quick", model="opus")
    result = await exec_mod.create_and_run_task(
        project_id="p", title="T", description="D", request=req
    )
    meta = json.loads(_task_metadata_for(result, project).read_text())
    assert meta["mode"] == "quick"
    assert meta["model"] == "opus"
    assert meta["parallel"] is True and meta["workers"] == 2


async def test_no_options_writes_no_metadata(tmp_path, monkeypatch):
    # Defaults: nothing requested → no task_metadata.json (no behavior change).
    project = _setup(tmp_path, monkeypatch)
    result = await exec_mod.create_and_run_task(
        project_id="p", title="T", description="D", request=CreateAndRunRequest()
    )
    assert not _task_metadata_for(result, project).exists()


async def test_parallel_false_is_persisted(tmp_path, monkeypatch):
    # Explicit parallel=false must be recorded (distinct from "unset").
    project = _setup(tmp_path, monkeypatch)
    req = CreateAndRunRequest(parallel=False)
    result = await exec_mod.create_and_run_task(
        project_id="p", title="T", description="D", request=req
    )
    meta = json.loads(_task_metadata_for(result, project).read_text())
    assert meta["parallel"] is False
