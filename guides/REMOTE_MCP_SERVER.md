# Remote MCP Server — non-Claude client access

> Same task-control plane as the stdio MCP server, different transport: an HTTP+SSE MCP server exposed by the AIFactory web-server so non-Claude clients (Cursor, Continue.dev, custom scripts, programmatic users) can drive AIFactory.

Sister doc to [`CLAUDE_CODE_MCP_TOOLS.md`](./CLAUDE_CODE_MCP_TOOLS.md) (stdio server for Claude Code in this repo). Same Epic (#50), different audience.

## Enabling

Off by default. Set the env var on your AIFactory deployment:

```bash
AIFACTORY_MCP_REMOTE_ENABLED=true
```

The routes mount only when this is truthy. Default deployments are completely unchanged — no new attack surface.

## Endpoints

| Path | Method | Purpose |
|---|---|---|
| `/api/mcp-remote/sse` | `GET` | SSE event stream — long-lived connection the MCP client subscribes to |
| `/api/mcp-remote/messages/` | `POST` | Client → server JSON-RPC message channel for the active SSE session |

Both require `Authorization: Bearer acw_<key>`.

## Auth model

The remote server validates **`acw_` API keys** (minted via the web UI), not the legacy admin bearer token. Each key carries **scopes**:

- `mcp:read` — for read tools (list / get / diff)
- `mcp:write` — for write tools (start / stop / approve / merge)

A key with only `mcp:read` calling `start_task` gets:

```
Error: API key lacks required scope 'mcp:write'. Mint a new key with the right scope via the web UI.
```

Why scopes (not just "is this key valid?"): scope-gating lets you give a Cursor session a read-only key to *observe* AIFactory state from your editor without risking accidental task starts. The write-scope key is what you mint when you actually want to drive things.

### Minting a key

In the AIFactory web UI:

1. Settings → API Keys → New key
2. Name: `Cursor (read-only)` or whatever helps you identify it
3. Scopes: tick `mcp:read` (and `mcp:write` if you want write access)
4. Save — the raw `acw_…` token is shown ONCE. Copy it to your client config.

## Client configuration

### Cursor

`~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "aifactory-remote": {
      "url": "https://aifactory.example.com/api/mcp-remote/sse",
      "headers": {
        "Authorization": "Bearer acw_yourKeyHere"
      }
    }
  }
}
```

### Continue.dev

`~/.continue/config.json`:

```json
{
  "experimental": {
    "modelContextProtocolServer": {
      "transport": {
        "type": "sse",
        "url": "https://aifactory.example.com/api/mcp-remote/sse"
      },
      "headers": {
        "Authorization": "Bearer acw_yourKeyHere"
      }
    }
  }
}
```

### Programmatic (Python `mcp` SDK)

```python
import os
from mcp.client.sse import sse_client

async with sse_client(
    "https://aifactory.example.com/api/mcp-remote/sse",
    headers={"Authorization": f"Bearer {os.environ['AIFACTORY_KEY']}"},
) as (read, write):
    # Now use mcp.ClientSession on (read, write)
    ...
```

## 12-tool catalog status

| Tool | Status | Scope | Notes |
|---|---|---|---|
| `aifactory.list_projects` | ✓ shipped | `mcp:read` | Lists all projects |
| `aifactory.list_tasks` | ✓ shipped | `mcp:read` | Per-project task list |
| `aifactory.get_task` | ✓ shipped | `mcp:read` | Full task detail |
| `aifactory.get_worktree_diff` | ✓ shipped | `mcp:read` | What the agent has written |
| `aifactory.start_task` | ✓ shipped | `mcp:write` | Start a task's agent |
| `aifactory.stop_task` | ✓ shipped | `mcp:write` | Stop a running task |
| `aifactory.approve_plan` | ✓ shipped | `mcp:write` | Approve plan at the review checkpoint |
| `aifactory.merge_pr` | ✓ shipped | `mcp:write` | Merge the worktree PR |
| `aifactory.get_qa_report` | ⏳ V1.1 | `mcp:read` | Needs `GET /api/tasks/{id}/qa-report` |
| `aifactory.tail_agent_console` | ⏳ V1.1 | `mcp:read` | Needs `GET /api/tasks/{id}/agent-console/sse` |
| `aifactory.reject_plan` | ⏳ V1.1 | `mcp:write` | Needs `POST /api/tasks/{id}/reject-plan` |
| `aifactory.recover_task` | ⏳ V1.1 | `mcp:write` | Builds on stdio M2's recover surface |

The 4 deferred tools land in a follow-up PR once their backing REST endpoints exist.

## Architecture — how this fits with the stdio server

```
                          ┌──────────────────────────┐
                          │  AIFactory web-server    │
                          │  (FastAPI)               │
                          │                          │
   Claude Code in repo    │  • REST /api/tasks/*     │     Cursor / Continue.dev
   ────────────────►      │  • stdio MCP             │     ────────────────────►
   stdio MCP subprocess   │    (separate process,    │     HTTP+SSE MCP
   (apps/backend/         │    spawned by Claude     │     (apps/web-server/
    mcp_server/           │    Code via .mcp.json)   │      server/mcp_remote/)
    aifactory_server.py)  │                          │
                          └──────────────────────────┘
```

Both servers expose the same conceptual surface (task control plane). They share the underlying REST endpoints — anything one can do, the other can do. The split is purely about transport + auth model:

| | stdio MCP | Remote HTTP+SSE MCP |
|---|---|---|
| Transport | stdin/stdout pipes | HTTP + SSE |
| Started by | Claude Code via `.mcp.json` | The AIFactory web-server |
| Auth | Legacy admin bearer token (`~/.aifactory/.token`) | `acw_` API keys with `mcp:read`/`mcp:write` scopes |
| Audience | Claude Code in this repo | Cursor, Continue.dev, programmatic clients |
| Default | Always on (registered via `.mcp.json`) | Off (`AIFACTORY_MCP_REMOTE_ENABLED=true`) |

## Error matrix

| Situation | What the client sees |
|---|---|
| Missing `Authorization` header | HTTP 401 + `{"error": "Missing or malformed Authorization header (expected 'Bearer <token>')"}` |
| Unknown key | HTTP 401 + `{"error": "Invalid API key"}` |
| Revoked key | HTTP 401 + `{"error": "API key has been revoked"}` |
| Insufficient scope | `content` block with `isError: true` + actionable message naming the missing scope |
| Tool not found | `content` block with `isError: true` + `"unknown tool: <name>"` |
| Backend HTTP error | `content` block with `isError: true` + truncated body |

Every error stays a single line, no stack traces.

## Security notes

- `acw_` keys are sha256-hashed at rest with an 8-char preview prefix. The raw token is shown only at creation time.
- `mcp:read` is enough to enumerate tasks across the install — treat it like read-only DB access.
- `mcp:write` can trigger paid LLM runs (`start_task`) and merge PRs (`merge_pr`). Treat it like a deploy key.
- Routes are reachable only when `AIFACTORY_MCP_REMOTE_ENABLED=true`. The default deployment surface is unchanged.
