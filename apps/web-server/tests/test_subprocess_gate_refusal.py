"""Subprocess backend gate-refusal tests (#1932, #1984)."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from pathlib import Path
from typing import Any

import pytest
from server.error_ref import InputRejectedError
from server.services.agent_service import AgentService
from server.services.build_backend import (
    BuildBackendUnavailableError,
    GatedTaskRefusedError,
    subprocess_gate_refusal,
)


def _write_meta(spec_dir: Path, content: Any) -> Path:
    spec_dir.mkdir(parents=True, exist_ok=True)
    text = content if isinstance(content, str) else json.dumps(content)
    (spec_dir / "task_metadata.json").write_text(text, encoding="utf-8")
    return spec_dir


def test_gated_task_refused_error_subclasses_input_rejected_not_unavailable() -> None:
    err = GatedTaskRefusedError("msg")
    assert isinstance(err, InputRejectedError)
    assert not isinstance(err, BuildBackendUnavailableError)


@pytest.mark.parametrize(
    "meta",
    [
        {"requireReviewBeforeCoding": True},
        {"mode": "quick"},
        {"reviewTier": "blocking"},
    ],
    ids=["require_review", "mode_quick", "tier_blocking"],
)
def test_subprocess_refuses_each_metadata_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, meta: dict[str, Any]
) -> None:
    monkeypatch.delenv("AIFACTORY_BUILD_BACKEND", raising=False)
    spec_dir = _write_meta(tmp_path / "spec", meta)
    problem = subprocess_gate_refusal(spec_dir)
    assert problem is not None
    assert "this task needs the kubejob backend" in problem


def test_subprocess_refuses_quick_mode_arg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIFACTORY_BUILD_BACKEND", "subprocess")
    problem = subprocess_gate_refusal(tmp_path / "missing", mode="quick")
    assert problem is not None
    assert "this task needs the kubejob backend" in problem


@pytest.mark.parametrize("raw", ["{not-json", "[]", '"str"', "123"])
def test_subprocess_refuses_unparseable_or_non_dict_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.delenv("AIFACTORY_BUILD_BACKEND", raising=False)
    spec_dir = _write_meta(tmp_path / "spec", raw)
    problem = subprocess_gate_refusal(spec_dir)
    assert problem is not None
    assert "this task needs the kubejob backend" in problem


def test_subprocess_allows_missing_metadata_and_none_spec_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AIFACTORY_BUILD_BACKEND", raising=False)
    assert subprocess_gate_refusal(None) is None
    assert subprocess_gate_refusal(tmp_path / "missing") is None


def test_subprocess_allows_ungated_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIFACTORY_BUILD_BACKEND", "subprocess")
    spec_dir = _write_meta(
        tmp_path / "spec",
        {
            "requireReviewBeforeCoding": False,
            "mode": "full",
            "reviewTier": "advisory",
        },
    )
    assert subprocess_gate_refusal(spec_dir, mode="full") is None


def test_kubejob_env_returns_none_even_for_gated_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIFACTORY_BUILD_BACKEND", "kubejob")
    spec_dir = _write_meta(tmp_path / "spec", {"requireReviewBeforeCoding": True})
    assert subprocess_gate_refusal(spec_dir, mode="quick") is None
    assert subprocess_gate_refusal(_write_meta(tmp_path / "bad", "{bad")) is None


@pytest.mark.asyncio
async def test_start_task_execution_refuses_before_spawn_or_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AIFACTORY_BUILD_BACKEND", raising=False)
    _write_meta(
        tmp_path / ".aifactory" / "specs" / "001",
        {"requireReviewBeforeCoding": True},
    )
    svc = AgentService.__new__(AgentService)
    svc.running_tasks = {}
    svc._task_queue = deque()
    svc._store_enabled = False

    spawned: list[str] = []

    async def boom(**_kw: Any) -> Any:
        spawned.append("called")

    monkeypatch.setattr(svc, "_start_build_unit", boom)

    with pytest.raises(
        GatedTaskRefusedError, match="this task needs the kubejob backend"
    ):
        await svc.start_task_execution(
            task_id="p:001", project_path=tmp_path, spec_id="001"
        )
    assert spawned == []
    assert list(svc._task_queue) == []


@pytest.mark.asyncio
async def test_start_spec_creation_refuses_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AIFACTORY_BUILD_BACKEND", raising=False)
    _write_meta(
        tmp_path / ".aifactory" / "specs" / "001",
        {"reviewTier": "blocking"},
    )
    svc = AgentService.__new__(AgentService)
    svc.running_tasks = {}
    svc._store_enabled = False

    spawned: list[Any] = []

    async def boom(*a: Any, **_kw: Any) -> Any:
        spawned.append(a)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)

    with pytest.raises(
        GatedTaskRefusedError, match="this task needs the kubejob backend"
    ):
        await svc.start_spec_creation(
            task_id="p:001",
            project_path=tmp_path,
            title="t",
            description="d",
        )
    assert spawned == []
