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

    # ── M2 Write tools (destructive — gated by confirm=true) ──────────
    #
    # The 4 destructive M2 tools (create_and_run, recover, create_pr,
    # merge_pr) MUST refuse without explicit ``confirm=true``. Autonomous
    # Claude Code sessions shouldn't kick off paid agent runs or merge
    # production PRs unprompted — the confirm-gate forces a deliberate
    # second LLM turn for these actions.

    def _confirm_gate_response(verb: str, preview: dict[str, Any]) -> dict[str, Any]:
        """Structured ``requires_confirmation`` response shown when confirm=false."""
        body = {
            "requires_confirmation": True,
            "verb": verb,
            "preview": preview,
            "to_proceed": f"Re-call this tool with confirm=true to actually {verb}.",
        }
        return _format_json(body)

    @tool(
        "task_create_and_run",
        "Create a new task from a description and start it immediately. "
        "DESTRUCTIVE: kicks off a paid agent run — requires confirm=true. "
        "Returns the new task_id once started.",
        {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "model": {"type": "string", "description": "Override default model (optional)"},
                "confirm": {
                    "type": "boolean",
                    "default": False,
                    "description": "Required true to actually create + run",
                },
            },
            "required": ["project_id", "title", "description"],
        },
    )
    async def task_create_and_run(args: dict[str, Any]) -> dict[str, Any]:
        if not args.get("confirm"):
            return _confirm_gate_response(
                "create_and_run",
                {
                    "project_id": args.get("project_id"),
                    "title": args.get("title"),
                    "description_preview": (args.get("description", "")[:200]),
                },
            )
        payload = {
            "project_id": args["project_id"],
            "title": args["title"],
            "description": args["description"],
        }
        if args.get("model"):
            payload["model"] = args["model"]
        try:
            raw = await request(
                "POST", "/api/tasks/create-and-run", json=payload
            )
        except MCPHTTPError as exc:
            return _format_error(exc)
        return _format_json({"created_and_started": True, "details": raw})

    @tool(
        "task_recover",
        "Recover a stuck task — restarts the agent from its last checkpoint. "
        "DESTRUCTIVE: requires confirm=true. With auto_restart=false the task "
        "is left paused after recovery so a human can inspect first.",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "auto_restart": {"type": "boolean", "default": False},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["task_id"],
        },
    )
    async def task_recover(args: dict[str, Any]) -> dict[str, Any]:
        if not args.get("confirm"):
            return _confirm_gate_response(
                "recover",
                {
                    "task_id": args.get("task_id"),
                    "auto_restart": args.get("auto_restart", False),
                },
            )
        task_id = args["task_id"]
        payload = {"auto_restart": args.get("auto_restart", False)}
        try:
            raw = await request(
                "POST", f"/api/tasks/{task_id}/recover", json=payload
            )
        except MCPHTTPError as exc:
            return _format_error(exc)
        return _format_json({"recovered": True, "task_id": task_id, "details": raw})

    @tool(
        "task_create_pr",
        "Create a GitHub PR from the task's worktree branch. DESTRUCTIVE "
        "(visible on GitHub) — requires confirm=true. Title and body default "
        "to the spec title + summary.",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["task_id"],
        },
    )
    async def task_create_pr(args: dict[str, Any]) -> dict[str, Any]:
        if not args.get("confirm"):
            return _confirm_gate_response(
                "create_pr",
                {
                    "task_id": args.get("task_id"),
                    "title": args.get("title", "(default to spec title)"),
                },
            )
        task_id = args["task_id"]
        payload: dict[str, Any] = {}
        if args.get("title"):
            payload["title"] = args["title"]
        if args.get("body"):
            payload["body"] = args["body"]
        try:
            raw = await request(
                "POST", f"/api/tasks/{task_id}/worktree/create-pr", json=payload
            )
        except MCPHTTPError as exc:
            return _format_error(exc)
        return _format_json({"created": True, "task_id": task_id, "details": raw})

    @tool(
        "task_merge_pr",
        "Merge the task's open PR into the project's default branch. "
        "DESTRUCTIVE: requires confirm=true. merge_method defaults to "
        "'merge'; can be 'squash' or 'rebase'.",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "merge_method": {
                    "type": "string",
                    "enum": ["merge", "squash", "rebase"],
                    "default": "merge",
                },
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["task_id"],
        },
    )
    async def task_merge_pr(args: dict[str, Any]) -> dict[str, Any]:
        if not args.get("confirm"):
            return _confirm_gate_response(
                "merge_pr",
                {
                    "task_id": args.get("task_id"),
                    "merge_method": args.get("merge_method", "merge"),
                },
            )
        task_id = args["task_id"]
        payload = {"merge_method": args.get("merge_method", "merge")}
        try:
            raw = await request(
                "POST", f"/api/tasks/{task_id}/worktree/merge", json=payload
            )
        except MCPHTTPError as exc:
            return _format_error(exc)
        return _format_json({"merged": True, "task_id": task_id, "details": raw})

    # ── M2 Read tools ─────────────────────────────────────────────────

    @tool(
        "task_get_diff",
        "Get the worktree diff for a task — what the agent has written so far. "
        "Truncates at max_lines (default 1000) to keep the response sane; "
        "use the REST API directly for the full diff.",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "max_lines": {"type": "integer", "default": 1000},
            },
            "required": ["task_id"],
        },
    )
    async def task_get_diff(args: dict[str, Any]) -> dict[str, Any]:
        task_id = args["task_id"]
        max_lines = int(args.get("max_lines", 1000))
        try:
            raw = await request(
                "GET", f"/api/tasks/{task_id}/worktree/diff"
            )
        except MCPHTTPError as exc:
            return _format_error(exc)

        # Server may return {diff: "..."} or a raw string. Normalize to string.
        diff_text = raw.get("diff", "") if isinstance(raw, dict) else str(raw)
        lines = diff_text.splitlines()
        truncated = len(lines) > max_lines
        if truncated:
            lines = lines[:max_lines]
            lines.append(f"...[truncated after {max_lines} lines]")
        return _format_json(
            {
                "task_id": task_id,
                "lines": len(lines),
                "truncated": truncated,
                "diff": "\n".join(lines),
            }
        )

    @tool(
        "project_list",
        "List all projects registered with this AIFactory install. "
        "Returns id, name, path, git_provider for each.",
        {"type": "object", "properties": {}},
    )
    async def project_list(args: dict[str, Any]) -> dict[str, Any]:
        try:
            raw = await request("GET", "/api/projects")
        except MCPHTTPError as exc:
            return _format_error(exc)
        items = raw if isinstance(raw, list) else raw.get("projects", raw.get("data", []))
        lean = [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "path": p.get("path"),
                "git_provider": p.get("git_provider") or p.get("gitProvider"),
            }
            for p in items
            if isinstance(p, dict)
        ]
        return _format_json({"count": len(lean), "projects": lean})

    @tool(
        "agent_status",
        "Single-call answer to 'what's this agent doing right now?' Combines "
        "task_status (phase + progress) with the model + subtask in flight. "
        "Cheaper to read than calling task_status + task_get separately.",
        {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    )
    async def agent_status(args: dict[str, Any]) -> dict[str, Any]:
        task_id = args["task_id"]
        # Fetch status + task in parallel-ish (sequential here for simpler
        # error handling — both fail in the same way).
        try:
            status_data = await request("GET", f"/api/tasks/{task_id}/status")
        except MCPHTTPError as exc:
            return _format_error(exc)
        try:
            task_data = await request("GET", f"/api/tasks/{task_id}")
        except MCPHTTPError as exc:
            return _format_error(exc)

        if not isinstance(status_data, dict):
            status_data = {}
        if not isinstance(task_data, dict):
            task_data = {}

        # Best-effort field extraction — different server versions use
        # slightly different shapes for the phase-model mapping.
        phase_models = (
            task_data.get("phaseModels")
            or task_data.get("phase_models")
            or task_data.get("metadata", {}).get("phaseModels")
            or {}
        )
        current_phase = (
            status_data.get("phase")
            or status_data.get("current_phase")
            or task_data.get("status")
        )
        model = (
            status_data.get("model_in_use")
            or phase_models.get(current_phase or "")
            or task_data.get("model")
        )
        return _format_json(
            {
                "task_id": task_id,
                "phase": current_phase,
                "model": model,
                "current_subtask_id": status_data.get("current_subtask_id"),
                "current_subtask_title": status_data.get("current_subtask")
                or status_data.get("current_subtask_title"),
                "overall_progress": status_data.get("overall_progress"),
            }
        )

    tools.extend(
        [
            # M1
            task_list,
            task_running,
            task_get,
            task_status,
            task_get_logs,
            task_start,
            task_stop,
            task_approve_plan,
            # M2
            task_create_and_run,
            task_recover,
            task_create_pr,
            task_merge_pr,
            task_get_diff,
            project_list,
            agent_status,
        ]
    )
    return tools
