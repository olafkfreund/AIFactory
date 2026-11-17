---
status: draft
issue: 2079
author: olafkfreund
---

# Intent: AIFactory auth failures must reach the stream the alert reads

## Problem

The AIFactory code to push `auth.failure` events to OpenObserve (`audit_siem`, #2082) is merged, in the running image (sha-1d03c52) and default off. The deployed pod (factory-gitops `apps/aifactory`) sets no `AIFACTORY_AUDIT_SIEM_*`, so nothing is sent and `auth_failure_spike` (factory-gitops `apps/security-alerts`) still reports `no_field`. The earlier decision left open only "mint an ingest-only user"; nothing prepares the deployment or the owner step.

## Proposed outcome

- The aifactory Deployment carries the SIEM env: URL set, credentials from an optional Secret `observe-audit-ingest`. With the Secret absent the push stays inert (no behaviour change).
- One owner script creates the ingest user and the Secret without printing secrets. After it runs and the pod restarts, one bad-token request puts a row with `action = 'auth.failure'` into `audit_chain`.
- A test runs the rule's SQL against fixture rows shaped like AIFactory's record.
- #2079 closes only after that live row is seen.

## Affected users and systems

factory-gitops `apps/aifactory`, `apps/security-alerts` tests, docs; OpenObserve in `factory`; the owner (one script run).

## Constraints

Must not reuse the `observe-root` credential in the AIFactory pod (root in an app pod). No secret values handled or printed by agents. No live-cluster change by agents. Alert query stays unchanged.

## Open questions

None. Decision (owner delegation 2026-11-17): a dedicated ingest user is kept over `observe-root` because it limits blast radius at the cost of one owner command.
