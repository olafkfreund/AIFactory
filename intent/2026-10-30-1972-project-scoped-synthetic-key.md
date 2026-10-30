---
status: draft
issue: 1972
author: olafkfreund
---

# Intent: project-scoped synthetic correlation keys

## Problem

When a task has no `<repo>#<n>` key, AIFactory emits the synthetic key
`af-<spec_id>` and TFactory emits `tf-<spec_id>`. Spec ids (`003-add-login`)
are numbered per project, so two projects with the same spec id emit the same
key. CFactory matches keys exactly (olafkfreund/CFactory#506), so both land on
one work item. #1960, olafkfreund/TFactory#1467 and olafkfreund/PFactory#960
sent more tasks down this path today. PFactory's `pf-<session_id>` is globally
unique and is not affected.

## Proposed outcome

- AIFactory's synthetic key is `af-<project_id>:<spec_id>` (its task id) on
  every emitter: completion, usage snapshot, worker and progress events, task
  rows, the from-issue response, and the TFactory handoff mirror.
- TFactory's synthetic key is `tf-<project_id>:<spec_id>` on the triager
  completion event and the hand-back.
- CFactory's `_upstream_key` fallback for an AIFactory intake response without
  `correlation_key` joins on `af-<task_id>`, the key AIFactory now emits.
- `<repo>#<n>` keys and contract or handoff keys are unchanged.
- Two projects with the same spec id get distinct keys (regression test per repo).

## Affected users and systems

AIFactory web server and build process (local and kubejob), TFactory backend,
CFactory card intake. PFactory: none.

## Constraints

- Keys stay within CFactory's 128-character column.
- No migration unless prod already holds `af-` keyed rows. Read-only aggregate
  check on 2026-10-30: 0 `af-` work items, 0 `af-` cards, 13 `tf-` work items
  (newest 2026-10-23, none in flight) out of 221 work items and 5 cards. No
  migration needed.
- Deploy when no issue-less task is in flight (same rule as #1960).

## Open questions

None.
