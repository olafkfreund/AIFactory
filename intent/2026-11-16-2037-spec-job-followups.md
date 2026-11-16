---
status: approved
issue: 2037
author: olafkfreund
---

# Intent: settle the spec Job review follow-ups before the kubejob flag

## Problem

#2026 added spec creation as a Kubernetes Job (`AIFACTORY_SPEC_BACKEND=kubejob`,
#1932). Three review rounds left follow-ups (#2037) that are open while the
flag is off. Read from code, not yet seen at runtime:

- the fetched plan still carries the Job's `status`, `planStatus`, `reviewReason`
  and per-subtask `status`, which can block or overwrite control-plane writes;
- a transient S3 error on the plan reads like "no plan pushed" and can
  auto-approve `spec.md` without a plan;
- the Job poller has no local deadline, so a persistent API error holds the
  pooled Claude credential forever;
- Stop cannot interrupt the fetch-back, which still writes files;
- Job log lines are buffered without bound and one line over 64 KiB kills the
  consumer;
- smaller items: fetched files are 0600, two test gaps, per-process Job
  ownership, broad credential mounts, no size cap in the vendored store.

## Proposed outcome

The five blocking items and the 0600 and test-gap items are fixed with tests.
Items that need another repo, another team (#2025) or a design decision are
named in the issue comment, not changed here.

## Affected users and systems

`apps/backend/core/workspace_fetch.py`, `apps/web-server/server/services/spec_job_backend.py`,
`tests/test_spec_job_backend.py`. Only matters when the flag is on.

## Constraints

- The flag stays off. Turning it on in factory-gitops is an owner action.
- Keep the diff tight: teams a2025 and a2024 work in nearby files.
- `artifact_store.py` is vendored byte-for-byte from the Factory hub: not edited here.

## Open questions

None. Out of scope, listed for the owner: `_LIVE_SPEC_JOBS` multi-replica
ownership, credential mount gating (#2025), hub `get_bytes(max_bytes=)`.
