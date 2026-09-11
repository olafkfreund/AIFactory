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


class _Req:
    """Minimal stand-in for a FastAPI Request (headers mapping + empty state)."""

    def __init__(self, headers: dict | None = None):
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

    def fake_sweep(*, dry_run, project_ids):
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

    def fake_sweep(*, dry_run, project_ids):
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
