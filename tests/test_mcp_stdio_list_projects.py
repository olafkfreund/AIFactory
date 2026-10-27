"""Regression: the stdio-MCP `/projects` proxy must return ALL projects.

History:
- It called `list_projects()` with no args → `TypeError: missing 'request'`
  → HTTP 500 (broke `/handover`'s project lookup). [#488]
- The first fix forwarded `request`+`db` to the org-scoped `list_projects`,
  which fixed the 500 but returned an **empty** list for the acw/legacy
  principal (no accessible orgs) — handover still couldn't find the project.
- Correct fix: the M2M proxy returns the unfiltered project list directly
  (`load_projects()`), matching the original pre-#319 behaviour.
"""

import asyncio
import importlib
import sys
from pathlib import Path

from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))
sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

stdio_router = importlib.import_module("server.mcp_stdio.router")
projects_mod = importlib.import_module("server.routes.projects")
proxy_list_projects = stdio_router.proxy_list_projects


def test_proxy_list_projects_returns_all_unfiltered(monkeypatch):
    fake = {
        "p1": {"name": "alpha", "path": "/a", "org_id": "org-1"},
        "p2": {"name": "beta", "path": "/b", "org_id": "org-2"},
    }
    monkeypatch.setattr(projects_mod, "load_projects", lambda: fake)
    monkeypatch.setattr(stdio_router, "load_projects", lambda: fake)
    monkeypatch.setattr(
        projects_mod,
        "project_to_response",
        lambda pid, pdata: {"id": pid, "name": pdata["name"]},
    )

    # Direct call bypasses the scope dependency; use the legacy service principal.
    request = Request({"type": "http"})
    request.state.user = {"is_service": True}
    result = asyncio.run(proxy_list_projects(request, None, None))

    ids = sorted(p["id"] for p in result)
    assert ids == ["p1", "p2"], f"expected all projects unfiltered, got {result}"
