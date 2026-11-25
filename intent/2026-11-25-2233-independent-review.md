---
status: approved
issue: 2233
author: olafkfreund
---

# Intent: every task gets an independent review

## Problem

A task can reach `human_review` / `awaiting_merge` without any reviewer other
than its own coder looking at the code. The E2E campaign (Factory#3882) found
this on the first P1 run that got past the plan hand-off: run `mvr-20261124`,
repo `e2e-20261125-P1-…`, PR #2. The task was a from-plan task at tier `low`.
It got no QA report, no PR review and no comment.

Four gaps combine.

1. **The tier turns on solo mode, and solo mode skips QA.**
   - PFactory's tier profile sets `skip_planning` for **low and medium**
     (`PFactory apps/backend/plan/emit/tier_profile.py:43`). AIFactory's own
     intake does the same (`apps/backend/intake/execution_block.py:115-119`).
   - AIFactory reads `skipPlanning` as solo mode
     (`apps/backend/solo_mode.py:116-125`).
   - Solo mode sets `skip_qa = True`
     (`apps/backend/cli/build_commands.py:315-316`).
2. **Two other routes skip QA as well.**
   - When review is not required, a coder-written
     `qa_signoff: approved` counts as QA pre-approval
     (`apps/backend/qa/criteria.py:53-61,95-109`,
     `build_commands.py:665-750`).
   - Quick mode passes `--skip-qa`.
3. **Coding and QA get the same model.**
   - PFactory's tier profile writes
     `phase_models = {coding: m, qa: m, qa_fixer: m}` with one tier model
     (`tier_profile.py:62-63`). For `low`, that model is haiku.
   - AIFactory honours those values as given (`trusted_plan.py:943,1012`,
     `phase_config.py:566-569`).
   - Other inputs can collapse the two phases onto one model in the same way:
     the single-model fallback, `pinnedModel`, `phaseRoutes` and
     `AIFACTORY_FALLBACK_MODEL` (`phase_config.py:528-596`).
   - AIFactory's own Auto preset already separates them (sonnet for coding,
     opus for QA).
4. **The model a phase actually used cannot be read back.**
   - `GET /api/tasks/{id}/token-usage` renders one top-level `model` and
     drops `workers` (`agents/token_attribution.py` `render_breakdown`).
   - QA sessions never call `record_turn`, so the QA model is not recorded
     anywhere (`qa/loop.py`, `qa/reviewer.py`).
   - For from-plan tasks, `GET /api/tasks/{id}` returns `metadata: null`,
     because `phaseModels` lives in `task_metadata.json` and the route reads
     only `requirements.json` (`routes/task_service.py:809-817`).

**What is separate.** The post-build PR review (`pr_endgame`,
`PRReviewService`) is a different mechanism. It runs only when auto-PR is on
(default off), and it stores its verdict locally without posting it. On the
fallback merger path (`merger.process_one`) the PR is opened with no review
at all.

## Proposed outcome

- **A review always runs.** Every task that reaches `awaiting_merge` has an
  in-build QA review and a QA report. Solo mode, a coder's self sign-off and
  quick mode cannot skip it.
  - A QA rejection or error still blocks the task, as it does today.
  - A task with no report never reaches `awaiting_merge`.
- **The reviewer is independent.** The QA model that is actually used
  differs from every model that coded the task.
  - PFactory's tier profile no longer seeds QA with the coding model.
  - AIFactory enforces the rule after all precedence is resolved: it picks a
    distinct reviewer, or refuses with a logged reason.
- **The models can be read back.** An AIFactory API read returns the model
  each phase actually used, QA included. The E2E harness can then verify
  "reviewer ≠ coder" without shell access to the pod.

## Affected users and systems

- **AIFactory:** build flow (`build_commands.py`), solo mode, QA criteria and
  the QA loop, phase-model resolution (`phase_config.py`), token attribution
  and the task read / usage API.
- **PFactory:** `plan/emit/tier_profile.py`, the phase models in the
  contract.
- **Factory:** the E2E harness S3 read-back (`scripts/e2e/run.py`), to be
  updated after the API exists.
- **Cost:** low and medium tasks gain one QA session.

## Constraints

- Owner decision (2026-11-25): fix the factories and keep the E2E oracle
  strict.
- Keep solo mode's planning shortcut. Only its QA skip goes.
- No change to merge policy, to the PR endgame or to the TFactory hand-off.
- Ship in this order: a PFactory release and an AIFactory release, each
  through its own dev → main promotion.

## Open questions

1. **Which review counts.** Is the in-build QA review (with its report) the
   required review, with the PR endgame review optional on top?
   Recommended: yes.
2. **Scope.** Does this apply to all tasks, or only to from-plan tasks?
   Recommended: all tasks.
3. **What counts as independent.** Is a different model enough, or must the
   provider differ too? Recommended: a different model is required; a
   different provider is preferred when one is configured.
4. **When no distinct reviewer can be chosen,** for example because
   `pinnedModel` or a forced fallback leaves only one model: should the
   factory refuse the task (it lands in Needs you) or override the pin for
   QA? Recommended: override for QA only, and log it; refuse only if no
   second model is configured at all.
5. **The token figure.** 1.97M input tokens over 2 turns, 988% of the
   window. `pctOfWindow` divides cumulative input, including cache reads, by
   the window size. File it separately? Recommended: yes.
