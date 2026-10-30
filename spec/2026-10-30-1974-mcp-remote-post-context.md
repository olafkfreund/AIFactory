---
status: draft
issue: 1974
intent: intent/2026-10-30-1974-mcp-remote-post-context.md
---

# Spec: Remote MCP tool calls run as the posting key on mcp 2.x

## Design

In `messages_endpoint` (`apps/web-server/server/mcp_remote/server.py`), after
the existing session-owner check (`_session_keys.get(session_id) != key.key_id`
→ 403, #1910), set `_current_key` to the posting key around
`handle_post_message` and reset it in `finally`:

```python
token = _current_key.set(key)
try:
    return await _sse_transport.handle_post_message(...)
finally:
    _current_key.reset(token)
```

Safe because the posting key is re-authenticated on every request and the check
just proved its `key_id` equals the session owner's. This is the PFactory #967 /
TFactory #1470 shape.

Test: add `test_tool_call_over_the_real_transport_runs_as_the_session_key` to
`apps/web-server/tests/test_mcp_remote_session_key.py`, ported from PFactory
#967. It runs `sse_endpoint` on mcp's real `SseServerTransport` and
`_mcp_server.run`, posts `initialize` → `notifications/initialized` →
`tools/call` through `messages_endpoint`, with `dispatch_tool_call` stubbed,
and asserts: another key's post is 403, the owner's call reaches dispatch with
the owner's key, and `missing auth context` is not in the stream.

The session-owner binding already exists on dev (#1910), so no binding work.

## Alternatives rejected

- Store the key per session and look it up in `_call_tool`: the handler has no
  session id in its signature; more code for the same result.
- Pin mcp < 2: the server already uses the 2.x lowlevel API (`mcp>=2.0`).
- Port PF's other #967 changes (first-body-only endpoint match, project authz):
  out of scope for this bug.

## Risks

Low. Only the POST path changes; the 403 check runs first, so no key other than
the session owner's can be set. Remote MCP is flag-gated and off in prod.

## Verification

- New test fails on origin/dev (dispatch never called, stream has
  "missing auth context") and passes with the fix, on mcp 2.3.0.
- `pytest apps/web-server/tests -q -o asyncio_mode=auto`, ruff check/format with
  CI's ruff 0.14.10, `scripts/cq_ratchet.py` as CI runs it. CI green.
