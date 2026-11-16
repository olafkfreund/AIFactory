---
status: approved
issue: 2079
author: olafkfreund
---

# Intent: AIFactory auth failures in the OpenObserve audit_chain stream (#2079)

## Problem
factory-gitops `apps/security-alerts` rule `auth_failure_spike` counts `action = 'auth.failure'` rows in the OpenObserve `audit_chain` stream (Factory#3688). That stream is fed only by `apps/audit-siem-forward` from CFactory's chain, whose rows are keyed by `kind`, so the rule reports `no_field` and can never fire. AIFactory already writes a hash-chained `auth.failure` row (`server/auth.py` `_audit_auth_failure`) but only into its own Postgres, and only for a rejected bearer token: a failed `/login` password or a bad refresh token leaves no audit row at all.

Also open, small: `tests/test_github_env.py::test_real_gh_reads_token_without_rewriting_hosts` runs a `sh` stub, not `gh`; `AIFACTORY_MODEL_GATEWAY_URL`/`_TOKEN` are undocumented outside code and CHANGELOG; the PR body `Authored by agent` / `Originator` lines ignore `AIFACTORY_AGENT_COMMIT_TRAILERS` (#2062), which gates the same data in commit trailers.

## Proposed outcome
With a SIEM URL configured, every AIFactory auth failure (rejected token, failed login, bad refresh token) also lands in `audit_chain` with `action = "auth.failure"`, so the alert counts it. With it unset (default) nothing changes. The three lows are fixed and documented, chart values default off.

## Affected users and systems
AIFactory web-server (`auth.py`, `routes/auth_routes.py`, new `services/audit_siem.py`, `pr_endgame.py`), chart `charts/aifactory`, docs. Owner supplies an OpenObserve ingest credential (not done here).

## Constraints
No tokens, passwords or emails in the event; only action, reason, path, client IP (already stored in the AIFactory audit row). Push must never delay or fail the 401. Default off. No change to the CFactory forwarder or hwm logic (AIFactory rows carry no `id`).

## Open questions
None. Decision (owner delegation 2026-11-16): push directly from AIFactory (recommended) rather than a second forwarder CronJob, which would need an AIFactory service token and an id cursor that does not share CFactory's `max(id)` high-water mark.
