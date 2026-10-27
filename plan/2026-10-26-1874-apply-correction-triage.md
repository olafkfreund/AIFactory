---
status: approved
issue: 1874
spec: spec/2026-10-26-1874-apply-correction-triage.md
coders:
  1: codex
  2: codex
  3: codex
  4: claude
  5: claude
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
- Fix the stale `ApplyCorrectionRequest` docstring and the stale
  "additive / backward-compatible" comment on its `triage` field.
- Added in review of #1881: the MCP `http_client` rewrites `/api/...` to
  `/api/mcp-stdio/...`, and the stdio proxy had no apply-correction route, so
  every call 404'd. Add `POST /api/mcp-stdio/tasks/{task_id}/apply-correction`
  (scope `TASK_WRITE_SCOPE`), build `ApplyCorrectionRequest` from the body
  (bad body: 422), delegate to `routes.execution.apply_task_correction`, and
  write one `mcp.task.apply_correction` audit row only on a confirmed success.

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

4. `apps/web-server/server/mcp_stdio/router.py` (before the "PR / merge"
   section) + `apps/web-server/server/services/audit_service.py` (next to
   `ACTION_MCP_TASK_APPROVE_PLAN`): add `ACTION_MCP_TASK_APPLY_CORRECTION =
   "mcp.task.apply_correction"` and the `proxy_apply_correction` route as in
   the decisions above; bump the module docstring's route count to 16.
   `apps/web-server/server/routes/execution.py:118`: replace the stale comment.
   → verify by ruff check/format on the three files.
   Traps: import `routes.execution` inside the handler like the other proxies
   (import cycle); call `apply_task_correction(task_id, body)` without
   `_access` so the REST route skips its own audit row.
   Deviation (CI ratchet, run 40363959848): copying the siblings raised the
   strict ruff count for `router.py` from 80 to 86 (B008 +2, PLC0415 +1,
   TID252 +3). Fixed without touching the siblings: a module-level
   `_TASK_WRITE = Depends(require_acw_scope(TASK_WRITE_SCOPE))` for the new
   route only; the handler import stays lazy but absolute
   (`from server.routes.execution import ...  # noqa: PLC0415`, import cycle);
   `ACTION_MCP_TASK_APPLY_CORRECTION` imported absolutely on its own line.
   Strict count is back to 80; the repo ruff config still passes. The I001
   at line 25 already existed on `origin/dev` and is not touched: the strict
   and repo configs disagree on it.
   Deviation 2 (CI on 66e62edd): the mypy --strict ratchet rose 42 -> 44
   for `router.py`, so the new route is now typed
   (`key: AuthenticatedKey | _LegacyAdminKey`, `-> dict[str, Any]`); the
   generated `apps/web-server/openapi.yaml` and `openapi-optin.yaml` are
   regenerated with `scripts/generate-openapi-spec.py` (base and `--opt-in`).
5. `tests/audit/test_task_action_audit.py` (end of file): `TestClient` on the
   real mcp-stdio router with the legacy admin token, reusing the
   `rows` / `correction_calls` / `_flag` helpers: valid triage → 200, triage
   reaches `apply_correction`, one `mcp.task.apply_correction` row; no triage →
   422 naming `APP_REQUIRE_CORRECTION_TRIAGE`, nothing applied, no row.
   → verify by `apps/backend/.venv/bin/pytest tests/audit/test_task_action_audit.py -q`
   Traps: patch `log_audit_event_bg` on the router module too (it imported
   the sink by name).

## Tests

`apps/backend/.venv/bin/pytest tests/test_mcp_task_control.py -q` passes;
the two new tests fail against `origin/dev` code.
`apps/backend/.venv/bin/pytest tests/audit/test_task_action_audit.py -q`
passes; the two proxy tests 404 against `origin/dev`. Ruff check and format
clean on the three touched files.

## Rollback

Revert the PR. No data, config or route behaviour changes.
