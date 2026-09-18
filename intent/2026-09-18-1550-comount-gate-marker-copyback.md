---
status: approved
issue: 1550
author: Olaf Krasicki-Freund
---

# Intent: Gate evidence is lost on the co-mount Job path

## Problem

PR #1563 fixed two of the three ways the trailing-gate evidence marker
(`.trailing_gates_done`) is read from the wrong tree. It binds the marker to the
`aifactory/<spec>` branch tip, and it carries the marker out of a **packed** Job
through object storage. It left the third, the **PVC co-mount Job path**, which is
still the default whenever a build has no `workspace_uri`
(`services/build_backend.py:681-690`).

On that path the Job writes the marker into the task **worktree's** spec dir. Nothing
copies it to the main spec dir that the control plane reads:

- the object-store push from #1563 is a no-op there (no `WORKSPACE_URI`);
- the generic worktree-sync loop does not run under kubejob
  (`services/agent_kubejob.py:676-686`).

So on the default Job path the merger's PR body still says "no verification gates
recorded" for a build that ran them. That is a false negative, and it is the
honest-status line the merger exists to get right.

## Proposed outcome

On every build path (local, in-pod worktree, packed Job, co-mount Job), a build that
ran its trailing gates has that evidence readable from the main spec dir when the
merger and the QA guard look for it.

## Affected users and systems

- `apps/web-server/server/services/agent_kubejob.py` (kubejob reconcile / completion)
- Possibly `apps/web-server/server/services/completion.py` (where the packed-path fetch lives)
- `services/merger.py` PR bodies. `agents/tools_pkg/tools/qa.py` (the #1496 guard) is only a reader.
- Deployed kubejob builds that use the co-mount path

## Constraints

- #1496's guarantee holds: missing or unreadable evidence is reported as absent, never invented.
- A copy-back failure must not fail or delay the build's completion.
- No new sync loop. There is a precedent for copying one file:
  `review_redrive_service.check_review_obligation` syncs `qa_review_cycle.json` from the
  worktree itself (#1249).
- A marker copied into main must still be validated by the branch-tip binding from #1563,
  not trusted just because it exists.

## Open questions

1. Where should the copy-back hang: the completion path (next to #1563's packed-path
   fetch, one place for "bring the marker home"), or the kubejob reconcile, like #1249?
