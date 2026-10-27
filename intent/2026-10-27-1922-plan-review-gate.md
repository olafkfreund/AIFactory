---
status: approved
issue: 1922
author: olafkfreund
---

# Intent: plan-review gate must not trust files the planner can write

## Problem

After the planner session, `run_autonomous_agent` (`apps/backend/agents/coder.py`)
decides whether to pause for human plan review by reading two files in the
worktree spec dir: `task_metadata.json` (`requireReviewBeforeCoding`, `mode`)
and `review_state.json` (`approved`). The planner has just had a full agent
turn with write access to that directory, and `review/state.py`
`is_approval_valid` accepts `{"approved": true}` with no `spec_hash` as a
"legacy" approval. A planner can therefore skip human plan review for its own
run by clearing the flag or forging an approval.

How an approval reaches a build today (confirmed on origin/dev):

- **Approval write:** `POST /tasks/{id}/approve-plan`
  (`apps/web-server/server/routes/plan_approval.py`) runs
  `ReviewState.approve(spec_dir)` on the MAIN spec dir
  (`<project>/.aifactory/specs/<id>/`, with a `spec_hash`), records
  `in_progress` in `task_control.json`, and restarts the build with `force=True`.
- **The build does not wait in-process.** The gate `return`s; the process
  exits; the task shows `human_review (plan_review)`. Approval starts a NEW
  build process.
- **subprocess backend:** `run.py --project-dir <project>` →
  `setup_workspace` → `copy_spec_to_worktree` rmtree's and re-copies the main
  spec dir into the worktree at every start. The gate reads the worktree copy.
- **kubejob backend (prod, packed path):** the control plane populates a build
  clone from the main spec dir, packs it, the Job unpacks it into an emptyDir
  `/work` and runs `run.py --spec <id> --project-dir /work --auto-continue
  [--force]`, which again copies `/work/.aifactory/specs/<id>` into a nested
  isolated worktree. The Job has no API token (control-plane secrets are
  excluded from its env) and pushes back only the plan, usage, logs, memory and
  the gate marker — not `review_state.json` or `task_metadata.json`.
- On resume the plan already exists, so no planner runs and this gate is not
  reached; the gate fires only right after a planner session in the same process.

So at process start the worktree spec dir is an exact copy of control-plane
state on both backends, and no agent has run yet. The forgery window is only
the planner turn inside the current process.

## Proposed outcome

- A task that requires plan review always pauses after its planner session
  unless a human approval, taken from control-plane state before any agent turn
  and bound to the exact plan now on disk, exists.
- Clearing `requireReviewBeforeCoding` / `mode` or writing `review_state.json`
  during the planner turn has no effect on the gate.
- An approval with no `spec_hash` is never valid.
- Solo mode and the trusted-plan fast path (`skipPlanning`) behave as before.

## Affected users and systems

`apps/backend/agents/coder.py`, `apps/backend/review/state.py`, their tests.
Every build on both backends that requests plan review.

## Constraints

- Must not touch `apps/web-server/server/services/agent_worktree_sync.py`
  (AIFactory#1921 lane).
- No new network call from the Job: it holds no API credential, and none is needed.
- Must not change gitops, cluster config or deploy.

## Open questions

None. Who still writes hash-less approvals is answered in the spec.

Refs olafkfreund/Factory#1960
