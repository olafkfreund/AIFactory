"""Capability-discovery manifest — GET /.well-known/agent-skills/index.json (RFC-0019 §3.4).

The entry point an external agent hits *first*: it enumerates what AIFactory can
do before it holds a credential, so this endpoint is deliberately readable
without authentication. That is safe because the payload is public metadata only
— service identity, the running version, the capability names/descriptions the
MCP tool catalog already publishes, and origin-relative paths to the MCP +
OpenAPI surfaces. No tokens, no internal/upstream hostnames, no user, project or
task data.

``TokenAuthMiddleware`` guards ``/api/*`` and returns early for everything else
(``server/auth.py``), so this path is exempt by construction rather than by an
added exception — ``tests/test_well_known.py`` asserts that with auth enforced.

Shape: ``apis/agent-skills-manifest.schema.json`` in the Factory repo, the
``kind: "service"`` variant.

Where the skills come from
--------------------------
The MCP entries are derived from :func:`server.mcp_remote.tools.get_tool_definitions`
— the same catalog the MCP server answers ``tools/list`` with — so the manifest
cannot claim a tool the server does not serve. The two slash-command entries are
the skill packages under ``.claude/skills/`` in this repo.

``.claude/`` is in ``.dockerignore``, so globbing the skills directory at runtime
would advertise nothing at all from a container image while showing two entries
in a dev checkout. The packages install from GitHub, not from the running pod, so
presence on local disk is the wrong signal — hence the static list below, which
is versioned in the same commit as the packages it names.

URLs are origin-relative: a client already knows the origin it fetched this from,
and emitting an absolute base would mean publishing a hostname we would then have
to keep honest across local / dev / hosted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request

from ..mcp_remote import is_enabled as mcp_remote_enabled
from ..mcp_remote.tools import get_tool_definitions

router = APIRouter(tags=["well-known"])

WELL_KNOWN_AGENT_SKILLS_PATH = "/.well-known/agent-skills/index.json"

REPOSITORY_URL = "https://github.com/olafkfreund/AIFactory"
MCP_DOCUMENTATION_URL = (
    "https://github.com/olafkfreund/Factory/blob/main/apis/aifactory.mcp.md"
)
# argv for the stdio MCP server, for clients that spawn it per-process.
MCP_STDIO_COMMAND = ["bash", "scripts/start-aifactory-mcp.sh"]

# Skill packages shipped under .claude/skills/ — see the module docstring for
# why this is a static list rather than a directory scan.
_SKILL_PACKAGES: list[dict[str, Any]] = [
    {
        "name": "handover",
        "description": (
            "Hand the task you have been discussing interactively off to "
            "AIFactory's autonomous pipeline, so planner -> coder -> QA -> "
            "human-review runs in the background while you work on something "
            "else. Use it for anything you would otherwise babysit."
        ),
        "invocation": {"kind": "slash_command", "command": "/handover"},
        "install": {
            "source": REPOSITORY_URL,
            "path": ".claude/skills/handover/SKILL.md",
        },
        "tags": ["build", "autonomous", "parr"],
    },
    {
        "name": "aifactory-spec",
        "description": (
            "Drive AIFactory spec creation for a new feature: turns a described "
            "feature into the spec document the build pipeline consumes."
        ),
        "invocation": {"kind": "slash_command", "command": "/aifactory-spec"},
        "install": {
            "source": REPOSITORY_URL,
            "path": ".claude/skills/aifactory-spec/SKILL.md",
        },
        "tags": ["spec", "planning"],
    },
]

# REST capabilities worth naming explicitly. Everything else on the REST surface
# is enumerable from the OpenAPI document, which is public for the same reason
# this manifest is (RFC-0019 §3.3).
_REST_SKILLS: list[dict[str, Any]] = [
    {
        "name": "task-from-plan",
        "description": (
            "Build directly from a signed RFC-0002 task contract, skipping "
            "AIFactory's own planning stage. This is the trusted-plan fast path: "
            "the plan has already been decided upstream, so the coder starts "
            "immediately."
        ),
        "invocation": {"kind": "rest", "method": "POST", "path": "/api/tasks/from-plan"},
        "tags": ["build", "rfc-0002", "trusted-plan"],
    },
]


def _skill_name_for_tool(tool_name: str) -> str:
    """Turn an MCP tool name into a manifest skill id.

    ``aifactory.list_projects`` -> ``list-projects``. The schema constrains skill
    ids to ``^[a-z][a-z0-9-]{1,63}$``, which admits neither the dotted namespace
    nor the underscores; the untouched tool name still travels in ``invocation``.
    """
    return tool_name.removeprefix("aifactory.").replace("_", "-").replace(".", "-")


def _mcp_endpoint() -> dict[str, Any]:
    """Describe the MCP surface this instance actually serves.

    The remote HTTP+SSE server is mounted only when ``AIFACTORY_MCP_REMOTE_ENABLED``
    is set, so advertising its URL unconditionally would hand agents a 404. With
    it off we advertise the stdio server, which needs no mount.
    """
    if mcp_remote_enabled():
        return {
            "transport": "http+sse",
            "endpoint": "/api/mcp-remote/sse",
            "command": MCP_STDIO_COMMAND,
            "documentation": MCP_DOCUMENTATION_URL,
        }
    return {
        "transport": "stdio",
        "command": MCP_STDIO_COMMAND,
        "documentation": MCP_DOCUMENTATION_URL,
    }


def _mcp_skills() -> list[dict[str, Any]]:
    """One skill entry per advertised MCP tool, derived from the live catalog."""
    return [
        {
            "name": _skill_name_for_tool(tool["name"]),
            "description": tool["description"],
            "invocation": {"kind": "mcp_tool", "tool": tool["name"]},
            "tags": ["mcp"],
        }
        for tool in get_tool_definitions()
    ]


@router.get(WELL_KNOWN_AGENT_SKILLS_PATH)
async def agent_skills_index(request: Request) -> dict[str, Any]:
    """Serve the RFC-0019 §3.4 service manifest. Public by design — no auth."""
    return {
        "schema_version": "1",
        "kind": "service",
        "service": {
            "name": "aifactory",
            "title": "AIFactory",
            # FastAPI's app.version is read from apps/backend/__init__.py at
            # startup, so this tracks the release without a second source.
            "version": request.app.version,
            "role": "act",
            "description": (
                "Spec-first build core. A task is created from a governed GitHub "
                "issue or a signed RFC-0002 task contract, then a planner -> "
                "coder (isolated git worktree) -> QA pipeline runs against a "
                "multi-provider model factory. It applies TFactory handbacks "
                "through its QA fixer."
            ),
            "repository": REPOSITORY_URL,
        },
        "openapi_url": "/openapi.json",
        "mcp": _mcp_endpoint(),
        "auth": {
            "manifest": "none",
            "schemes": ["bearer", "api-key", "saml"],
        },
        "skills": [*_SKILL_PACKAGES, *_REST_SKILLS, *_mcp_skills()],
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
