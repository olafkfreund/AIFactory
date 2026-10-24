---
status: approved
issue: 1858
author: olafkfreund
---

# Intent: optionally require a triage envelope on apply-correction

## Problem

`POST /api/tasks/{id}/apply-correction` (`apps/web-server/server/routes/execution.py:1553`)
schema-validates the hand-back only when a `triage` block is present
(`apps/backend/qa/correction.py:260`). A request carrying only `fix_request_md`
skips validation and, with `confirm=true`, starts a paid QA Fixer run. Nothing
checks the shape or provenance of the request that starts the fix cycle.

## Proposed outcome

An operator can set `APP_REQUIRE_CORRECTION_TRIAGE=true`. With it on, a request
with no `triage` is refused with a 4xx and nothing is written or run. With it
off (the default for now), behaviour is exactly as today. A request with a
valid `triage` passes either way.

## Affected users and systems

- AIFactory web-server: the apply-correction route and `Settings`.
- TFactory: the code sender (`agents/handback/send.py`) already sends `triage`;
  three skills post markdown-only through the AIFactory MCP tool
  `task_apply_correction`, which cannot carry `triage` yet.
- Internal callers of `qa.correction.apply_correction` (PR-endgame auto-fix in
  `completion_orchestration.py`) call the function directly, not the route, and
  are not affected.

## Constraints

- Default must not break any current TFactory path.
- Smallest change: gate in the route, reuse the existing triage validation.
- No change to how a malformed `triage` is handled today.

## Open questions

- Default on or off? Decided (lead recommendation, user-approved 2026-10-24):
  checked TFactory `origin/dev`. `send.py` sends `triage`, but the
  `handback-to-aifactory`, `tfactory-fixloop` and companion skills send
  markdown-only, so the default is **off**. Filed olafkfreund/TFactory#1430 to
  send `triage` everywhere; flip the default when it lands.
