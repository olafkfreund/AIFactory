"""Tests for routes.merger -- authorization scoping (#4) and the event-loop
fix (#5). See services/merger.py for the sweep logic itself.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

_WS = Path(__file__).resolve().parents[1]
if str(_WS) not in sys.path:
    sys.path.insert(0, str(_WS))

from server.routes import merger as merger_routes  # noqa: E402
from server.routes.project_authz import _roles_at_or_above  # noqa: E402


class _Req:
    """Minimal stand-in for a FastAPI Request (headers mapping + empty state)."""

    def __init__(self, headers: dict[str, str] | None = None):
        self.headers = headers or {}
        self.state = type("S", (), {})()


def test_report_merger_restricts_to_the_callers_visible_projects(monkeypatch):
    """Finding #4: a non-service caller must only trigger a scan of the
    projects owned by an org they belong to, not every registered project."""
    monkeypatch.setattr(
        merger_routes,
        "load_projects",
        lambda: {"p1": {"org_id": "org-a"}, "p2": {"org_id": "org-b"}},
    )
    captured = {}

    def fake_sweep(*, dry_run, project_ids, **_kwargs):
        captured["project_ids"] = list(project_ids)
        return {"dry_run": dry_run, "results": [], "counts": {}}

    monkeypatch.setattr(merger_routes, "sweep", fake_sweep)
    with patch.object(
        merger_routes, "accessible_org_ids", new=AsyncMock(return_value={"org-a"})
    ):
        asyncio.run(merger_routes.report_merger(request=_Req(), db=None))
    assert captured["project_ids"] == ["p1"]


def test_report_merger_service_principal_sees_every_project(monkeypatch):
    """``accessible_org_ids`` returning None (service principal / local UI /
    auth-disabled) is the one case the fleet-wide scan is intended for."""
    monkeypatch.setattr(
        merger_routes,
        "load_projects",
        lambda: {"p1": {"org_id": "org-a"}, "p2": {"org_id": "org-b"}},
    )
    captured = {}

    def fake_sweep(*, dry_run, project_ids, **_kwargs):
        captured["project_ids"] = sorted(project_ids)
        return {"dry_run": dry_run, "results": [], "counts": {}}

    monkeypatch.setattr(merger_routes, "sweep", fake_sweep)
    with patch.object(
        merger_routes, "accessible_org_ids", new=AsyncMock(return_value=None)
    ):
        asyncio.run(merger_routes.report_merger(request=_Req(), db=None))
    assert captured["project_ids"] == ["p1", "p2"]


def test_run_merger_does_not_block_the_event_loop(monkeypatch):
    """Finding #5: sweep shells out to git/gh per spec, so it must run off
    the event loop. A concurrently-scheduled coroutine must be able to finish
    while a slow (here: sleeping) sweep is still running in its thread."""
    monkeypatch.setattr(merger_routes, "load_projects", lambda: {})
    order: list[str] = []

    # **_ absorbs project_ids: the stub must ACCEPT the route's real kwargs
    # without asserting on them, and naming an unused one trips ARG001.
    def blocking_sweep(*, dry_run, **_):
        order.append("sweep-start")
        time.sleep(0.05)
        order.append("sweep-end")
        return {"dry_run": dry_run, "results": [], "counts": {}}

    monkeypatch.setattr(merger_routes, "sweep", blocking_sweep)

    async def other_coro() -> None:
        order.append("other-start")
        await asyncio.sleep(0)
        order.append("other-end")

    async def run_both() -> None:
        with patch.object(
            merger_routes, "accessible_org_ids", new=AsyncMock(return_value=None)
        ):
            await asyncio.gather(
                merger_routes.run_merger(request=_Req(), dry_run=True, db=None),
                other_coro(),
            )

    asyncio.run(run_both())
    assert order.index("other-end") < order.index("sweep-end"), (
        "the event loop was blocked for the whole sweep -- other_coro should "
        "have finished while sweep was still running in its worker thread"
    )


# ── #1554 finding 1: tenant scope wired through to sweep ────────────────────


def test_report_merger_passes_resolved_tenant_when_multi_tenant_on(monkeypatch):
    monkeypatch.setenv("AIFACTORY_MULTI_TENANT", "true")
    monkeypatch.setattr(merger_routes, "load_projects", lambda: {})
    captured = {}

    def fake_sweep(*, dry_run, tenant=None, **_kwargs):
        captured["tenant"] = tenant
        return {"dry_run": dry_run, "results": [], "counts": {}}

    monkeypatch.setattr(merger_routes, "sweep", fake_sweep)
    with patch.object(
        merger_routes, "accessible_org_ids", new=AsyncMock(return_value=None)
    ):
        asyncio.run(
            merger_routes.report_merger(request=_Req({"X-Tenant-Id": "acme"}), db=None)
        )
    assert captured["tenant"] == "acme"


def test_report_merger_tenant_is_none_when_multi_tenant_off(monkeypatch):
    monkeypatch.delenv("AIFACTORY_MULTI_TENANT", raising=False)
    monkeypatch.setattr(merger_routes, "load_projects", lambda: {})
    captured = {}

    def fake_sweep(*, dry_run, tenant=None, **_kwargs):
        captured["tenant"] = tenant
        return {"dry_run": dry_run, "results": [], "counts": {}}

    monkeypatch.setattr(merger_routes, "sweep", fake_sweep)
    with patch.object(
        merger_routes, "accessible_org_ids", new=AsyncMock(return_value=None)
    ):
        asyncio.run(
            merger_routes.report_merger(request=_Req({"X-Tenant-Id": "acme"}), db=None)
        )
    assert captured["tenant"] is None


# ── #1554 finding 2: write path requires more than viewer membership ───────


def test_run_merger_write_path_requires_member_role(monkeypatch):
    """dry_run=false pushes branches and opens PRs -- the same write
    routes/pr.py gates with require_task_access("member"), not the bare
    org-membership check a read-only report uses."""
    monkeypatch.setattr(merger_routes, "load_projects", lambda: {})
    monkeypatch.setattr(
        merger_routes,
        "sweep",
        lambda **_: {"dry_run": False, "results": [], "counts": {}},
    )
    mock = AsyncMock(return_value=set())
    with patch.object(merger_routes, "accessible_org_ids", new=mock):
        asyncio.run(merger_routes.run_merger(request=_Req(), dry_run=False, db=None))
    mock.assert_awaited_once()
    assert mock.await_args is not None
    assert mock.await_args.args[2] == "member"


def test_run_merger_dry_run_stays_at_viewer_role(monkeypatch):
    """A POST with dry_run=true opens nothing -- same level as the GET report."""
    monkeypatch.setattr(merger_routes, "load_projects", lambda: {})
    monkeypatch.setattr(
        merger_routes,
        "sweep",
        lambda **_: {"dry_run": True, "results": [], "counts": {}},
    )
    mock = AsyncMock(return_value=set())
    with patch.object(merger_routes, "accessible_org_ids", new=mock):
        asyncio.run(merger_routes.run_merger(request=_Req(), dry_run=True, db=None))
    mock.assert_awaited_once()
    assert mock.await_args is not None
    assert mock.await_args.args[2] == "viewer"


def test_report_merger_stays_at_viewer_role(monkeypatch):
    monkeypatch.setattr(merger_routes, "load_projects", lambda: {})
    monkeypatch.setattr(
        merger_routes,
        "sweep",
        lambda **_: {"dry_run": True, "results": [], "counts": {}},
    )
    mock = AsyncMock(return_value=set())
    with patch.object(merger_routes, "accessible_org_ids", new=mock):
        asyncio.run(merger_routes.report_merger(request=_Req(), db=None))
    mock.assert_awaited_once()
    assert mock.await_args is not None
    assert mock.await_args.args[2] == "viewer"


# ── _roles_at_or_above (pure helper backing finding 2) ──────────────────────


def test_roles_at_or_above_member_excludes_viewer():
    roles = _roles_at_or_above("member")
    assert "viewer" not in roles
    assert {"member", "admin", "owner"} <= set(roles)


def test_roles_at_or_above_viewer_includes_everyone():
    roles = _roles_at_or_above("viewer")
    assert {"viewer", "member", "admin", "owner"} <= set(roles)
