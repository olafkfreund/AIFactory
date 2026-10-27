---
status: approved
issue: 1874
author: olafkfreund
predecessor: intent/2026-10-26-1431-correction-triage-default-on.md
---

# Intent: let the MCP `task_apply_correction` tool send a triage report (#1874)

## Problem

Since #1872, `APP_REQUIRE_CORRECTION_TRIAGE` is on by default and
`POST /api/tasks/{id}/apply-correction` returns 422 when the body has no
`triage` block. The MCP tool `task_apply_correction`
(`apps/backend/agents/tools_pkg/tools/task_control.py`) has no `triage`
input, so every confirmed call it makes now fails. The caller sees a raw
HTTP 422 and cannot fix it from the tool.

The `ApplyCorrectionRequest` docstring in
`apps/web-server/server/routes/execution.py` still says the markdown-only
POST keeps working, which has been false since #1872.

## Proposed outcome

- The tool takes an optional `triage` object and passes it to the route
  unchanged, so a caller with a TFactory triage report can apply a correction.
- A call without `triage` that the route rejects returns a clear error: send a
  triage report, or set `APP_REQUIRE_CORRECTION_TRIAGE=false` on the server.
- The `ApplyCorrectionRequest` docstring describes the current behaviour.

## Affected users and systems

- MCP clients (Claude Code, CFactory) that call `task_apply_correction`.
- AIFactory web-server route docs only; no route behaviour change.

## Constraints

- Do not invent triage fields: the shape is the route's `triage: dict | None`
  and TFactory's sender (`agents/handback/send.py`), validated server-side by
  `apps/backend/qa/handback_contract.py`.
- Deployments with `APP_REQUIRE_CORRECTION_TRIAGE=false` must keep working
  without triage, so the tool must not refuse locally.
- No route or validator change.

## Open questions

None. The user chose "add a pass-through `triage` argument" over deprecating
the tool.
