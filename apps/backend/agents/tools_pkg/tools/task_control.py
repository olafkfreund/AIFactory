"""Task-control MCP tools — Epic #50 M1.

8 tools that let a Claude Code session in the AIFactory repo drive
AIFactory tasks via natural-language MCP calls:

  Read tools
  - task_list         — list tasks (filter by status/project, default limit 50)
  - task_running      — list currently running tasks
  - task_get          — full task detail (heavy fields truncated)
  - task_status       — execution state (phase, current subtask, progress)
  - task_get_logs     — last N log lines (default 100, cap 500)

  Write tools (each writes an AuditLog row server-side)
  - task_start        — POST /api/tasks/{id}/start
  - task_stop         — POST /api/tasks/{id}/stop
  - task_approve_plan — POST /api/tasks/{id}/approve-plan

Trust model (per Epic #50): tools have full admin access via the legacy
bearer token at ``~/.aifactory/.token``. Per-user MCP tokens land in the
v1.1 RBAC work — until then, anyone with the token has admin.

Registered ONLY from the standalone MCP server
(``apps/backend/mcp_server/aifactory_server.py``), NOT from
``registry.create_all_tools`` — the in-process Claude Agent SDK shouldn't
be able to drive itself recursively.
"""

from __future__ import annotations

import json
from typing import Any

try:
    from claude_agent_sdk import tool

    SDK_TOOLS_AVAILABLE = True
except ImportError:
    SDK_TOOLS_AVAILABLE = False
    tool = None  # type: ignore[assignment]

from ..http_client import MCPHTTPError, request


def _format_error(exc: Exception) -> dict[str, Any]:
    """Wrap an MCPHTTPError (or other) as a content-block error response.

    MCP tools don't have a separate ``isError`` field in the simple SDK
    helper; we return ``content[]`` with a single text block prefixed
    with "Error:" so the LLM client renders it as a failure message and
    the operator sees the actionable guidance directly.
    """
    return {
        "content": [{"type": "text", "text": f"Error: {exc}"}],
        "isError": True,
    }


def _format_json(data: Any) -> dict[str, Any]:
    """Wrap a JSON-serializable payload as a content-block response."""
    return {
        "content": [{"type": "text", "text": json.dumps(data, indent=2, default=str)}]
    }


# Heavy fields stripped from task_get so the LLM context doesn't bloat.
# These remain available via direct REST if needed.
_HEAVY_FIELDS_TO_TRUNCATE = ("requirements_json", "implementation_plan_json", "context_json")
_HEAVY_FIELD_CAP = 2000


def _lean_task(task: dict) -> dict:
    """Strip / truncate heavy fields from a task detail payload."""
    lean = dict(task)
    for field in _HEAVY_FIELDS_TO_TRUNCATE:
        if field in lean and isinstance(lean[field], str) and len(lean[field]) > _HEAVY_FIELD_CAP:
            lean[field] = lean[field][:_HEAVY_FIELD_CAP] + "...[truncated]"
    return lean


def create_task_control_tools() -> list:
    """Create the 8 task-control tools.

    Returns a list of tool functions decorated with @tool — callers pass
    this to ``mcp.server.Server.tools`` via ``create_sdk_mcp_server``.
    """
    if not SDK_TOOLS_AVAILABLE:
        return []

    tools = []

    # ── Read tools ────────────────────────────────────────────────────

    @tool(
        "task_list",
        "List AIFactory tasks across all projects. Filter by status (e.g. 'running', "
        "'completed', 'failed') or project_id. Returns lean entries with id, title, "
        "status, project_id, created_at.",
        {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Optional status filter"},
                "project_id": {"type": "string", "description": "Optional project filter"},
                "limit": {"type": "integer", "default": 50, "description": "Max results"},
            },
        },
    )
    async def task_list(args: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": args.get("limit", 50)}
        if args.get("status"):
            params["status"] = args["status"]
        if args.get("project_id"):
            params["project_id"] = args["project_id"]
        try:
            raw = await request("GET", "/api/tasks", params=params)
        except MCPHTTPError as exc:
            return _format_error(exc)
        # Server returns either a list or a wrapped object — handle both.
        items = raw if isinstance(raw, list) else raw.get("tasks", raw.get("data", []))
        lean = [
            {
                "id": t.get("id"),
                "title": t.get("title") or t.get("spec_id"),
                "status": t.get("status"),
                "project_id": t.get("project_id"),
                "created_at": t.get("created_at"),
            }
            for t in items
            if isinstance(t, dict)
        ]
        return _format_json({"count": len(lean), "tasks": lean})

    @tool(
        "task_running",
        "List AIFactory tasks currently running (phase != idle/completed/failed). "
        "Returns id, title, project_id, phase, started_at for each.",
        {"type": "object", "properties": {}},
    )
    async def task_running(args: dict[str, Any]) -> dict[str, Any]:
        try:
            raw = await request("GET", "/api/tasks/running")
        except MCPHTTPError as exc:
            return _format_error(exc)
        items = raw if isinstance(raw, list) else raw.get("tasks", raw.get("data", []))
        lean = [
            {
                "id": t.get("id"),
                "title": t.get("title") or t.get("spec_id"),
                "project_id": t.get("project_id"),
                "phase": t.get("phase") or t.get("current_phase"),
                "started_at": t.get("started_at"),
            }
            for t in items
            if isinstance(t, dict)
        ]
        return _format_json({"count": len(lean), "running": lean})

    @tool(
        "task_get",
        "Get full task detail by id. Heavy fields (requirements_json, "
        "implementation_plan_json) are truncated to 2000 chars to keep the "
        "response sensibly sized; use the REST API directly for the full payload.",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task id"},
            },
            "required": ["task_id"],
        },
    )
    async def task_get(args: dict[str, Any]) -> dict[str, Any]:
        task_id = args["task_id"]
        try:
            raw = await request("GET", f"/api/tasks/{task_id}")
        except MCPHTTPError as exc:
            return _format_error(exc)
        if not isinstance(raw, dict):
            return _format_error(RuntimeError(f"unexpected payload shape: {type(raw)}"))
        return _format_json(_lean_task(raw))

    @tool(
        "task_status",
        "Get the execution-state object for a task: current phase, current subtask, "
        "overall progress, and the model in use right now. Cheaper than task_get; "
        "use this for polling.",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task id"},
            },
            "required": ["task_id"],
        },
    )
    async def task_status(args: dict[str, Any]) -> dict[str, Any]:
        task_id = args["task_id"]
        try:
            raw = await request("GET", f"/api/tasks/{task_id}/status")
        except MCPHTTPError as exc:
            return _format_error(exc)
        return _format_json(raw)

    @tool(
        "task_get_logs",
        "Get the last N log lines for a task. Default 100, capped at 500 to keep "
        "the response sensibly sized.",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task id"},
                "tail": {
                    "type": "integer",
                    "default": 100,
                    "description": "Number of trailing lines (capped at 500)",
                },
            },
            "required": ["task_id"],
        },
    )
    async def task_get_logs(args: dict[str, Any]) -> dict[str, Any]:
        task_id = args["task_id"]
        tail = min(int(args.get("tail", 100)), 500)
        try:
            raw = await request("GET", f"/api/tasks/{task_id}/logs", params={"tail": tail})
        except MCPHTTPError as exc:
            return _format_error(exc)
        return _format_json(raw)

    # ── Write tools (each writes an AuditLog row server-side) ─────────

    @tool(
        "task_start",
        "Start a task's agent. The task must exist and be in a startable state "
        "(typically 'planned' or 'paused'). Writes an audit log entry server-side.",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task id"},
            },
            "required": ["task_id"],
        },
    )
    async def task_start(args: dict[str, Any]) -> dict[str, Any]:
        task_id = args["task_id"]
        try:
            raw = await request("POST", f"/api/tasks/{task_id}/start", json={})
        except MCPHTTPError as exc:
            return _format_error(exc)
        return _format_json({"started": True, "task_id": task_id, "details": raw})

    @tool(
        "task_stop",
        "Stop a running task. The agent subprocess is terminated; the task can be "
        "resumed with task_start. Writes an audit log entry server-side.",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task id"},
            },
            "required": ["task_id"],
        },
    )
    async def task_stop(args: dict[str, Any]) -> dict[str, Any]:
        task_id = args["task_id"]
        try:
            raw = await request("POST", f"/api/tasks/{task_id}/stop", json={})
        except MCPHTTPError as exc:
            return _format_error(exc)
        return _format_json({"stopped": True, "task_id": task_id, "details": raw})

    @tool(
        "task_approve_plan",
        "Approve a task's implementation plan at the human-review checkpoint. The "
        "agent resumes from where it paused. Writes an audit log entry server-side.",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task id"},
            },
            "required": ["task_id"],
        },
    )
    async def task_approve_plan(args: dict[str, Any]) -> dict[str, Any]:
        task_id = args["task_id"]
        try:
            raw = await request(
                "POST", f"/api/tasks/{task_id}/approve-plan", json={}
            )
        except MCPHTTPError as exc:
            return _format_error(exc)
        return _format_json({"approved": True, "task_id": task_id, "details": raw})

    tools.extend(
        [
            task_list,
            task_running,
            task_get,
            task_status,
            task_get_logs,
            task_start,
            task_stop,
            task_approve_plan,
        ]
    )
    return tools
