---
status: approved
issue: 1987
author: olafkfreund
---

# Intent: a coder-written QA pre-approval must not skip QA when review is required

## Problem

`should_run_qa(spec_dir)` and `is_qa_approved(spec_dir)` (`apps/backend/qa/criteria.py`)
read `qa_signoff` from `implementation_plan.json`. The coder session can write
that file, so it can mark its own work `approved`. `cli/build_commands.py`
(~615, ~663) then skips `run_qa_validation_loop` and only reports
"pre-approved". `run_qa_validation_loop` (`qa/loop.py` ~222) has the same
early return, and the reviewer reads its verdict from the same field, so a
stale coder approval also counts as the reviewer's verdict if the reviewer
session writes nothing.

Re-verified on origin/dev 8497484b (2026-10-31): unchanged.

## Proposed outcome

Owner decision (2026-10-31), option (c): when the task requires review
(`RunGates.requires_review`, snapshotted at run start, #1983), a coder-written
`qa_signoff.status=approved` is ignored and the QA loop always runs.
When review is not required, behaviour is unchanged.

## Affected users and systems

AIFactory backend build path (`run.py` build command, in-pod and Job builds).
Review-gated tasks pay one extra QA session on resumed builds (accepted by the owner).

## Constraints

- Use only the run-start snapshot; read nothing new from the agent-writable worktree.
- No change for tasks without required review.
- The `--qa` command (`cli/qa_commands.py`) is out of scope.

## Open questions

None.
