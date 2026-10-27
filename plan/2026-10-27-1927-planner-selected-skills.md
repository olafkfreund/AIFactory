---
status: approved
issue: 1927
spec: spec/2026-10-27-1927-planner-selected-skills.md
---

# Plan: remove the planner's selectedSkills step

Approved decisions: option 1. Remove planner.md step 1.1b; fix the two comments that
describe the planner refining skills; add a guard test. No runtime change. Two steps,
implemented by the session model (below the coder handoff threshold; one code file
plus a prompt and a docstring).

## Steps

1. `apps/backend/prompts/planner.md:139-154`: delete section "1.1b";
   `apps/web-server/server/routes/execution.py:1239-1243` and
   `apps/web-server/server/services/skills_service.py:749-755`: drop "the planner may
   refine them into selectedSkills".
   → verify by `grep -rn "selectedSkills" apps/backend/prompts` (no hits)
   Traps: keep the "1.2" heading intact.
2. `tests/test_hybrid_skill_selection.py`: add a test that `planner.md` does not
   contain `selectedSkills`.
   → verify by `cd apps/backend && PYTHONPATH=. .venv/bin/pytest ../../tests/test_hybrid_skill_selection.py ../../tests/test_task_skills.py`
   Traps: must fail on dev.

## Tests

Above pytest command green; guard test red on dev.

## Rollback

Revert the commit.
