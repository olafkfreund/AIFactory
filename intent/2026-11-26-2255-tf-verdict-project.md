---
status: approved
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

Owner decision (2026-11-26): **AIFactory stores TFactory's project id.** Live checks on prod
showed that TFactory doesn't recognise AIFactory's project reference, the workspace directory
name `olafkfreund-e2e-…`:

- `?project_id=olafkfreund-e2e-…` returns 404;
- TFactory scopes a lookup by its project UUID or its name `e2e-…`;
- scoped responses return the UUID.

So `?project_id=` alone, with the directory name, can't fix this.

- **TFactory's ingest response already returns the resolved id** as `workspace_project_id`
  (`routes/specs.py`, `ingest_spec`). AIFactory currently keeps only `sent`, `reason`, `status` and
  `url` from that call, and drops the body.
- **AIFactory records it.** The handoff result saved to `tfactory_handoff.json` gains
  `tfactory_project_id`, taken from the ingest response body's `workspace_project_id`.
- **`tfactory_verdict` uses it** when it is present:
  - it adds `?project_id=<tfactory_project_id>`, URL-encoded, to the task request and to the
    `verdicts.json` request;
  - it requires the returned `project_id` to equal that same id.
- **Old tasks are unchanged.** A task with no recorded id (handed off before this release) uses
  today's lookup, so nothing regresses.
- **Unchanged checks:**
  - `source_sha == head`;
  - `status == "triaged"`;
  - only `reject` fails;
  - any error gives `None`;
  - there is never a retry with a bare spec id once an id is recorded.
- **Live proof:** E2E P1 merges the PR (S4), and S5 reports the verdict on the merge SHA.

## Affected users and systems

- AIFactory `apps/backend/pfactory/tfactory_client.py`: the handoff sender (`send_*`, which today
  keeps no response body), where the handoff result is written to `tfactory_handoff.json`, and
  `tfactory_verdict`, which is the one reader of a TFactory task by spec id. Tests are in
  `apps/backend/pfactory/test_tfactory_verdict.py` and the handoff tests.
- Prod AIFactory, through a normal release.

## Constraints

- Make no change to TFactory. It already returns `workspace_project_id` and supports `?project_id=`.
- URL-encode the project id. It comes from config or a directory name.
- The merge gate stays fail-closed.

## Open questions

None.
