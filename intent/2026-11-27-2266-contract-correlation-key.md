---
status: approved
issue: 2266
author: olafkfreund
---

# Intent: a trusted-plan task reports under its contract's correlation key

## Problem

In E2E run `mvr-20261127u` (Factory#3882, Factory#3939), one `factory submit` run shows up in
CFactory as separate, unjoined work items.

- **PFactory** reports under the signed contract's key, `pf-337-wordfreq-…`.
- **AIFactory** has the same key on disk, in the spec's `context/task_contract.json`, but none of
  its outputs use it:
  - its completion and progress events use `af-<project>:<spec>`, because the task has no GitHub
    issue;
  - its task list returns the same `af-` key, or none at all once the project is gone. CFactory's
    poller then falls back to the bare task id and prunes the `af-` row, and AIFactory's usage
    goes with it.

As a result, CFactory has no single timeline and no AIFactory cost for the run. The E2E X oracle
fails, and the cockpit can't show what a build cost.

## Proposed outcome

When a task's contract carries a correlation key, AIFactory uses it in all of these:
- the task list's `correlationKey`;
- every completion, worker and progress event;
- the job-state row.

CFactory then threads PFactory and AIFactory onto one work item, with AIFactory's usage on it.
Tasks without a contract key behave as today.

## Affected users and systems

- **AIFactory web-server:** `services/completion.py`, `routes/task_service.py`,
  `services/agent_service.py` and their tests.
- **CFactory and the E2E harness X oracle:** consumers only, no change.

## Constraints

- **Validate the key.** The key is read from a file in the spec dir, which the agent can write. It
  must be validated (a safe character set, at most 128 characters, CFactory's column width), and a
  bad value falls back to today's key.
- **No regression for other tasks.** Tasks from issue intake keep `<repo>#<n>`, and keyless tasks
  keep `af-…`.
- **AIFactory release only.** The TFactory side, the `factory test` key, is a separate change
  under Factory#3939.

## Open questions

None.
