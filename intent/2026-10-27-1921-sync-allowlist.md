---
status: approved
issue: 1921
author: olafkfreund
---

# Intent: worktree spec sync copies agent-writable control files over the canonical spec dir

## Problem

`_sync_worktree_files` (`apps/web-server/server/services/agent_worktree_sync.py`)
mirrors the build's worktree spec dir over the canonical main spec dir every
~3s and at process exit. After its explicit `files_to_sync` list, an "extra
files" loop copies every other top-level file, minus a skip set
(Factory#1960 added `requirements.json`, `task_control.json`,
`qa_review_cycle.json`).

`copy_spec_to_worktree` seeds the worktree with the whole canonical spec dir, so
every control-plane file is there, and the agent can write that dir (the planner
prompt tells it to edit `task_metadata.json`). Each tick copies those files
back. Still overwritable: `task_metadata.json` (tenant stamp, `budgetUsd`,
`base_branch`, `github_repo` fallback, `reviewTier`), `review_state.json` (human
plan approval), `COMPLETED.json` and the terminal markers, `tfactory_handoff.json`,
`TFACTORY_HANDOFF.md`, `skill_context.md`, `.worktree_path`.

Confirmed impact on origin/dev:

- Auto-merge escalation is NOT reachable. The sync runs only from
  `process_monitor` (in-pod subprocess backend), where
  `trusted_contract.host_isolated()` is False and every merge disposition is
  `hold-blocking`. kubejob + store (prod) never calls the sync; its packed path
  fetches an explicit allowlist (`core/workspace_fetch.py`).
- On subprocess hosts (default backend; prod's fallback without
  `DATABASE_URL`) an agent can re-stamp the tenant, drop the budget, retarget the
  PR base/repo, lower `reviewTier` before the TFactory handoff, or mark its plan
  approved.
- With no agent action, the seed-time snapshot reverts control-plane writes made
  to the canonical copy during the build.

## Proposed outcome

The extra-files loop copies only build-produced files the control plane reads
from the main spec dir. An agent-written control file in the worktree never
changes the canonical copy.

## Affected users and systems

AIFactory web server, subprocess build backend. No change to kubejob/packed builds.

## Constraints

- Keep the explicit `files_to_sync` list and its plan-merge logic unchanged.
- Must not stop mirroring a file some reader needs (usage, injection scan,
  guardrail halt, handback receipt).
- No gitops or cluster change.

## Open questions

None.
