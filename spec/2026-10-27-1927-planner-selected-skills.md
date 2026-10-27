---
status: draft
issue: 1927
intent: intent/2026-10-27-1927-planner-selected-skills.md
---

# Spec: remove the planner's selectedSkills step

## Design

Option 1 from the issue.

- Delete step "1.1b: Confirm the proposed skills" from `apps/backend/prompts/planner.md`.
- Fix the comments that say the planner may refine `suggestedSkills` into
  `selectedSkills`: `apps/web-server/server/routes/execution.py` (~1241) and
  `apps/web-server/server/services/skills_service.py` (`suggest_selected_skills`
  docstring).
- Guard test in `tests/test_hybrid_skill_selection.py`: the planner prompt does not
  mention `selectedSkills`.

Consumers checked on dev: `selectedSkills` is read only by `_write_skill_context`
(canonical copy, at dispatch) and set only by the control plane (create/update
routes, `trusted_plan.py` contract `skills`). No backend reader.

## Alternatives rejected

- Option 2, a build-written `selected_skills.json` validated against the catalog:
  new channel and validation for a gain only on re-runs, since `skill_context.md` is
  written before planning. Not justified by any current need.
- Keep the step as advisory text: still costs planner tokens and misleads.

## Risks

None at runtime: the step's output was already dropped. The planner no longer reads
`task_metadata.json` for skills; `skill_context.md` already carries the skills.

## Verification

The guard test fails on dev (the prompt mentions `selectedSkills`) and passes after.
`tests/test_hybrid_skill_selection.py` and `tests/test_task_skills.py` pass.
