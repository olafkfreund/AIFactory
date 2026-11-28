---
status: approved
issue: 2272
author: olafkfreund
---

# Intent: AIFactory's task list carries the correlation key

## Problem

CFactory's AIFactory poller reads `GET /api/tasks`. That list returns `Task` models with no
correlation-key field, so the poller files every task under its bare task id. #2266 gave the key
only to the detail endpoint.

As a result, a `factory submit` run splits in CFactory, and the AIFactory slice, with its usage,
is pruned from the joined work item. E2E `mvr-20261128z` showed this: the X check's
`cost_reconciled` was false.

## Proposed outcome

The task list carries the same key the AIFactory events carry. CFactory then keeps one work item
per run, with AIFactory's usage on it.

## Affected users and systems

AIFactory `routes/task_models.py` (`Task`), `routes/task_service.py` (`spec_to_task`), the
generated `openapi.yaml`, and the tests. Consumers are CFactory's poller and every caller of
`spec_to_task`, which gains one optional field.

## Constraints

- The field is additive and optional.
- It uses the same key rule as the events: issue, then contract, then `af-`.
- AIFactory release only.

## Open questions

None.
