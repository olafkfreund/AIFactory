---
status: draft
issue: 1698
author: olafkfreund
---

# Intent: show the effective review tier, and a metadata/signed tier mismatch, on the PR

## Problem

Open on `dev`; nothing on `dev` mentions the effective tier in a PR body.
For a verified trusted task, `merge_disposition`
(`apps/web-server/server/services/pr_endgame.py:566-587`) takes the signed
`execution.review_tier` as the floor. When `task_metadata.json`'s `reviewTier`
is below it, it logs one warning ("reviewTier X below signed Y; deciding on
the stricter tier") and decides on the stricter tier (#1672). The merge is
safe. A reviewer of the PR sees nothing:

- The endgame PR body (`pr_endgame.py:1786-1795`) only carries the advisory
  path-floor note.
- The merger's PR body (`services/merger.py:263-294`) prints
  `- Review tier: {review_tier}`. That is the raw, agent-writable metadata
  value read off disk, not the tier the gate uses, and it is echoed without
  `_describe_tier`.

A metadata tier below the signed one means something on the build side
lowered it. That is possible tampering, and the reviewer is the one who
should see it.

## Proposed outcome

- Both PR bodies state the review tier the merge gate decides on, as a
  `_describe_tier` label.
- When the metadata tier is below the signed tier, the PR body carries a
  visible "possible tampering" warning naming both labels.
- No change to any merge decision.

## Affected users and systems

- `apps/web-server/server/services/pr_endgame.py` (`merge_disposition`,
  `run_pr_endgame`).
- `apps/web-server/server/services/merger.py` (`honest_pr_title_and_body`).
- Tests asserting PR body text: `apps/web-server/tests/test_merger.py`.
- PR reviewers (more lines in the body).

## Constraints

- Visibility only; merge safety is already correct and must not move.
- Anything echoed into a body goes through `_describe_tier` (py/log-injection,
  a crafted on-disk value must never reach the PR).
- The merger body must never claim verification ("review surface, not a
  certificate").
- Target `dev`, CHANGELOG entry.

## Open questions

1. The merger sweep passes `trusted=None` on purpose (`merger.py:324-328`), so
   it cannot see a signed tier. Look the record up there? Conservative answer,
   taken here: no. Its body says the tier was not checked against a signed
   contract, rather than calling it effective.
