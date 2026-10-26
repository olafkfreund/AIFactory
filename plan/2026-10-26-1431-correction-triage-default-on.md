---
status: approved
issue: 1431
spec: spec/2026-10-26-1431-correction-triage-default-on.md
---

# Plan: require a triage block on apply-correction by default (TFactory#1431)

Decision (approved): `REQUIRE_CORRECTION_TRIAGE` defaults to `True`;
`APP_REQUIRE_CORRECTION_TRIAGE=false` opts out. Route logic unchanged.
factory-gitops sets no value. Two file-editing steps, so implemented by the
session, not handed off.

## Steps

1. `apps/web-server/server/config.py` L239-242: `False` → `True`, comment says on
   by default since TFactory#1436, opt out with `APP_REQUIRE_CORRECTION_TRIAGE=false`.
   `tests/audit/test_task_action_audit.py`: add `triage={"contract_version": 1}`
   to the request in `test_apply_correction_audits_only_a_confirmed_success`; add
   `test_triage_required_by_default` (env unset, `Settings()` →
   `REQUIRE_CORRECTION_TRIAGE is True`).
   → verify: `.venv/bin/pytest tests/audit/test_task_action_audit.py -q` (from repo root, per CI's path setup)
   Traps: `settings` is built at import; the default test must build a fresh `Settings()`.
2. `CHANGELOG.md` `## [Unreleased]`: one entry for the flip with the opt-out.
   → verify: entry present.
   Traps: do not edit the 3.10.x sections.

## Tests

Audit tests pass; the default test fails on the old code. PR CI green.

## Rollback

Revert, or set `APP_REQUIRE_CORRECTION_TRIAGE=false` and restart.
