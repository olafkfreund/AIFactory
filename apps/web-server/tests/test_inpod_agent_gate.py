"""Hard-disable of in-pod agents outside local dev (#1932)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server.services import clarification_service  # noqa: E402
from server.services.build_backend import inpod_agent_refusal  # noqa: E402


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("AIFACTORY_FORBID_INPOD_AGENTS", raising=False)
    monkeypatch.delenv("AIFACTORY_LOCAL_DEV", raising=False)
    assert inpod_agent_refusal() is None


def test_forbid_and_local_dev_escape(monkeypatch):
    monkeypatch.setenv("AIFACTORY_FORBID_INPOD_AGENTS", "true")
    monkeypatch.delenv("AIFACTORY_LOCAL_DEV", raising=False)
    assert "disabled" in (inpod_agent_refusal() or "")
    monkeypatch.setenv("AIFACTORY_LOCAL_DEV", "1")
    assert inpod_agent_refusal() is None


@pytest.mark.asyncio
async def test_clarification_skips_when_forbidden(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFACTORY_FORBID_INPOD_AGENTS", "1")
    monkeypatch.delenv("AIFACTORY_LOCAL_DEV", raising=False)

    async def _spawn(*_a, **_k):
        raise AssertionError("agent must not be spawned")

    monkeypatch.setattr(clarification_service.asyncio, "create_subprocess_exec", _spawn)
    result = await clarification_service.generate_clarification_questions(
        "t", "d", tmp_path
    )
    assert result["skip"] is True and result["questions"] == []
