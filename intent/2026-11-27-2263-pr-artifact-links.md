---
status: approved
issue: 2263
author: olafkfreund
---

# Intent: the auto-PR body names the task's intent, spec and plan

## Problem

E2E P1 run `mvr-20261127r` (Factory#3882) was the first run where the factory auto-merged a build
end to end. The campaign's S4 oracle still failed, because the PR description does not mention
the task's `intent/`, `spec/` and `plan/` files.

The task came in through `factory submit`. The CLI sent the three approved files to PFactory with
their repo paths. PFactory signed them into the contract as `provenance.source_artifacts`, and
AIFactory copied that list into the task's `requirements.json`. The body builder,
`pr_endgame._pr_title_body`, already reads that provenance for the issue number, but ignores the
artifact list. The managed artifact workflow requires every PR description to link all three files.

## Proposed outcome

When a task carries `source_artifacts`, its auto-PR body has an "Artifacts" section that names the
intent, spec and plan paths. Tasks without them, such as plain issue intake, get the same body as
today.

## Affected users and systems

- AIFactory web-server: `apps/web-server/server/services/pr_endgame.py` (`_pr_title_body`) and its
  tests.
- Every PR the factory opens for a `factory submit` task, and the E2E harness's S4 check.

## Constraints

- The paths come from a client-attested record, so they are untrusted text in the PR body. Only
  well-formed `intent/`, `spec/` and `plan/` Markdown paths are written, and nothing else from the
  record.
- Nothing else in the body changes: the description, the evidence block, the attribution lines and
  `Fixes #N` stay as they are.
- A normal AIFactory release, no host changes.

## Open questions

None. Searching the repo for artifacts when the provenance has none is left out: the only tasks
with artifacts arrive through `factory submit`, and those carry the list.
