---
status: draft
issue: 1981
intent: intent/2026-10-31-1981-mcp-remote-call-authz.md
---

# Spec: Finish porting PF #967 / TF #1470 remote-MCP hardening

## Design

Decision (lead, 2026-10-31 wave): port what applies from PF #967 / TF #1470;
keep #1910 session-owner binding and #1977 key-on-POST; do not change the
remote-MCP default (off).

`apps/web-server/server/mcp_remote/tools.py`, `_authorize_call`:

- A missing id argument raises `MCPAuthError("missing required argument: <arg>")`
  instead of returning (PF/TF behaviour).
- The project authorized is `value.partition(":")[0]` for `task_id` and the
  whole `value` for `project_id`, so `list_tasks` authorizes exactly the
  project it proxies.

`apps/web-server/server/mcp_remote/server.py`, `sse_endpoint`:

- Replace `_SESSION_ID` (search anywhere) with `_ENDPOINT_EVENT`, matched at
  the start of the body:
  `rb"event: endpoint\r?\ndata: [^\r\n]*[?&]session_id=([0-9a-f]{32})\r?\n"`.
- Only the first `http.response.body` is inspected; later bodies never bind.
- Messages are still forwarded unchanged, and bound sessions are still dropped
  when the stream closes.

Already present on origin/dev, no change (verified against the reference):

- viewer for `mcp:read`, member for `mcp:write`, always enforced via
  `authorize_project_for_user` (#1910).
- `list_projects` filtered by `accessible_org_ids`, which intersects with the
  key's own org (#1913) and requires current membership.
- `_SAFE_ID` rejects `/ ? # %` and a leading `.`; task ids need `:`.
- Session-owner binding (#1910) and key on POST (#1977).

## Not ported

- PF's `_principal(key)`: AIFactory uses `acw_principal` from
  `mcp_stdio/auth.py`, which already maps ownerless keys to the service
  principal and also handles scoped service tokens. Replacing it would drop that.
- PF's `_visible_org_ids` / `_list_projects` helper: AIFactory's
  `accessible_org_ids` already limits a key to its own org (#1913). Same result.
- PF's mcp 2.x lowlevel-API migration and `mcp>=2.0` pin: AIFactory's
  `server.py` already uses the 2.x API (`on_list_tools`/`on_call_tool`).
- PF's trailing `return Response()` in `sse_endpoint`: response plumbing, not
  part of the authz hardening in #1981. Out of scope.

## Alternatives rejected

- Rejecting `:` in `project_id` instead of authorizing the whole value: also
  closes gap 2, but differs from PF/TF, and registry ids are not documented as
  colon-free. Authorizing what is proxied is the reference rule.
- Leaving the missing-argument early return (the `KeyError` already refuses):
  it keeps authz dependent on the tool body; PF/TF refuse in authz.

## Risks

- A client whose first SSE body is not the endpoint event never gets a bound
  session, so its POSTs get 403. The mcp SDK's `SseServerTransport` always
  sends the endpoint event first; the existing real-transport test covers it.
- Remote MCP is off in prod; no runtime effect until enabled.

## Verification

- `tests/test_mcp_remote_org_authz.py`: new cases — missing `task_id` /
  `project_id` refused with no proxy; `list_tasks` with `project_id="pa:x"`
  where `pa` is own-org and `pa:x` is org-b is refused. They fail on dev.
- `apps/web-server/tests/test_mcp_remote_session_key.py`: a first body that
  is not the endpoint event, followed by an endpoint-shaped body, binds
  nothing; an endpoint body not at the start of the first body binds nothing.
  Fails on dev.
- Existing remote-MCP tests still pass; ruff and `scripts/ratchet_lint.py`.
