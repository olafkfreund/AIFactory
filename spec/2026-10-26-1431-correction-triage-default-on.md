---
status: approved
issue: 1431
intent: intent/2026-10-26-1431-correction-triage-default-on.md
---

# Spec: require a triage block on apply-correction by default (TFactory#1431)

## Design

The user's decision: flip the default to on. `apps/web-server/server/config.py`
`REQUIRE_CORRECTION_TRIAGE: bool = True`, with its comment updated (on since
TFactory#1436; set `APP_REQUIRE_CORRECTION_TRIAGE=false` to opt out). The route
check in `routes/execution.py` is unchanged. A test pins the default by
instantiating `Settings()` with the env var unset. The existing audit test that
posts a markdown-only confirmed hand-back adds a `triage` block so it keeps
testing the audit, not the gate. CHANGELOG `[Unreleased]` records the flip.

## Alternatives rejected

- Set the flag in factory-gitops only: the user chose the code default so every
  deployment is protected.
- Drop the flag: it is the rollback lever without a release.

## Risks

Any markdown-only sender now gets a 422: the AIFactory MCP
`task_apply_correction` tool when called without triage (documented fallback).
Rollback: `APP_REQUIRE_CORRECTION_TRIAGE=false` and restart.

## Verification

`pytest tests/audit/test_task_action_audit.py`; the default test fails on the
old code. CI green.
