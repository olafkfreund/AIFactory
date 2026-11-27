---
status: approved
issue: 2258
author: olafkfreund
---

# Intent: accept TFactory's own test write-back commit as the verified head

## Problem

TFactory verifies a build at `source_sha`. Its triager's `git_writer` then commits the accepted tests
onto the PR branch and pushes them. That commit becomes the PR head. Two AIFactory gates bind to the exact head, so both refuse:

- **The TFactory verdict check** (`tfactory_verdict` in `apps/backend/pfactory/tfactory_client.py`)
  requires `source_sha == head`.
- **The independent QA gate** (`qa_gate.check` in `apps/web-server/server/services/qa_gate.py`)
  requires `reviewed_head == head`.

The QA gate also guards CFactory's "Approve (PR + merge)" button, which answers HTTP 409
`qa_stale` (seen on run `n`). The endgame then logs
`merge gate not green yet` until the run times out.

Seen live in E2E P1 run `mvr-20261126o` (Factory#3882):

- verified `db5fa75`, 5 accept and 1 flag, status `triaged`;
- TFactory's write-back commit `5246cd2` is the PR head;
- S4 never merges.

Run `n` hid this behind the 409 fixed in #2255.

## Proposed outcome

When the only change between the verified commit and the PR head is TFactory's own test write-back
commit, the gate trusts the verdict. P1 then merges at S4 and reaches S5.

## Affected users and systems

- AIFactory merge gate (`_build_merge_gate` / `_gated_head` in
  `apps/web-server/server/services/pr_endgame.py`).
- The QA gate and its callers: `pr_endgame`, `routes/pr.py` (create-pr, CFactory approve) and
  `completion_orchestration`.
- Every auto-merge task whose TFactory run writes tests back (`TFACTORY_TRIAGER_GIT_WRITE=1`, on in
  prod).
- No change to TFactory.

## Constraints

- **Fail closed.** Any head move other than TFactory's own commit, made directly on top of
  `source_sha`, still forces a new verdict.
- `git_writer` fetches the branch tip before it commits, and a plain `git commit` also includes
  anything already staged. The head's parent and its changed files must therefore both be checked.
- `tfactory_verdict` never raises.
- Normal release: AIFactory 3.21.18.

## Open questions

None. The owner approved the approach, with intent, spec and plan unseen (2026-11-26).
