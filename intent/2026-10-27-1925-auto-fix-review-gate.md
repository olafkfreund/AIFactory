---
status: draft
issue: 1925
author: olafkfreund
---

# Intent: manual auto-fix start must not force-run an unreviewed plan

## Problem

`POST /api/projects/{id}/auto-fix/{N}/start` reuses any existing spec dir whose
name carries `-gh{N}-` (or `-mr{N}-` / `-wi{N}-`) and starts it with
`force=True`. `should_pass_force` then sends `--force` to run.py, which skips
its approval pre-flight on both backends. The plan already exists, so no
planner runs and the post-planner gate is never reached: coding starts on a
plan that needs review and was never approved.

The route also has no project-role check; any authenticated user can trigger
it (and read/write the auto-fix config, which holds a bot token) for any
project. The config field `requireHumanApproval` is read by nothing.

## Proposed outcome

- Restarting an existing spec through auto-fix never skips the approval
  pre-flight. A spec that requires review and has no valid approval pauses
  for review; one with a valid approval, or no review requirement, runs.
- Freshly imported issues (including the background poll) behave as today.
- Auto-fix routes require a project role like the other project routes.
- `requireHumanApproval` makes auto-fix specs require review before coding.

## Affected users and systems

AIFactory web-server: `routes/auto_fix.py`, `services/auto_fix_service.py`.
Users of the GitHub Issues auto-fix view.

## Constraints

- Do not touch `agent_worktree_sync.py`, `agents/coder.py`, `review/state.py`,
  `cli/build_commands.py` (other lanes).
- Background poll path unchanged.
- No gitops/cluster change, no deploy.

## Open questions

None.
