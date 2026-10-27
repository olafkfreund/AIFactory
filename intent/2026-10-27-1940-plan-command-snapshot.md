---
status: draft
issue: 1940
author: olafkfreund
---

# Intent: plan-command grants come only from the plan the build was allowed to start with

## Problem

`agents/coder.py` calls `seed_profile_with_plan_commands(project_dir, plan_file)`
before every coder/QA session, and `agents/parallel_integration.py` does the same
for every wave child. Both re-read the live `implementation_plan.json`, which the
build agent can edit. Names in `PLAN_GRANTABLE_COMMANDS` (including `npx`, `node`,
`pip`, `make`, `cargo`) found there are merged into the worktree's security
profile. Approval is checked only at build start (run.py pre-flight, the coder's
post-planner gate, StartTask). So after a human approves a plan, the agent can add
`required_commands` to its own plan and have them allowed at the next session.
Same on the kubejob backend. #1934 made such an edit invalidate the stored
approval, but nothing re-checks it between sessions.

## Proposed outcome

When the spec requires review before coding, every session in a run is granted
only the commands of the plan as it was when the run started (the copy the start
gate validated). A command added to the worktree plan mid-run is not granted.
The approved plan's commands are still granted. A run with no review required
behaves as today.

## Affected users and systems

AIFactory backend: `agents/coder.py`, `agents/parallel_integration.py`,
`project/analyzer.py`. Local and kubejob builds.

## Constraints

- No change for specs that do not require review, or for solo mode.
- Never widen grants from the live file mid-run; narrowing is fine.
- `trusted_plan.py` seeds once at signed-plan ingest from the plan it just
  verified; unchanged.

## Open questions

Second question from the issue, answered during investigation: yes, the agent can
write `.aifactory/.aifactory-security.json` directly (Write/Edit are allowed on
`./**` and only `.git` paths are guarded; the Bash hook allows `echo … >` and
`cp` into it), and the Bash hook re-reads it live with trust, without re-applying
`DENY_COMMANDS`. That is wider than this issue and needs a different design (the
hook trusting an in-process profile rather than an agent-writable file), so it is
filed as a follow-up, not fixed here.
