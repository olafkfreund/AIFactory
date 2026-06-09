"""Regression: the stdio-MCP create-and-run proxy must pass CreateAndRunRequest.

`create_and_run_task` takes `CreateAndRunRequest` (a `StartTaskRequest` subclass
that adds `provenance`, #332) and reads `request.provenance`. The proxy passed a
bare `StartTaskRequest` → `AttributeError: 'StartTaskRequest' object has no
attribute 'provenance'` → HTTP 500, which broke the `/handover` write-path
(reproduced live). The fix passes CreateAndRunRequest.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))
sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

import importlib  # noqa: E402

r = importlib.import_module("server.mcp_stdio.router")  # the module, not the APIRouter


class _FakeRequest:
    def __init__(self, body: bytes):
        self._body = body

    async def body(self) -> bytes:
        return self._body

    @property
    def client(self):
        return None


def test_proxy_create_and_run_passes_provenance_capable_request(monkeypatch):
    captured = {}

    async def fake_create_and_run_task(project_id, title, description, request):
        captured["request"] = request
        return {"task_id": "t1"}

    async def _noop_audit(*a, **k):
        return None

    import server.routes.execution as ex

    monkeypatch.setattr(ex, "create_and_run_task", fake_create_and_run_task)
    monkeypatch.setattr(r, "_audit_mcp_write", _noop_audit)

    req = _FakeRequest(json.dumps({"model": "gemini-3.5-flash"}).encode())
    result = asyncio.run(
        r.proxy_create_and_run_task(req, "p1", "title", "desc", None)
    )

    assert result == {"task_id": "t1"}
    # The model passed must expose `provenance` (i.e. it's a CreateAndRunRequest).
    assert hasattr(captured["request"], "provenance"), "must pass CreateAndRunRequest"
    assert captured["request"].provenance is None
    assert captured["request"].model == "gemini-3.5-flash"
