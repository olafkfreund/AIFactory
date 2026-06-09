"""Regression: the stdio-MCP `/projects` proxy must forward request + db.

`routes.projects.list_projects` requires `(request, db)` since #319 (org-scoped
visibility). The proxy `proxy_list_projects` used to call it with no args →
`TypeError: list_projects() missing 1 required positional argument: 'request'`
→ HTTP 500, which broke the stdio-MCP / `/handover` project-lookup step
(reproduced live against the deployed instance). The fix injects and forwards
both.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))
sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from server.mcp_stdio.router import proxy_list_projects  # noqa: E402


def test_proxy_list_projects_forwards_request_and_db(monkeypatch):
    captured = {}

    async def fake_list_projects(request, db=None):
        captured["request"] = request
        captured["db"] = db
        return [{"id": "p1", "name": "proj"}]

    import server.routes.projects as projects_mod

    monkeypatch.setattr(projects_mod, "list_projects", fake_list_projects)

    sentinel_request = object()
    sentinel_db = object()
    # Call directly (bypassing FastAPI DI); `_` is the scope dependency.
    result = asyncio.run(proxy_list_projects(sentinel_request, sentinel_db, None))

    assert result == [{"id": "p1", "name": "proj"}]
    assert captured["request"] is sentinel_request, "request not forwarded"
    assert captured["db"] is sentinel_db, "db not forwarded"
