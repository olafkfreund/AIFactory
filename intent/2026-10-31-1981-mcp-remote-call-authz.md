---
status: approved
issue: 1981
author: olafkfreund
---

# Intent: Finish porting PF #967 / TF #1470 remote-MCP hardening to AIFactory

## Problem

PFactory #967 and TFactory #1470 hardened remote MCP: per-call project authz on
every `tools/call`, `list_projects` limited to the key's org, strict task ids,
and an SSE session bound only from the transport's first (endpoint) body.

On origin/dev (89c12286) AIFactory already has most of this from #1910, #1913
and #1977 (`apps/web-server/server/mcp_remote/tools.py`, `server.py`):
viewer/member authz per call, the key-org filter on `list_projects`, `_SAFE_ID`,
the `:` rule for task ids, session-owner binding and the key on POST.
Three gaps remain against the reference:

1. `_authorize_call` returns early when the id argument is missing. The tool's
   `KeyError` still refuses the call before any proxy, so this is not
   exploitable today, but the authz check should not rely on that.
2. `aifactory.list_tasks` authorizes `project_id.partition(":")[0]` but
   proxies the whole `project_id`. For `project_id="pa:x"` the key is checked
   against `pa` while REST lists project `pa:x`. Project ids are uuid4 when
   created through REST, but the registry does not forbid `:`.
3. `sse_endpoint` binds the session from the first body anywhere containing
   `session_id=<32 hex>`, not only from the first body. If the first body is
   not the endpoint event, a later body (tool output a caller can influence)
   could bind a session id to this key.

## Proposed outcome

Remote MCP in AIFactory enforces the same rules as PF #967 and TF #1470:
a missing id argument is refused by authz; `list_tasks` authorizes exactly the
project it proxies; a session is bound only from the first body, and only if
that body is the `event: endpoint` event.

## Affected users and systems

`apps/web-server/server/mcp_remote/{tools,server}.py` and their tests. Remote
MCP is off by default and off in prod, so there is no runtime change there.

## Constraints

- Keep the #1910 session-owner binding and the #1977 key-on-POST check.
- Do not change the remote-MCP enabled default.
- No change to REST, stdio MCP or auth.

## Open questions

None. The lead's decision: port what applies; anything that does not apply goes
into the spec under "Not ported" with the reason.
