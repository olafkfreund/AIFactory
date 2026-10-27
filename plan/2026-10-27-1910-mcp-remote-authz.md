---
status: approved
issue: 1910
spec: spec/2026-10-27-1910-mcp-remote-authz.md
---

# Plan: remote MCP tools authorize the calling key, not the service token

Decisions (from the spec):
- Check in `dispatch_tool_call` before the proxy. The loopback keeps the
  service token, and the key is not forwarded.
- Target per tool: `project_id` for `list_tasks`, `task_id` for the rest.
  `list_projects` is filtered.
- Ids must match `[\w:-][\w.:-]*`.
- Role: viewer for `mcp:read` tools, member for `mcp:write` tools.
- If `get_db` yields no session, refuse the call.

## Steps

1. `apps/web-server/server/mcp_remote/tools.py`:
   - add `_authorize_call(tool_name, arguments, key, minimum_role)` and
     `_accessible_org_ids(key)`, using `get_db`, `acw_principal`,
     `authorize_project_for_user` and `accessible_org_ids`;
   - call `_authorize_call` after `require_scope`, turning `MCPAuthError` and
     `HTTPException` into `_format_error`;
   - filter `list_projects` by `load_projects()[id]["org_id"]`;
   - update the module docstring (step 1 of the tool wrapper);
   - in `tests/test_mcp_remote.py`, add an autouse fixture that stubs both
     helpers so the endpoint tests stay unit tests.
   → verify: `pytest tests/test_mcp_remote.py tests/test_mcp_remote_import.py -q`
2. Add `tests/test_mcp_remote_org_authz.py`, the real-DB tests from the spec,
   with `tools.get_db` monkeypatched to a SQLite session, `load_projects`
   patched to `{pa: org-a, pb: org-b}`, `_auth_disabled` set to False, and
   `_call_internal` mocked.
   → verify: the new file passes with the fix and fails on dev. Then run
   `pytest tests/ -k "mcp" -q` and the cq_ratchet ruff command.

## Tests

As above. The Docker/CI suites run on the PR.

## Rollback

Revert the PR. Remote MCP is off by default.

## Deviations

- Step 1: no stub fixture in `tests/test_mcp_remote.py`. The suite runs with
  `APP_DISABLE_AUTH=true` (tests/conftest.py), so `authorize_project_for_user`
  and `accessible_org_ids` no-op there, and all 47 tests pass unchanged. The
  new real-DB file sets `_auth_disabled` to False itself.
- Step 1: `acw_principal` is imported at module level. There is no import cycle,
  because `server.mcp_remote/__init__` does not import `tools`.
