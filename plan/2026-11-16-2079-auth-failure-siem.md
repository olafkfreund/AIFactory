---
status: draft
issue: 2079
spec: spec/2026-11-16-2079-auth-failure-siem.md
coders:
  1: codex   # small tricky logic: new module plus tests
  2: you     # three-call-site wiring, reviewed against the module
  3: you     # docs, env example, chart: mechanical, lead edits
reviewers:
  - codex    # independent read of the final diff
---

# Plan: AIFactory auth.failure to OpenObserve (#2079)

Decisions carried from the spec: push from AIFactory (no second forwarder); field `action = "auth.failure"` unchanged; record has no token, password, email or path; default off; credentials only via env from a Secret; push is background and swallowed; login and refresh failures are now audited; PR-body attribution gated by `AIFACTORY_AGENT_COMMIT_TRAILERS`; gh test renamed; docs and chart updated, default off.

## Steps
1. `apps/web-server/server/services/audit_siem.py` (new) and `apps/web-server/tests/test_audit_siem.py` (new): `siem_config()` returning `(url, auth)` or None, `async emit(action, **fields)` using `httpx.AsyncClient(timeout=3)`, and `schedule(action, **fields)` that keeps a strong ref to the task. Tests inject `httpx.MockTransport` through a module-level `_client_factory`. → `pytest apps/web-server/tests/test_audit_siem.py`
   Traps: no secret in log lines; ruff import sorting (plain names, no `as` in parenthesised imports).
2. `apps/web-server/server/auth.py` `_audit_auth_failure`: after the DB write call `audit_siem.schedule("auth.failure", resource_type="auth", reason=..., ip=...)`. `apps/web-server/server/routes/auth_routes.py` `login` (add `request: Request`) and `refresh`: `await _audit_auth_failure(request, reason=...)` before the 401s for bad password / bad refresh token (not the deactivated-account 403, not no-token probes). `auth.py` `verify_websocket_token` and `authenticate_websocket`: same call before the final `close(4001)` for a presented-but-invalid token (`invalid_ws_token`). Extend tests in `tests/audit/test_security_event_coverage.py` style. → `pytest tests/audit/test_security_event_coverage.py apps/web-server/tests/test_audit_siem.py`
   Traps: an audit failure must never change the status code; refresh needs `request: Request` too.
3. Lows. `tests/test_github_env.py` rename + docstring. `apps/web-server/server/services/pr_endgame.py` `_pr_title_body`: gate `who` on the trailer flag (export `enabled()` from `core/commit_trailers.py` by renaming `_enabled`; update its one other use) and update `test_pr_endgame.py` (on/off cases). Docs: `docs/docs/environment-reference.md`, `.env.example`, `charts/aifactory/values.yaml` and `templates/deployment.yaml` (`modelGateway`, `auditSiem`), CHANGELOG. → `pytest tests/test_github_env.py apps/web-server/tests/test_pr_endgame.py tests/test_commit_trailers.py`; `helm template charts/aifactory` default and with `--set auditSiem.url=... --set modelGateway.url=...`; `ruff check`, `ruff format --check`.
   Traps: autonomy matrix is generated, do not hand-merge; vendored files untouched.

## Tests
`pytest` on the files named above plus `tests/audit`; ruff; helm template. All green before push.

## Rollback
Revert the PR; with `AIFACTORY_AUDIT_SIEM_URL` unset the feature is inert, so a revert is not urgent.
