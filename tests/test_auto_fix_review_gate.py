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


def test_start_route_requires_member_role(monkeypatch):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from server.database.engine import get_db
    from server.routes import auto_fix, project_authz
    from server.services import auto_fix_service

    roles: list[str] = []

    async def deny(user, project_id, db, minimum_role="viewer"):
        roles.append(minimum_role)
        raise HTTPException(status_code=403, detail="forbidden")

    monkeypatch.setattr(project_authz, "_auth_disabled", lambda: False)
    monkeypatch.setattr(project_authz, "authorize_project_for_user", deny)
    start = AsyncMock()
    monkeypatch.setattr(auto_fix_service, "start_auto_fix", start)

    app = FastAPI()
    app.include_router(auto_fix.router, prefix="/api/projects")
    app.dependency_overrides[get_db] = lambda: None
    r = TestClient(app).post("/api/projects/p1/auto-fix/7/start")

    assert r.status_code == 403
    assert roles == ["member"]
    start.assert_not_called()


def test_write_spec_dir_require_review_metadata(tmp_path):
    from server.services.auto_fix_service import _write_spec_dir

    issue = {"number": 7, "title": "x", "body": "b", "state": "open", "url": "u"}

    def meta(name, **kw):
        sid = _write_spec_dir(tmp_path / name, issue, "github", **kw)
        req = tmp_path / name / ".aifactory" / "specs" / sid / "requirements.json"
        return json.loads(req.read_text())

    assert meta("a", require_review=True)["metadata"] == {
        "requireReviewBeforeCoding": True
    }
    assert "metadata" not in meta("b")
    both = meta("c", require_review=True, delegate_by_default=True)["metadata"]
    assert both["requireReviewBeforeCoding"] is True
    assert both["enableDelegation"] is True


@pytest.mark.asyncio
async def test_new_spec_with_require_human_approval_is_gated(tmp_path):
    from server.services import auto_fix_service

    with patch.object(
        auto_fix_service,
        "get_config",
        return_value={"requireHumanApproval": True},
    ):
        await _start(tmp_path)
    (spec,) = (tmp_path / ".aifactory" / "specs").iterdir()
    meta = json.loads((spec / "task_metadata.json").read_text())
    assert meta["requireReviewBeforeCoding"] is True


def _config_client(monkeypatch, stored, allow=True):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from server.database.engine import get_db
    from server.routes import auto_fix, project_authz
    from server.services import auto_fix_service

    roles: list[str] = []

    async def authz(user, project_id, db, minimum_role="viewer"):
        roles.append(minimum_role)
        if not allow:
            raise HTTPException(status_code=403, detail="forbidden")

    saved: dict = {}
    monkeypatch.setattr(project_authz, "_auth_disabled", lambda: False)
    monkeypatch.setattr(project_authz, "authorize_project_for_user", authz)
    monkeypatch.setattr(auto_fix_service, "get_config", lambda pid: dict(stored))
    monkeypatch.setattr(
        auto_fix_service, "save_config", lambda pid, cfg: saved.update(cfg) or True
    )
    app = FastAPI()
    app.include_router(auto_fix.router, prefix="/api/projects")
    app.dependency_overrides[get_db] = lambda: None
    return TestClient(app), roles, saved


def test_get_config_masks_bot_token(monkeypatch):
    client, _, _ = _config_client(monkeypatch, {"botToken": "ghp_secret"})
    body = client.get("/api/projects/p1/auto-fix/config").json()
    assert body["botToken"] == "********"


@pytest.mark.parametrize("sent", [{"botToken": "********"}, {}])
def test_put_config_keeps_stored_token_for_mask_or_omitted(monkeypatch, sent):
    client, roles, saved = _config_client(monkeypatch, {"botToken": "ghp_secret"})
    r = client.put("/api/projects/p1/auto-fix/config", json={"enabled": True, **sent})
    assert r.status_code == 200
    assert saved["botToken"] == "ghp_secret"
    assert roles == ["admin"]


def test_put_config_replaces_token_when_new_one_sent(monkeypatch):
    client, _, saved = _config_client(monkeypatch, {"botToken": "ghp_old"})
    client.put("/api/projects/p1/auto-fix/config", json={"botToken": "ghp_new"})
    assert saved["botToken"] == "ghp_new"


def test_put_config_denied_below_admin(monkeypatch):
    client, roles, saved = _config_client(monkeypatch, {}, allow=False)
    r = client.put("/api/projects/p1/auto-fix/config", json={"enabled": True})
    assert r.status_code == 403
    assert roles == ["admin"]
    assert not saved
