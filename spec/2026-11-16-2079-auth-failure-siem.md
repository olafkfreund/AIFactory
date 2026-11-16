---
status: approved
issue: 2079
intent: intent/2026-11-16-2079-auth-failure-siem.md
---

# Spec: AIFactory auth.failure to OpenObserve (#2079)

## Design
1. New `apps/web-server/server/services/audit_siem.py`: `emit(action, **fields)` POSTs one JSON record to `{AIFACTORY_AUDIT_SIEM_URL}/api/{ORG}/{STREAM}/_json` (OpenObserve native ingest, same endpoint the forwarder uses) with HTTP Basic from `AIFACTORY_AUDIT_SIEM_USER` / `AIFACTORY_AUDIT_SIEM_PASSWORD`. Off when the URL, user or password is empty. Record: `_timestamp` (microseconds), `action`, `source="aifactory"`, `resource_type`, `reason`, `ip`. The request path is NOT sent (it can carry ids or emails; it stays in the AIFactory DB row only). Defaults `ORG=default`, `STREAM=audit_chain`; both overridable. 3 s timeout, errors swallowed and logged as the exception class name only.
2. `auth._audit_auth_failure` (single chokepoint) schedules `emit` as a background task after the DB write, so the 401 never waits on OpenObserve. `routes/auth_routes.py` calls the same function on a failed login (`reason=bad_credentials`) and a bad refresh token (`invalid_refresh_token`), and the two WebSocket rejects in `auth.py` (`verify_websocket_token`, `authenticate_websocket`, `reason=invalid_ws_token`) do too; none wrote anything before. Only PRESENTED-and-rejected credentials count, as in #313: a missing token (`get_current_user`, WebSocket no-token) is probe noise and a deactivated account with the right password is not a credential failure. No email or path is sent.
3. Field contract: the alert's `action` field and value `auth.failure` are used unchanged, so factory-gitops needs no change. Rows from AIFactory carry no `id`, so the forwarder's `max(id)` high-water mark is unaffected.
4. Lows: rename the gh test to `test_stub_gh_...` (it runs the sh stub); document both gateway vars and the new SIEM vars in `docs/docs/environment-reference.md`, `.env.example` and chart values (`modelGateway`, `auditSiem`, both default off, passwords/tokens only via `secretKeyRef`); in `pr_endgame._pr_title_body` emit the agent/originator lines only when `AIFACTORY_AGENT_COMMIT_TRAILERS` is on, reusing the existing flag helper.

## Alternatives rejected
- Second forwarder CronJob reading AIFactory's `/api/audit/export`: needs a service principal, org-scoped NDJSON with UUID ids and its own cursor; more moving parts than one POST.
- Change the alert to query CFactory `kind`: CFactory records no auth failures, so the rule would still never see them.
- OTLP logs: the app only wires traces; a new exporter is larger than one HTTP call.

## Risks
Credential-stuffing flood becomes one POST each (bounded by the existing one-DB-write-each; ponytail note kept). Unreachable OpenObserve logs a warning per failure. Owner must mint an ingest-only OpenObserve user; until then the feature stays off.

## Verification
New unit tests with a fake HTTP transport: record shape and no secret fields, off by default, swallowed errors, login and refresh failures emit, 401 path schedules without awaiting. Existing `tests/audit/test_security_event_coverage.py`, `apps/web-server/tests/test_pr_endgame.py`, `tests/test_github_env.py`, `helm template` with defaults and with values set.
