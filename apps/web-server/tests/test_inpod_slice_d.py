"""Slice D of #1932 / #2095: remaining in-pod agent paths refuse under FORBID."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server.routes import changelog, execution  # noqa: E402
from server.services import (  # noqa: E402
    changelog_service,
    conflict_service,
    insights_service,
)


@pytest.fixture
def forbid(monkeypatch):
    monkeypatch.setenv("AIFACTORY_FORBID_INPOD_AGENTS", "1")
    monkeypatch.delenv("AIFACTORY_LOCAL_DEV", raising=False)


def _boom(*_a, **_k):
    raise AssertionError("agent must not be spawned")


@pytest.mark.usefixtures("forbid")
def test_conflict_merge_refused(monkeypatch):
    svc = conflict_service.ConflictService.__new__(conflict_service.ConflictService)
    monkeypatch.setattr(svc, "_ensure_backend_path", _boom)
    for res in (
        svc._ai_merge_three_way_sync("f", "b", "l", "t", "L", "T"),
        svc._resolve_conflict_markers_sync("f", "<<<<<<< x\n"),
    ):
        assert res["success"] is False and "disabled" in res["error"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("forbid")
async def test_changelog_refused(monkeypatch, tmp_path):
    svc = changelog_service.ChangelogService()
    errors = []

    async def _emit(_pid, msg):
        errors.append(msg)

    monkeypatch.setattr(svc, "_emit_error", _emit)
    monkeypatch.setattr(changelog_service.asyncio, "create_subprocess_exec", _boom)
    assert await svc.start_generation("p", tmp_path, {}) is False
    assert errors and "disabled" in errors[0]


@pytest.mark.asyncio
@pytest.mark.usefixtures("forbid")
async def test_insights_chat_refused(monkeypatch, tmp_path):
    sent = []

    async def _bcast(name, payload):
        sent.append((name, payload))

    monkeypatch.setattr(insights_service, "broadcast_event", _bcast)
    monkeypatch.setattr(insights_service, "get_provider", _boom)
    await insights_service.InsightsService().send_message(tmp_path, "p", "hi")
    assert sent[0][1]["type"] == "error" and "disabled" in sent[0][1]["error"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("forbid")
async def test_generate_task_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(changelog, "_get_project_path", lambda _p: tmp_path)
    req = changelog.GenerateTaskRequest(modelConfig=None)
    res = await changelog.generate_task_from_chat(projectId="p", request=req)
    assert res.status_code >= 400 and b"disabled" in res.body


@pytest.fixture(autouse=True)
def _no_triage_gate(monkeypatch):
    class _S:
        REQUIRE_CORRECTION_TRIAGE = False

    monkeypatch.setattr(execution, "get_settings", _S)


def _correction_req(confirm=True):
    return execution.ApplyCorrectionRequest(fix_request_md="fix", confirm=confirm)


@pytest.mark.asyncio
@pytest.mark.usefixtures("forbid")
async def test_apply_correction_refused_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        execution, "_resolve_task", lambda _t: ("p", "s", tmp_path, tmp_path)
    )
    monkeypatch.setattr(execution, "apply_correction", _boom)
    with pytest.raises(HTTPException) as exc:
        await execution.apply_task_correction("p:s", _correction_req(), _access={})
    assert exc.value.status_code == 409 and "disabled" in exc.value.detail
