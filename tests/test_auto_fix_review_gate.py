"""#1925 — manual auto-fix start must not force-run a reused (possibly gated) spec."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))


async def _start(tmp_path):
    from server.services import auto_fix_service

    projects = {"p1": {"path": str(tmp_path), "settings": {"gitProvider": "github"}}}
    svc = MagicMock()
    svc.start_task_execution = AsyncMock()
    provider = MagicMock()
    provider.fetch_issue = AsyncMock(
        return_value=SimpleNamespace(
            number=7, title="x", body="b", state="open", labels=[], url="u"
        )
    )
    with (
        patch("server.project_registry.load_projects", return_value=projects),
        patch("server.services.agent_service.get_agent_service", return_value=svc),
        patch.object(auto_fix_service, "_provider_for", return_value=provider),
        patch.object(auto_fix_service, "_upsert_queue_item"),
        patch("server.websockets.events.broadcast_event", new=AsyncMock()),
    ):
        await auto_fix_service.start_auto_fix("p1", 7)
    return svc.start_task_execution


@pytest.mark.asyncio
async def test_reused_spec_is_not_forced(tmp_path):
    (tmp_path / ".aifactory" / "specs" / "001-gh7-x").mkdir(parents=True)
    start = await _start(tmp_path)
    assert start.call_args.kwargs["force"] is False


@pytest.mark.asyncio
async def test_new_spec_is_forced(tmp_path):
    start = await _start(tmp_path)
    assert start.call_args.kwargs["force"] is True
    assert any((tmp_path / ".aifactory" / "specs").iterdir())


def test_should_pass_force_follows_review_flag(tmp_path):
    from server.services.task_phase import should_pass_force

    (tmp_path / "requirements.json").write_text(
        json.dumps({"metadata": {"requireReviewBeforeCoding": True}})
    )
    assert should_pass_force(tmp_path, False) is False
    # fresh dir: the first call syncs the flag into task_metadata.json
    other = tmp_path / "other"
    other.mkdir()
    (other / "requirements.json").write_text(json.dumps({}))
    assert should_pass_force(other, False) is True
