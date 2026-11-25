---
status: draft
issue: 2233
author: olafkfreund
---

# Intent: every task gets an independent review

## Problem

A task handed over from PFactory as an approved plan ("from-plan") with
difficulty tier `low` reaches `human_review` / `awaiting_merge` with no review
of its code:

- **QA does not run.** No QA report exists for the task. Solo mode treats the
  coder as its own QA and skips the QA loop
  (`apps/backend/cli/build_commands.py:311-316`, `agents/coder.py:227`).
- **QA would not be independent if it did run.** The auto profile routes both
  `coding` and `qa` to the cheap route, so they resolve to the same model
  (`phaseModels.coding = phaseModels.qa = claude-haiku-5-5`).
- **The models used cannot be read through an API.** `GET
  /api/tasks/{id}/token-usage` exposes one top-level `model`. Its `workers`
  field is never rendered (`agents/token_attribution.py` `render_breakdown`).
  The per-phase models are only in `task_metadata.json` on the pod.

The E2E campaign (Factory#3882) found this on its first P1 run that got past
plan hand-off (`mvr-20261124`, repo `e2e-20261125-P1-…`, PR #2). The PR got
no review and no comment, and was left waiting for a merge.

## Proposed outcome

- **A review always runs.** Every from-plan task, at every tier and in solo
  mode, gets a QA review and a QA report before it reaches `awaiting_merge`.
- **The reviewer is independent.** The resolved QA model differs from the
  resolved coding model. The auto profile picks a different model for QA, and
  an explicit configuration that makes them equal is refused or overridden,
  with a logged reason.
- **The models used can be read back.** An API read returns the model each
  phase actually used. The E2E harness can then check that the reviewer is not
  the coder without shell access to the pod.

## Affected users and systems

- AIFactory backend: build flow, solo mode, the phase-model resolution / auto
  profile, and the token-usage or task read API.
- PFactory → AIFactory from-plan tasks (all tiers).
- The E2E harness in Factory, `scripts/e2e/run.py` S3 read-back. It is updated
  in a separate Factory PR once the API exists.
- Cost: low-tier tasks gain one QA session.

## Constraints

- Owner decision (2026-11-25): fix AIFactory and keep the E2E oracle strict.
- Do not remove solo mode. It still collapses plan and implement into one
  session. Only the QA skip goes.
- No change to the merge or TFactory hand-off behaviour.
- Existing per-project phase-model settings keep working, except that a
  setting which makes QA equal to coding is no longer allowed.

## Open questions

1. **Scope of "every task":** should non-from-plan tasks (from an issue, or
   manual) also lose the solo QA skip, or only from-plan tasks? Recommended:
   all tasks, which is simplest and matches "every task gets an independent
   review".
2. **What counts as independent:** is a different model enough, or must the
   provider differ too? Recommended: a different model is required; a
   different provider is preferred when one is configured.
3. **Token figure:** the reported 1.97M input tokens over 2 turns (988% of
   the window), attributed to system instructions, look like a counting bug.
   Fold it into this task, or file it as a separate issue? Recommended:
   separate.
