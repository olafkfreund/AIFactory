"""``start_task_execution`` inherits a spec's persisted parallel/workers (#376).

The audited gap: the parallel harness (agents/parallel_runner) and the
task_metadata round-trip both shipped, but ``start_task_execution`` passed its
``parallel`` argument straight through. Only the auto-continue monitor read
task_metadata itself, so every OTHER entry point -- intake from-issue, /start,
plan-approval, auto-fix, delegation -- silently ran serial no matter what the
spec said.

These tests pin the fallback at that single choke point: absent an explicit
argument, the spec's own setting wins; an explicit argument still overrides it.
``_spawn_task_execution`` is stubbed, so no subprocess and no real build.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server.services.agent_service import AgentService  # noqa: E402


def _write_metadata(project_path: Path, spec_id: str, meta: dict) -> None:
    """Persist a spec's task_metadata.json exactly where the service reads it."""
    spec_dir = project_path / ".aifactory" / "specs" / spec_id
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "task_metadata.json").write_text(json.dumps(meta))


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> tuple[AgentService, dict]:
    """AgentService whose spawn records the parallel/workers it was handed."""
    svc = AgentService()
    svc.settings.MAX_CONCURRENT_TASKS = 0  # unlimited: never exercise queueing

    spawned: dict = {}

    async def fake_spawn(**kwargs: Any) -> object:
        spawned.update(kwargs)
        return object()

    monkeypatch.setattr(svc, "_spawn_task_execution", fake_spawn)
    # Force the in-memory path; the durable store is not under test here.
    monkeypatch.setattr(svc, "_store_enabled", False, raising=False)
    return svc, spawned


async def _start(
    svc: AgentService, tmp_path: Path, spec_id: str = "spec-1", **kwargs: Any
) -> None:
    await svc.start_task_execution(
        task_id=f"p:{spec_id}", project_path=tmp_path, spec_id=spec_id, **kwargs
    )


async def test_parallel_inherited_from_task_metadata(
    service: tuple[AgentService, dict], tmp_path: Path
) -> None:
    svc, spawned = service
    _write_metadata(tmp_path, "spec-1", {"parallel": True, "workers": 4})

    await _start(svc, tmp_path)  # caller passes nothing -- the intake case

    assert spawned["parallel"] is True
    assert spawned["workers"] == 4


async def test_explicit_argument_overrides_metadata(
    service: tuple[AgentService, dict], tmp_path: Path
) -> None:
    svc, spawned = service
    _write_metadata(tmp_path, "spec-1", {"parallel": True, "workers": 4})

    await _start(svc, tmp_path, parallel=False)

    assert spawned["parallel"] is False  # a deliberate serial start stays serial


async def test_explicit_workers_kept_while_parallel_inherited(
    service: tuple[AgentService, dict], tmp_path: Path
) -> None:
    svc, spawned = service
    _write_metadata(tmp_path, "spec-1", {"parallel": True, "workers": 4})

    await _start(svc, tmp_path, workers=2)

    assert spawned["parallel"] is True  # inherited
    assert spawned["workers"] == 2  # caller's cap wins


async def test_serial_metadata_stays_serial(
    service: tuple[AgentService, dict], tmp_path: Path
) -> None:
    svc, spawned = service
    _write_metadata(tmp_path, "spec-1", {"parallel": False})

    await _start(svc, tmp_path)

    assert spawned["parallel"] is False


async def test_missing_or_malformed_metadata_is_not_an_error(
    service: tuple[AgentService, dict], tmp_path: Path
) -> None:
    svc, spawned = service
    # No task_metadata.json at all -- the pre-#376 shape must still start.
    await _start(svc, tmp_path)
    assert spawned["parallel"] is None
    assert spawned["workers"] is None

    spec_dir = tmp_path / ".aifactory" / "specs" / "spec-2"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "task_metadata.json").write_text("{not json")

    await _start(svc, tmp_path, spec_id="spec-2")
    assert spawned["parallel"] is None
