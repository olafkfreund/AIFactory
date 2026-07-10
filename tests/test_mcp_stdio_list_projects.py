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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))
sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from server.mcp_stdio.router import proxy_list_projects  # noqa: E402


def test_proxy_list_projects_returns_all_unfiltered(monkeypatch):
    import server.routes.projects as projects_mod

    fake = {
        "p1": {"name": "alpha", "path": "/a", "org_id": "org-1"},
        "p2": {"name": "beta", "path": "/b", "org_id": "org-2"},
    }
    monkeypatch.setattr(projects_mod, "load_projects", lambda: fake)
    monkeypatch.setattr(
        projects_mod,
        "project_to_response",
        lambda pid, pdata: {"id": pid, "name": pdata["name"]},
    )

    # `_` is the scope dependency; call directly bypassing FastAPI DI.
    result = asyncio.run(proxy_list_projects(None))

    ids = sorted(p["id"] for p in result)
    assert ids == ["p1", "p2"], f"expected all projects unfiltered, got {result}"
