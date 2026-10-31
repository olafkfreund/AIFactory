---
status: approved
issue: 1969
author: olafkfreund
---

# Intent: coder and QA model/thinking selection reads the source spec dir

## Problem

In worktree mode the build agent can write its worktree copy of
`task_metadata.json`. The coder loop (`agents/coder.py` ~457),
the parallel phase runner (`agents/parallel_integration.py` ~458) and the QA
loop (`qa/loop.py` ~232, ~335, ~541, ~635) resolve the model, provider and
thinking budget from that copy on every session. An agent can therefore pick
its own model, `pinnedModel`, provider or thinking level for later sessions,
and can downgrade its own QA reviewer (small model, no thinking).

## Proposed outcome

Model, provider and thinking selection for coder and QA sessions come from the
source spec dir (`source_spec_dir or spec_dir`, the #1952/#1967 idiom). A
worktree metadata edit changes none of them.

## Affected users and systems

AIFactory backend builds in worktree mode, and `run.py --qa`. Direct mode is
unchanged (one dir).

## Constraints

No behaviour change when worktree and source metadata agree. CLI `--model`,
the control-plane fallback env and routing policy keep their precedence.

## Open questions

None. Out of scope, noted for follow-up: `agents/token_attribution.py` ~514
(telemetry tier stamp only) and the per-subtask `model` field, which comes
from the plan (covered by plan review, #1940), not metadata.
`qa/correction.py` already receives the source spec dir from the REST route.
