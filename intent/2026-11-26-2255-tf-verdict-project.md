---
status: draft
issue: 2255
author: olafkfreund
---

# Intent: the merge gate asks TFactory for the verdict of the right project

## Problem

`pfactory.tfactory_client.tfactory_verdict` (`apps/backend/pfactory/tfactory_client.py:1281`)
fetches `GET /api/tfactory/tasks/{spec_id}` and `.../verdicts.json` by spec id alone.

Spec ids are only unique within a project. When another registered TFactory project has the same
spec id, TFactory's `resolve_task_project` cannot pick one and answers `409 "Specify project_id for
this task"`. Every E2E journey builds the fixture spec `001-wordfreq-deterministic-word-fr` in a
new project, so this always happens there. Two real projects can collide the same way.

AIFactory reads the 409 as "no trusted verdict" (`None`). In E2E run `mvr-20261126n`
(Factory#3882), TFactory's verdict was clean (6 accepted, 1 flagged, 0 rejected) and CI was green,
yet the merge gate never went green and the PR never auto-merged.

## Proposed outcome

- AIFactory asks TFactory for the task and its verdicts **in the project it already expects**: the
  `project_id` that `tfactory_verdict` computes today (`TFACTORY_PROJECT_ID` or the AIFactory
  project name). TFactory's existing `?project_id=` parameter narrows the lookup, so a same-named
  spec in another project no longer causes a 409.
- Nothing else changes:
  - AIFactory still checks that the returned `project_id` matches;
  - it still requires `source_sha == head` and `status == "triaged"`;
  - only a `reject` fails;
  - any error is still `None`.
- Live proof: E2E P1 merges the PR (S4), and S5 reports the verdict on the merge SHA.

## Affected users and systems

- AIFactory `apps/backend/pfactory/tfactory_client.py` (the one caller that fetches a TFactory task
  by spec id) and `apps/backend/pfactory/test_tfactory_verdict.py`.
- Prod AIFactory, through a normal release.

## Constraints

- Make no change to TFactory. It already supports `?project_id=`.
- URL-encode the project id. It comes from config or a directory name.
- The merge gate stays fail-closed.

## Open questions

None.
