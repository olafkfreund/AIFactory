---
status: approved
issue: 1874
intent: intent/2026-10-26-1874-apply-correction-triage.md
---

# Spec: let the MCP `task_apply_correction` tool send a triage report (#1874)

## Design

Chosen (user decision): add an optional `triage` argument to
`task_apply_correction` and pass it through unchanged.

1. **Tool schema** (`apps/backend/agents/tools_pkg/tools/task_control.py`,
   `task_apply_correction`). Add `triage` with `"type": "object"`, the
   route's own type (`triage: dict | None` in `ApplyCorrectionRequest`). The
   description reuses the route's field description and names the keys
   TFactory's sender builds (TFactory `agents/handback/send.py`:
   `contract_version` + `CorrectionRequest.to_dict()` + `manifest_hash`,
   `correlation_key`), stating which ones the server validates
   (`source`, `failing_tests[].test_id`/`reason` or `has_visual_plan`).
   No nested schema is declared: the server validator
   (`apps/backend/qa/handback_contract.py`) is the source of truth.
2. **Payload.** When `triage` is given, put it in the POST body as-is. When
   absent, omit the key (the route default is `None`).
3. **Clear error.** If the call had no `triage` and the route answers
   HTTP 422, return an `isError` result saying a triage report is required by
   the server and the two ways out: pass `triage`, or set
   `APP_REQUIRE_CORRECTION_TRIAGE=false`. The tool does not refuse locally,
   because it cannot see the server setting and opt-out deployments must work.
   Other errors keep the existing `_format_error` path.
4. **Docstring.** Rewrite the `ApplyCorrectionRequest` docstring
   (`apps/web-server/server/routes/execution.py`) to say `triage` is required
   while `APP_REQUIRE_CORRECTION_TRIAGE` is on (default since #1872), and the
   markdown-only POST only works with it off.

5. **MCP proxy route** (added in review). The MCP `http_client` rewrites
   `/api/...` to `/api/mcp-stdio/...` (`apps/backend/agents/tools_pkg/http_client.py`),
   and `apps/web-server/server/mcp_stdio/router.py` had no apply-correction
   route, so every confirmed call got a 404 with or without triage. Add
   `POST /api/mcp-stdio/tasks/{task_id}/apply-correction`, gated by
   `TASK_WRITE_SCOPE` like start/stop/recover. It builds
   `ApplyCorrectionRequest` from the body (a bad body is a 422, not a 500)
   and delegates to `routes.execution.apply_task_correction`, so the triage gate
   and validation run unchanged. It writes one `mcp.task.apply_correction`
   audit row (new `ACTION_MCP_TASK_APPLY_CORRECTION`) only on a confirmed
   success, the same rule the REST route uses; the REST route skips its own
   row for proxied calls.
6. **Nit.** Drop the stale "additive / backward-compatible" comment on
   `ApplyCorrectionRequest.triage`.

## Alternatives rejected

- **Deprecate the tool.** Rejected by the user; the MCP path is still useful
  for an operator replaying a TFactory report.
- **Refuse locally when `triage` is missing.** Breaks deployments with the
  flag off; the client cannot read the server setting.
- **Declare the full triage JSON schema in the tool.** Duplicates
  `handback_triage.schema.json` and drifts; the route accepts a plain dict.
- **Also add `manifest_hash` / `correlation_key` arguments.** Not asked for;
  TFactory's triage block already carries both.

## Risks

- A 422 without triage for an unrelated reason gets the triage message. Low:
  the tool builds the body itself, so the only other 422 source is a schema
  mismatch, and the message still includes the server's text.

## Verification

- New tests in `tests/test_mcp_task_control.py`: triage passes through
  unchanged; a 422 with no triage returns the clear error naming
  `APP_REQUIRE_CORRECTION_TRIAGE`. Both fail on `origin/dev`.
- New tests in `tests/audit/test_task_action_audit.py` drive a `TestClient`
  through the real `/api/mcp-stdio/tasks/{id}/apply-correction` route: valid
  triage reaches the route (one `mcp.task.apply_correction` row); no triage
  gets the route's 422. Both 404 on `origin/dev`.
- `pytest tests/test_mcp_task_control.py`, `ruff check` / `ruff format --check`
  on touched files.
