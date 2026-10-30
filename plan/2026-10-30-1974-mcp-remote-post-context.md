---
status: draft
issue: 1974
spec: spec/2026-10-30-1974-mcp-remote-post-context.md
coders:
  1: codex   # user asked for Codex as this lane's coder (2026-10-30 wave)
  2: codex
---

# Plan: Remote MCP tool calls run as the posting key on mcp 2.x

Approved decisions (from the spec):

- On mcp 2.x each POSTed message runs with the POST request's contextvars, so
  `messages_endpoint` must set `_current_key` itself.
- Set it to the posting key only AFTER the existing session-owner 403 check
  (#1910, already on dev), reset it in `finally`. Same shape as PFactory #967.
- Regression test drives mcp's REAL SSE transport (no stubbed `run`,
  `connect_sse` or `handle_post_message`); only `authenticate` and
  `dispatch_tool_call` are stubbed.
- Nothing else from PF #967 is ported.

## Steps

1. `apps/web-server/tests/test_mcp_remote_session_key.py` (append after line 86):
   add `test_tool_call_over_the_real_transport_runs_as_the_session_key`, ported
   from PFactory #967 (`gh pr diff 967 -R olafkfreund/PFactory`). Start
   `srv.sse_endpoint` in an anyio task group with a Request whose receive waits
   on an event then returns `http.disconnect`; read `session_id` from the
   stream; POST via `srv.messages_endpoint` `initialize` (202),
   `notifications/initialized` (202), `tools/call` from key-b (403, catch
   `HTTPException`) and from key-a (202); wait for `"id":2`; assert
   `dispatched == [(name, "key-a")]` and `b"missing auth context" not in stream`.
   → verify: `apps/backend/.venv/bin/pytest apps/web-server/tests/test_mcp_remote_session_key.py -q -o asyncio_mode=auto`
   FAILS on the new test only (fix not yet in).
   Traps: the existing test imports `server.mcp_remote.server as srv` at module
   level (conftest puts apps/web-server on sys.path); keep that style. Use
   `mcp.types.LATEST_PROTOCOL_VERSION`. Bound every wait with `anyio.fail_after`.

2. `apps/web-server/server/mcp_remote/server.py:184-186` (`messages_endpoint`):
   wrap the `handle_post_message` return in `token = _current_key.set(key)` /
   `try` / `finally: _current_key.reset(token)`, with a one-line comment saying
   why (mcp 2.x hands each message the POSTer's contextvars; key equals the
   session owner). Update the docstring sentence about per-call auth if needed.
   → verify: same pytest command passes; full `apps/web-server/tests` passes.
   Traps: keep the 403 check before the set.

## Tests

- `apps/backend/.venv/bin/pytest apps/web-server/tests -q -o asyncio_mode=auto` passes.
- `apps/backend/.venv/bin/ruff check apps/web-server` and
  `ruff format --check apps/web-server` with ruff 0.14.10 (CI pin).
- `apps/backend/.venv/bin/python scripts/cq_ratchet.py --base origin/dev --ruff apps/backend/.venv/bin/ruff --config standards/ruff.toml --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`
  and the `--tool mypy` variant, as cq-ratchet.yml runs them.
- Local venv: throwaway `apps/backend/.venv` in the worktree (mcp 2.3.0); never staged.

## Rollback

Revert the PR. Remote MCP returns to failing closed with "missing auth context".
