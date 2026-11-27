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


def test_read_correlation_key_returns_contract_key(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _make_spec(project, "001-trusted", contract_key="pf-337-x")
    service = AgentService()

    assert service._read_correlation_key(project, "001-trusted") == "pf-337-x"


def test_read_correlation_key_digits_contract_falls_back_to_provenance_issue(
    tmp_path: Path,
) -> None:
    project = tmp_path / "proj"
    _make_spec(
        project,
        "001-trusted",
        requirements={"title": "Task", "provenance": {"issue_number": 5}},
        contract_key="42",
    )
    service = AgentService()

    assert service._read_correlation_key(project, "001-trusted") == "5"


@pytest.mark.asyncio
async def test_durable_admission_passes_contract_key_to_admit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "proj"
    _make_spec(project, "001-trusted", contract_key="pf-337-x")

    service = AgentService()
    service._store_enabled = True
    admitted: dict[str, Any] = {}

    class _FakeStore:
        async def admit(
            self,
            task_id: str,
            _spawn_args: Any,
            _cap: int,
            *,
            correlation_key: str | None = None,
        ) -> str:
            admitted["task_id"] = task_id
            admitted["correlation_key"] = correlation_key
            return "queued"

    monkeypatch.setattr(service, "_store", lambda: _FakeStore())
    monkeypatch.setattr(
        service,
        "_mark_task_queued",
        lambda *_args, **_kwargs: _noop_async(),
    )

    result = await service.start_task_execution(
        task_id="p1:001-trusted",
        project_path=project,
        spec_id="001-trusted",
    )
    assert result is None
    assert admitted == {
        "task_id": "p1:001-trusted",
        "correlation_key": "pf-337-x",
    }


async def _noop_async() -> None:
    return None
