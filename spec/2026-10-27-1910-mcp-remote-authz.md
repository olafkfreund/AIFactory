---
status: approved
issue: 1910
intent: intent/2026-10-27-1910-mcp-remote-authz.md
---

# Spec: remote MCP tools authorize the calling key, not the service token

## Design

All of it is in `apps/web-server/server/mcp_remote/tools.py::dispatch_tool_call`.
The loopback still uses the service token. Before it proxies, the dispatcher
runs the same check stdio runs, as the key's own principal:

1. After `require_scope`, the dispatcher resolves the call's target. For
   `list_tasks` that is `arguments["project_id"]`. For every other tool except
   `list_projects` it is `arguments["task_id"]`, and the project is the part
   before the first `:`. Each tool reads only the argument it proxies, so an
   extra argument cannot point the check at a different project.
2. The target must match `[\w:-][\w.:-]*`: no `/`, `?`, `#`, `%` and no
   leading `.`. Otherwise `a:1/../b:2` would pass the check for `a` and then
   reach `/api/tasks/b:2/...` once httpx collapses the dot segment.
3. `authorize_project_for_user(acw_principal(key), project_id, db, role)`, with
   role `viewer` for `mcp:read` tools and `member` for `mcp:write` tools. Since
   #1914 this denies another org's project (the key is bound to `key.org_id`)
   and an unknown project (404). The `HTTPException` becomes an `isError`
   content block. The DB session comes from `get_db`. If `get_db` yields
   nothing, the call is refused rather than let through.
4. `list_projects` filters the REST result to projects whose stored
   `org_id` is in `accessible_org_ids(...)` for the key principal. `None`
   (service key, or auth disabled) means no filter.

A service key (`user_id` None) becomes an `is_service` principal, so it passes
as before.

## Alternatives rejected

- Forward the caller's own key to REST. Since #1914, REST requires the `api`
  scope for owned keys. MCP-only keys would then fail, or we would have to grant
  `api`, which lets the key act on all of REST. Both are worse than checking
  here.
- Add the check in `_call_internal`. It sees only a path string, not which
  argument names the project.
- Move remote MCP onto the stdio router. That is a larger rewrite.

## Risks

- `tail_agent_console` now checks viewer access before it returns the SSE URL.
  The stream itself was already authorized by REST, with the caller's key.
- Ids with characters outside the allowed set are refused. Task and project ids
  are uuids and `NNN-slug` spec ids, so no real id should be affected.

## Verification

- New real-DB tests (SQLite, real `OrgMember`/`ApiKey` rows, the real
  `authorize_project_for_user`): a member key for org A gets an error from each
  write tool and each task read tool on an org B task, and from `list_tasks`
  on an org B project. `_call_internal` is never called. `list_projects`
  leaves out B's project. The same key on an org A task proxies. A
  `../` task id is refused.
- These fail on dev.
- `tests/test_mcp_remote.py` is updated to stub the new check. Run it with
  `tests/test_mcp_remote_import.py` and the stdio tests.
