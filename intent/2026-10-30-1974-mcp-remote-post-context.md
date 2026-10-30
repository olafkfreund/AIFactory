---
status: approved
issue: 1974
author: olafkfreund
---

# Intent: Remote MCP tool calls run as the posting key on mcp 2.x

## Problem

On mcp 2.x every remote MCP `tools/call` returns `Error: missing auth context`,
for every caller. mcp 2.x runs each client message with the contextvars of the
request that POSTed it (`mcp/shared/_context_streams.py`), and
`apps/web-server/server/mcp_remote/server.py` sets `_current_key` only in
`sse_endpoint` (the GET), never in `messages_endpoint`. `_call_tool` sees no key
and refuses. It fails closed, so nothing is exposed, but remote MCP does not
work at all (it is off by default and off in prod).

`apps/web-server/tests/test_mcp_remote_session_key.py` misses it because it
stubs the transport and `_mcp_server.run`, so no message reaches `_call_tool`.

## Proposed outcome

A `tools/call` posted by the key that opened the SSE session reaches
`dispatch_tool_call` with that key. Another key's post to the session is still
403. A test drives mcp's real SSE transport end to end and fails on dev.

## Affected users and systems

AIFactory web-server, remote MCP routes (`/api/mcp-remote/*`), enabled only by
its feature flag. Same fix already landed in PFactory #967 and TFactory #1470.

## Constraints

- Keep the session-owner check (#1910): it already exists on dev and must stay
  in front of the new code, so the key set is always the session owner's.
- No change to auth, scopes or the tool catalog. No deploy.

## Open questions

None.
