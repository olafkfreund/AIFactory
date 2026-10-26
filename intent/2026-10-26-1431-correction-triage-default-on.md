---
status: approved
issue: 1431
author: olafkfreund
predecessor: intent/2026-10-24-1858-correction-triage-gate.md
---

# Intent: require a triage block on apply-correction by default (TFactory#1431)

## Problem

`POST /api/tasks/{id}/apply-correction` can refuse a hand-back with no
structured `triage` block (#1858), but `APP_REQUIRE_CORRECTION_TRIAGE` is off
by default, so markdown-only hand-backs still reach the QA Fixer. It was off
only until TFactory sent triage on every handback path. TFactory#1436
(released in TFactory 0.9.42, now running) did that.

## Proposed outcome

With the variable unset, a hand-back without `triage` gets a 422 before anything
is written. `APP_REQUIRE_CORRECTION_TRIAGE=false` remains as the opt-out.

## Affected users and systems

AIFactory web-server (apply-correction route), TFactory handback senders, the
AIFactory MCP `task_apply_correction` tool (markdown-only fallback, which
TFactory#1436 already marks as rejected once this is on).

## Constraints

- TFactory 0.9.42 must be running first. Done.
- factory-gitops must not set the old value. Checked: no manifest under
  `apps/*/manifests` sets `APP_REQUIRE_CORRECTION_TRIAGE`.

## Open questions

None. The user pre-approved flipping the default for the 2026-10-26 wave.
