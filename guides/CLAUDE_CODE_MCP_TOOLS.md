# MCP Control-Plane Tools

> Drive AIFactory tasks from a Claude Code session in the AIFactory repo — no portal switch required.

When you open the AIFactory repo in Claude Code, the project-scoped `.mcp.json` registers our stdio MCP server. The server exposes two toolsets:

- **Spec-internal tools** (already shipped) — record discoveries, update subtask/QA status, query build progress. These act on the *active spec*.
- **Task-control tools** (this guide, Epic #50 M1) — list / inspect / start / stop / approve tasks across the whole install. These talk to the running web-server's REST API.

Both sets live under the `mcp__aifactory__*` namespace. Same `.mcp.json` entry, single mental model.

## Prerequisites

1. **Web-server running** on the host. Default: `http://localhost:3101`. Start with:

   ```bash
   cd apps/web-server && python -m server.main
   ```

2. **API token at `~/.aifactory/.token`**. The web-server auto-generates this on first start and the token gets printed to stdout. The MCP server reads it at every tool call (so rotating the token doesn't need a restart).

3. **Override paths via env** (optional — defaults are sensible):

   | Env var | Default | Purpose |
   |---|---|---|
   | `AIFACTORY_API_URL` | `http://localhost:3101` | Where to reach the web-server |
   | `AIFACTORY_API_TOKEN_FILE` | `~/.aifactory/.token` | Path to the bearer token |

   These are wired into `.mcp.json` so a Claude Code session in the repo picks them up automatically.

## Trust model

Tools have **full admin access** via the legacy bearer token. Anyone with the token can drive every task on the install. This matches the current pilot scope. Per-user MCP tokens land in the v1.1 RBAC Epic (#41 SAML+SCIM); until then, treat `~/.aifactory/.token` as a root password.

Every write tool writes an `AuditLog` row server-side with `action=mcp.task.<verb>` so all MCP-initiated state changes are traceable.

## The 8 M1 tools

### Read tools

#### `task_list`

```
What can it do? List tasks across all projects.
Args: status (optional), project_id (optional), limit (default 50)
Returns: lean entries with id, title, status, project_id, created_at
```

**Example prompt:**

> "Show me the running tasks across all projects"

Claude Code will call `task_list({status: "running"})` and report back.

#### `task_running`

```
What can it do? Just-running shortcut — same shape as task_list({status: "running"}) but always-current via GET /api/tasks/running.
Args: none
Returns: id, title, project_id, phase, started_at
```

#### `task_get`

```
What can it do? Full task detail.
Args: task_id (required)
Returns: full task payload with requirements_json / implementation_plan_json
         truncated at 2000 chars so the LLM context doesn't bloat.
```

**Example prompt:**

> "What's the state of task abc123? Show me the implementation plan."

The plan field comes back truncated to keep the response sensibly sized. Hit the REST API directly if you want the whole thing.

#### `task_status`

```
What can it do? Just the execution-state object — cheaper than task_get.
Args: task_id (required)
Returns: { phase, current_subtask, overall_progress, model_in_use }
```

Use this for polling.

#### `task_get_logs`

```
What can it do? Last N log lines.
Args: task_id (required), tail (default 100, capped at 500)
Returns: the log lines.
```

### Write tools

Each writes an `AuditLog` row with `action=mcp.task.<verb>`.

#### `task_start`

```
What can it do? Start an agent for a task.
Args: task_id (required)
Returns: { started: true, task_id, details }
Audit: action=mcp.task.start
```

**Example prompt:**

> "Start task abc123"

#### `task_stop`

```
What can it do? Terminate the running agent subprocess. Resumable via task_start.
Args: task_id (required)
Returns: { stopped: true, task_id, details }
Audit: action=mcp.task.stop
```

#### `task_approve_plan`

```
What can it do? Approve the implementation plan at the human-review checkpoint so the agent resumes.
Args: task_id (required)
Returns: { approved: true, task_id, details }
Audit: action=mcp.task.approve_plan
```

**Example prompt:**

> "Approve the plan for task abc123 and let it continue"

## Error handling — what each failure mode looks like

The MCP tools never raise; failures land as a content block with `isError: true`. Examples:

| Situation | What you see |
|---|---|
| Web-server isn't running | `Error: AIFactory web-server not reachable at http://localhost:3101 — start it with: python -m server.main` |
| Token file missing | `Error: AIFactory API token not found at ~/.aifactory/.token — regenerate via the web UI or run: python -m server.main` |
| Token rejected | `Error: AIFactory token at ~/.aifactory/.token rejected — regenerate via the web UI` |
| Task id not found | `Error: Resource not found at GET /api/tasks/xyz (HTTP 404)` |
| Server error (5xx) | `Error: AIFactory web-server returned HTTP 503: <body, truncated to 500 chars>` |

All are single-line — no stack traces dumped into the chat.

## Walkthrough — full task lifecycle from Claude Code

A complete demo flow without leaving the chat:

```
You: What tasks are currently running?
Claude: [calls task_running] → "No tasks running. There are 3 paused tasks: ..."

You: Start task spec-042-auth-validation
Claude: [calls task_start({task_id: "spec-042-auth-validation"})] → "Started."

You: How's it going?
Claude: [calls task_status({task_id: "spec-042-auth-validation"})] → "Phase: planning, 15% complete..."

You: Show me the implementation plan once it's ready
Claude: [polls task_status, then calls task_get when phase is human_review]
        → "Here's the plan. 8 subtasks, focus on the JWT middleware..."

You: Looks right. Approve it.
Claude: [calls task_approve_plan({task_id: "spec-042-auth-validation"})] → "Approved."

You: If it gets stuck, stop it.
Claude: [later, after detecting stuck phase] [calls task_stop] → "Stopped."
```

## Coming next (M2, Epic #50)

M2 (#52) adds 7 more tools: `task_create_and_run`, `task_recover`, `task_get_diff`, `task_create_pr`, `task_merge_pr`, `project_list`, `agent_status`. Write tools in M2 require explicit `confirm=true` since they're destructive (kick off paid runs, merge PRs, etc.).

## Coming after that (#83 — remote MCP control plane)

Same control-plane surface, different transport: an HTTP+SSE MCP server inside the web-server at `/api/mcp` so non-Claude clients (Cursor, Continue.dev) can drive AIFactory too. Opt-in via `AIFACTORY_MCP_REMOTE_ENABLED=true`.
