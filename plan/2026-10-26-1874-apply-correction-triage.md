---
status: approved
issue: 1874
spec: spec/2026-10-26-1874-apply-correction-triage.md
coders:
  1: codex
  2: codex
  3: codex
---

# Plan: let the MCP `task_apply_correction` tool send a triage report (#1874)

Coders: the 2026-10-26 wave brief routes implementation through
`codex-implement`; Claude reviews the diff and finishes any step it hands back.

Approved decisions (from the spec):

- Add an optional `triage` argument (`"type": "object"`) to
  `task_apply_correction`; pass it to `POST /api/tasks/{id}/apply-correction`
  unchanged; omit the key when absent. No nested schema, no other new
  arguments (`manifest_hash`, `correlation_key` ride inside triage).
- Description: the route's field text plus the keys TFactory's sender builds
  (`contract_version`, `source`, `failing_tests` items with `test_id` and
  `reason`, `has_visual_plan`, `manifest_hash`, `correlation_key`); the
  server validates `source` and needs at least one failing test with
  `test_id` + `reason`, or `has_visual_plan: true`.
- No local refusal. When triage was absent and the route returns HTTP 422,
  return an `isError` result that says a triage report is required and to
  pass `triage` or set `APP_REQUIRE_CORRECTION_TRIAGE=false` on the server,
  including the server's text. Other errors use `_format_error` as today.
- Fix the stale `ApplyCorrectionRequest` docstring.

## Steps

1. `apps/backend/agents/tools_pkg/tools/task_control.py:441-490`
   (`task_apply_correction`): add the `triage` property; set
   `payload["triage"]` only when `args.get("triage") is not None`; in the
   `except MCPHTTPError` branch, when triage is absent and `"HTTP 422"` is in
   `str(exc)` (format from `http_client.py`), return the clear error.
   → verify by `cd apps/backend && .venv/bin/ruff check agents/tools_pkg/tools/task_control.py`
   Traps: keep the confirm gate first; error results use the
   `{"content": [...], "isError": True}` shape of `_format_error`.
2. `apps/web-server/server/routes/execution.py:103-109`: rewrite the
   docstring: fields below `confirm` are the #467 typed-handback contract;
   `triage` is required while `APP_REQUIRE_CORRECTION_TRIAGE` is on (the
   default since #1872), and a markdown-only POST is accepted only with it off.
   → verify by `ruff format --check` on the file.
   Traps: docstring only, no behaviour change.
3. `tests/test_mcp_task_control.py` (after the existing apply-correction
   tests, ~line 578): add (a) triage dict passes through to `json["triage"]`
   unchanged, and the body has no `triage` key when not given; (b) a stub
   `request` raising `MCPHTTPError("AIFactory web-server returned HTTP 422: ...")`
   with no triage yields `isError` and text naming
   `APP_REQUIRE_CORRECTION_TRIAGE` and `triage`.
   → verify by `apps/backend/.venv/bin/pytest tests/test_mcp_task_control.py -q`
   Traps: use the file's existing `tools_by_name` fixture and
   `_content_text` helper; tests are async like their neighbours.

## Tests

`apps/backend/.venv/bin/pytest tests/test_mcp_task_control.py -q` passes;
the two new tests fail against `origin/dev` code. Ruff check and format
clean on the three touched files.

## Rollback

Revert the PR. No data, config or route behaviour changes.
