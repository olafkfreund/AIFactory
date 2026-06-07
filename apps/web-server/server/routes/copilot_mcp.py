"""AIFactory MCP server for the GitHub Copilot cloud agent.

Implements a minimal POST-only JSON-RPC 2.0 MCP transport at ``POST /mcp``
so the Copilot cloud agent can call AIFactory tools during its coding
session.  The Copilot MCP client supports this POST-only mode — full
Streamable HTTP (server-initiated GET channel, session lifecycle) is out
of scope here.

Enabled when ``AIFACTORY_MCP_SECRET`` is set in the environment (the bearer
token the Copilot cloud agent sends).  If the variable is absent the server
accepts all requests (dev convenience); set it in production.

Configure in GitHub repository settings → Copilot → MCP servers:
    {
      "mcpServers": {
        "aifactory": {
          "type": "http",
          "url": "${COPILOT_MCP_AIFACTORY_URL}/mcp",
          "headers": {"Authorization": "Bearer ${COPILOT_MCP_AIFACTORY_TOKEN}"}
        }
      }
    }

Set as Copilot agent secrets:
    COPILOT_MCP_AIFACTORY_URL   — public URL of this AIFactory instance
    COPILOT_MCP_AIFACTORY_TOKEN — same value as AIFACTORY_MCP_SECRET
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Copilot MCP"])

# ---------------------------------------------------------------------------
# Tool catalog
# ---------------------------------------------------------------------------

MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "aifactory_get_spec",
        "description": "Get the spec.md for a task (implementation requirements and acceptance criteria)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "issue_number": {
                    "type": "integer",
                    "description": "GitHub issue number linked to the task",
                },
                "task_id": {
                    "type": "string",
                    "description": "AIFactory task ID (e.g. '001-add-auth')",
                },
            },
        },
    },
    {
        "name": "aifactory_get_plan",
        "description": "Get the implementation_plan.json subtask breakdown with file-level guidance",
        "inputSchema": {
            "type": "object",
            "properties": {
                "issue_number": {"type": "integer"},
                "task_id": {"type": "string"},
            },
        },
    },
    {
        "name": "aifactory_get_context",
        "description": "Get discovered codebase context: relevant files, patterns, and dependencies",
        "inputSchema": {
            "type": "object",
            "properties": {
                "issue_number": {"type": "integer"},
            },
        },
    },
    {
        "name": "aifactory_record_discovery",
        "description": "Record a code discovery into AIFactory memory for future sessions",
        "inputSchema": {
            "type": "object",
            "required": ["content"],
            "properties": {
                "issue_number": {"type": "integer"},
                "content": {"type": "string", "description": "Discovery text to store"},
                "category": {
                    "type": "string",
                    "enum": ["pattern", "dependency", "architecture", "other"],
                    "description": "Discovery category",
                },
            },
        },
    },
    {
        "name": "aifactory_record_gotcha",
        "description": "Record a gotcha or pitfall found during implementation so future agents avoid it",
        "inputSchema": {
            "type": "object",
            "required": ["content"],
            "properties": {
                "issue_number": {"type": "integer"},
                "content": {"type": "string", "description": "Gotcha description"},
            },
        },
    },
    {
        "name": "aifactory_get_build_progress",
        "description": "Get current task status and subtask completion progress",
        "inputSchema": {
            "type": "object",
            "properties": {
                "issue_number": {"type": "integer"},
                "task_id": {"type": "string"},
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------


def _verify_mcp_token(request: Request) -> None:
    expected = os.environ.get("AIFACTORY_MCP_SECRET", "")
    if not expected:
        return  # no secret configured — open (dev mode)
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid MCP token")


# ---------------------------------------------------------------------------
# Task / spec resolver
# ---------------------------------------------------------------------------


def _get_projects_data_dir() -> Path:
    raw = os.environ.get("APP_PROJECTS_DATA_DIR", "")
    return Path(raw).expanduser() if raw else Path.home() / ".aifactory"


async def _resolve_task_by_issue(issue_number: int) -> dict | None:
    """Scan task_metadata.json files for one whose copilot_dispatch.issue_number matches."""
    base = _get_projects_data_dir()
    for spec_dir in sorted(base.glob("**/specs/*")):
        if not spec_dir.is_dir():
            continue
        meta_file = spec_dir / "task_metadata.json"
        if not meta_file.exists():
            continue
        try:
            meta = json.loads(meta_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if meta.get("copilot_dispatch", {}).get("issue_number") == issue_number:
            return {
                "task_id": spec_dir.name,
                "spec_dir": str(spec_dir),
                "metadata": meta,
            }
    return None


async def _resolve_task(
    issue_number: int | None, task_id: str | None
) -> tuple[Path | None, dict | None]:
    """Return (spec_dir, metadata) from issue_number or task_id, or (None, None)."""
    if issue_number is not None:
        result = await _resolve_task_by_issue(issue_number)
        if result:
            return Path(result["spec_dir"]), result["metadata"]

    if task_id:
        base = _get_projects_data_dir()
        for spec_dir in base.glob(f"**/specs/{task_id}"):
            if spec_dir.is_dir():
                meta_file = spec_dir / "task_metadata.json"
                meta = {}
                if meta_file.exists():
                    try:
                        meta = json.loads(meta_file.read_text())
                    except (json.JSONDecodeError, OSError):
                        pass
                return spec_dir, meta

    return None, None


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def _tool_get_spec(spec_dir: Path) -> str:
    spec_file = spec_dir / "spec.md"
    if not spec_file.exists():
        return "spec.md not found for this task"
    return spec_file.read_text()


async def _tool_get_plan(spec_dir: Path) -> str:
    plan_file = spec_dir / "implementation_plan.json"
    if not plan_file.exists():
        return "implementation_plan.json not found — task may still be in spec phase"
    return plan_file.read_text()


async def _tool_get_context(spec_dir: Path) -> str:
    ctx_file = spec_dir / "context.json"
    if not ctx_file.exists():
        return "context.json not found for this task"
    return ctx_file.read_text()


async def _tool_record_discovery(
    spec_dir: Path, content: str, category: str = "other"
) -> str:
    discoveries_file = spec_dir / "discoveries.jsonl"
    entry = json.dumps({"category": category, "content": content}) + "\n"
    with discoveries_file.open("a") as fh:
        fh.write(entry)
    logger.info(
        "[copilot-mcp] recorded discovery category=%s spec=%s", category, spec_dir.name
    )
    return f"Discovery recorded (category={category})"


async def _tool_record_gotcha(spec_dir: Path, content: str) -> str:
    gotchas_file = spec_dir / "gotchas.jsonl"
    entry = json.dumps({"content": content}) + "\n"
    with gotchas_file.open("a") as fh:
        fh.write(entry)
    logger.info("[copilot-mcp] recorded gotcha spec=%s", spec_dir.name)
    return "Gotcha recorded"


async def _tool_get_build_progress(spec_dir: Path, metadata: dict) -> str:
    plan_file = spec_dir / "implementation_plan.json"
    if not plan_file.exists():
        return json.dumps({"status": "planning", "subtasks": []})
    try:
        plan = json.loads(plan_file.read_text())
    except (json.JSONDecodeError, OSError):
        return json.dumps({"status": "unknown"})
    # Summarise subtask completion
    phases = plan.get("phases", [])
    total, done = 0, 0
    for phase in phases:
        for subtask in phase.get("subtasks", []):
            total += 1
            if subtask.get("status") == "completed":
                done += 1
    return json.dumps(
        {
            "task_id": spec_dir.name,
            "phases": len(phases),
            "subtasks_total": total,
            "subtasks_done": done,
            "copilot_dispatch": metadata.get("copilot_dispatch", {}),
        }
    )


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 dispatcher
# ---------------------------------------------------------------------------


async def _dispatch_tool(
    name: str, arguments: dict, issue_number: int | None, task_id_arg: str | None
) -> str:
    spec_dir, metadata = await _resolve_task(issue_number, task_id_arg)

    if spec_dir is None:
        return f"No task found for issue_number={issue_number} task_id={task_id_arg}"

    if name == "aifactory_get_spec":
        return await _tool_get_spec(spec_dir)
    if name == "aifactory_get_plan":
        return await _tool_get_plan(spec_dir)
    if name == "aifactory_get_context":
        return await _tool_get_context(spec_dir)
    if name == "aifactory_record_discovery":
        return await _tool_record_discovery(
            spec_dir,
            arguments.get("content", ""),
            arguments.get("category", "other"),
        )
    if name == "aifactory_record_gotcha":
        return await _tool_record_gotcha(spec_dir, arguments.get("content", ""))
    if name == "aifactory_get_build_progress":
        return await _tool_get_build_progress(spec_dir, metadata or {})

    return f"Unknown tool: {name}"


# ---------------------------------------------------------------------------
# MCP endpoint
# ---------------------------------------------------------------------------


@router.post("/mcp")
async def mcp_endpoint(request: Request) -> JSONResponse:
    """JSON-RPC 2.0 MCP endpoint for the GitHub Copilot cloud agent.

    Handles: initialize, tools/list, tools/call.
    """
    _verify_mcp_token(request)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content=_error(-32700, "Parse error", id=None),
        )

    rpc_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params", {})

    if method == "initialize":
        return JSONResponse(
            _result(
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "aifactory", "version": "1.0.0"},
                },
                rpc_id,
            )
        )

    if method == "tools/list":
        return JSONResponse(_result({"tools": MCP_TOOLS}, rpc_id))

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        issue_number = arguments.get("issue_number")
        task_id_arg = arguments.get("task_id")

        try:
            text = await _dispatch_tool(tool_name, arguments, issue_number, task_id_arg)
        except Exception as exc:
            logger.exception("[copilot-mcp] tool call failed tool=%s", tool_name)
            return JSONResponse(_error(-32603, f"Internal error: {exc}", rpc_id))

        return JSONResponse(
            _result(
                {"content": [{"type": "text", "text": text}]},
                rpc_id,
            )
        )

    return JSONResponse(
        status_code=400,
        content=_error(-32601, f"Method not found: {method}", rpc_id),
    )


def _result(result: Any, rpc_id: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _error(code: int, message: str, id: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}
