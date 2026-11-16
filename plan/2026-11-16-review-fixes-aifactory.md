---
status: draft
issue: 2067
spec: spec/2026-11-16-review-fixes-aifactory.md
coders:
  1: you
  2: you
  3: you
reviewers:
  - codex
---

# Plan: CLI login terminal ownership

Decisions: route binds owner and uses the session object; `may_use` skips the project check only for project-less owned sessions.

## Steps
1. apps/web-server/server/routes/cli_accounts.py start_cli_login_terminal: owner_id from request.state.user, `terminal_id = session.id`, `session.write(...)` -> verify by new route test.
2. apps/web-server/server/pty/access.py may_use: project-less owned session allowed -> verify by new may_use test.
3. tests/test_terminal_ownership.py (two new tests), tests/test_ws_org_routing.py (mock returns object with id/write).

## Tests
`PYTHONPATH=apps/backend:apps/web-server:. pytest tests/test_terminal_ownership.py tests/test_ws_org_routing.py`; both new tests fail on dev, pass after.

## Rollback
Revert the commit.
