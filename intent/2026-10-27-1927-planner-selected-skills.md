---
status: approved
issue: 1927
author: olafkfreund
---

# Intent: the planner stops doing skill-confirmation work that has no effect

Refs olafkfreund/Factory#1960.

## Problem

`apps/backend/prompts/planner.md` step 1.1b (#394) tells the planner to write a
`selectedSkills` array into its spec dir's `task_metadata.json`. That write has no
effect on the build it runs in:

- The only reader, `_write_skill_context` (`apps/web-server/server/services/agent_skill_context.py`),
  reads the canonical `task_metadata.json` once at dispatch, before the planner runs.
- Nothing in `apps/backend` reads `selectedSkills`.
- In a worktree build the planner writes the worktree copy. Since #1921/#1923 the
  worktree sync mirrors only an allowlist that excludes `task_metadata.json`, and on
  kubejob it never reached the canonical copy. Without a worktree the write lands in
  the canonical file, but only after `skill_context.md` was written, so at most it
  changes a later run's skills, and it is an agent writing a control-plane-owned
  file (#1921, #1922).

So the planner spends tokens on a step that is dropped, and two code comments still
describe the planner "refining" the proposals.

## Proposed outcome

The planner prompt no longer asks for `selectedSkills`, and the comments describe
the real flow: the build uses `selectedSkills` set by the operator, else the
matcher's `suggestedSkills`.

## Affected users and systems

AIFactory planner prompt; comments in `routes/execution.py` and
`services/skills_service.py`. No runtime behaviour change.

## Constraints

- `task_metadata.json` stays control-plane owned (#1921, #1922); no new channel back.
- Operator-chosen `selectedSkills` (UI, contract `skills`) keep working.

## Open questions

None. The issue's option 2 (a validated `selected_skills.json` channel) is left for
a separate issue if wanted; it would only help re-runs.
