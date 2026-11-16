"""#1972: worker sub-events carry the web server's project-scoped task id."""

from __future__ import annotations

import types
from pathlib import Path

import pytest
from agents import parallel_integration as pi


def _emit(tmp_path: Path):
    seen: dict = {}
    mod = types.SimpleNamespace(
        emit_worker_completion=lambda **kw: seen.update(kw),
        emit_worker_progress=lambda **kw: seen.update(kw),
    )
    spec = tmp_path / "003-x"
    spec.mkdir()
    return seen, mod, spec


@pytest.mark.parametrize(
    "env,expected", [("uuid-1:003-x", "uuid-1:003-x"), (None, "proj:003-x")]
)
def test_worker_events_use_env_task_id(tmp_path, monkeypatch, env, expected):
    seen, mod, spec = _emit(tmp_path)
    monkeypatch.setattr(pi, "_load_completion_emitter", lambda: mod)
    if env:
        monkeypatch.setenv("AIFACTORY_TASK_ID", env)
    else:
        monkeypatch.delenv("AIFACTORY_TASK_ID", raising=False)
    proj = tmp_path / "proj"
    pi.emit_worker_live_event(
        spec_dir=spec,
        project_dir=proj,
        worker_id="w",
        subtask_id=None,
        provider=None,
        model=None,
        agent_phase=None,
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.0,
        duration_ms=1,
    )
    assert seen["task_id"] == expected
    assert seen["project_id"] == expected.split(":")[0]
    seen.clear()
    pi.emit_worker_progress_heartbeat(
        spec_dir=spec,
        project_dir=proj,
        worker_id="w",
        subtask_id=None,
        provider=None,
        model=None,
        agent_phase=None,
        total_tokens=1,
        cost_usd=0.0,
        elapsed_ms=1,
    )
    assert seen["task_id"] == expected


def test_completion_emitter_loads_standalone(monkeypatch):
    """#2064: the file-path load must resolve completion.py's relative imports."""
    monkeypatch.setattr(pi, "_completion_mod", None)
    mod = pi._load_completion_emitter()
    assert mod is not None
    assert callable(mod.classify_billing_mode)
