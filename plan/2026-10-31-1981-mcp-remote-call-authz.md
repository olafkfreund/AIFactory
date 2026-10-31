---
status: draft
issue: 1981
spec: spec/2026-10-31-1981-mcp-remote-call-authz.md
coders:
  1: agy   # the lead named agy for this lane
  2: agy
  3: agy
---

# Plan: Finish porting PF #967 / TF #1470 remote-MCP hardening

Approved decisions (from the spec): port what applies from PF #967 / TF #1470;
keep the #1910 session-owner binding and the #1977 key-on-POST check; leave
remote MCP off by default. Already on dev and unchanged: per-call viewer/member
authz, `list_projects` limited to the key's org, `_SAFE_ID` and the `:` rule.
Not ported (see spec): PF's `_principal`, `_visible_org_ids`, mcp 2.x
migration, trailing `return Response()`.

## Steps

1. `apps/web-server/server/mcp_remote/tools.py`, `_authorize_call` (~line 90):
   - replace `if arg not in arguments: return  # ...` with
     `raise MCPAuthError(f"missing required argument: {arg}")`;
   - authorize `value.partition(":")[0] if arg == "task_id" else value`
     instead of `value.partition(":")[0]`.
   → verify by `cd apps/web-server && .venv/bin/python -m pytest ../../tests/test_mcp_remote_org_authz.py ../../tests/test_mcp_remote.py -q`
   Traps: `list_projects` must still skip authz; no other edits in this file.

2. `apps/web-server/server/mcp_remote/server.py`:
   - replace `_SESSION_ID = re.compile(rb"session_id=([0-9a-f]{32})")` with
     `_ENDPOINT_EVENT = re.compile(rb"event: endpoint\r?\ndata: [^\r\n]*[?&]session_id=([0-9a-f]{32})\r?\n")`;
   - in `sse_endpoint.send`, inspect only the first `http.response.body`
     (a `first_body = [True]` flag set False on it), using
     `_ENDPOINT_EVENT.match(...)`; always forward the message.
   - `apps/web-server/tests/test_mcp_remote_session_key.py` uses
     `srv._SESSION_ID.search(stream)` (~line 171): change it to
     `re.search(rb"session_id=([0-9a-f]{32})", stream)` (import `re`).
   → verify by `cd apps/web-server && .venv/bin/python -m pytest tests/test_mcp_remote_session_key.py tests/test_mcp_remote_import.py -q`
   Traps: keep the #1910/#1977 checks in `messages_endpoint` unchanged; keep
   dropping session ids in `finally`.

3. Regression tests (each must fail on origin/dev):
   - `tests/test_mcp_remote_org_authz.py`: missing `task_id` (`stop_task`, `{}`)
     and missing `project_id` (`list_tasks`, `{}`) return isError with no
     proxy call; add `"pa:x": {"org_id": "org-b"}` to the `projects` fixture and
     assert `list_tasks` with `{"project_id": "pa:x"}` is refused with no proxy.
   - `apps/web-server/tests/test_mcp_remote_session_key.py`: a test where the
     stubbed `connect_sse` sends a first body that is not the endpoint event,
     then an endpoint-event body; assert `SID` was never bound. And one where
     the first body has the endpoint event after other text (not at start);
     assert nothing bound.
   → verify by both pytest commands above, then `git stash`-free check: run the
   new tests against `git show origin/dev:<file>` copies of tools.py/server.py
   to confirm they fail on dev.
   Traps: never `git stash` in this worktree.

## Tests

- `cd apps/web-server && .venv/bin/python -m pytest ../../tests/test_mcp_remote_org_authz.py ../../tests/test_mcp_remote.py tests/test_mcp_remote_session_key.py tests/test_mcp_remote_import.py -q` — all pass.
- `ruff check` / `ruff format --check` on the changed files; `scripts/ratchet_lint.py` if present.

## Rollback

Revert the PR commit(s); no data or config changes.
