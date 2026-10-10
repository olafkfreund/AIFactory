---
status: draft
issue: 1633
author: olafkfreund
---

# Intent: kubejob builds must report the tokens they spent

## Problem

The cockpit token panel shows "not instrumented yet" for builds that did
call a model. On dev (1c2df772) this is only partly fixed. The subprocess
backend reports spend at human_review (#740, #718) and live spend while a
build runs (#719). The kubejob backend reports live spend over the log
stream (#1249), but its end-of-run reporting still has these gaps:

- **Failed builds often send no usage.** The reaper's `_fail` and
  `reap_abandoned_tasks` both reach `_update_plan_status(..., "failed")`,
  which emits a `failed` completion event. But that event carries usage only
  if a `token_usage.json` reached the control plane (see next point), and
  `_update_plan_status` returns before the emit when the plan has no phases,
  which is the case for a build that died before writing one.
- **A stopped build sends nothing.** `_stop_kubejob_build` in
  `agent_kubejob.py` marks the row failed and sends a task-status update,
  but never emits a completion or usage event.
- **Inside the Job, usage is never pushed for these builds.**
  `handle_build_command` exits with `sys.exit` on failure and on the review
  pause (`build_commands.py`), and a crashed or killed Job never returns.
  Both skip the `maybe_push_usage` block in `cli/main.py`. On the packed path
  no `token_usage.json` reaches the control plane, so any emit has nothing
  to send.
- **A review pause is reported as completed or failed.** A Job that parks
  for review exits 0, and `_emit_kubejob_terminal_completion` always sends
  `completed`, or `failed` if the evidence gate downgrades it. No kubejob
  path sends a `human_review` completion event, and on the packed path the
  event carries no usage.
- **Job-owned tasks skip the spec-creation snapshot.** The #1628 early
  return in `agent_service.py` (around line 374) runs before the review
  snapshot.
- **Side issue on the subprocess backend:** the #1407 fire-once marker is
  written at the human_review emit. When a resumed build finishes, its
  final status and extra spend are never sent.

CFactory only counts usage from events that carry a usage block. With no
event, the panel cannot tell "no model was called" from "nothing reported
yet".

## Proposed outcome

- A kubejob build that fails, is reaped, times out or is stopped shows the
  spend it actually used in the cockpit, when that spend was recorded.
- A kubejob build paused for review shows a review state with its cost, not
  completed or failed.
- Job-owned tasks report spec-creation spend.
- Nothing ever overwrites real spend with zero.

## Affected users and systems

- Operators reading the cockpit token panel and spend totals.
- AIFactory web server: `agent_kubejob.py`, `build_backend.py`,
  `agent_service.py`, `completion.py`, `completion_orchestration.py`.
- AIFactory build CLI in the Job pod: `cli/main.py`, `cli/build_commands.py`.
- CFactory (consumes the events; no change needed in this repo).
- TFactory (its own usage only arrives at a terminal outcome; separate repo).

## Constraints

- No TFactory handoff or PR endgame on a failed, reaped, stopped or
  review-paused build.
- Same event envelope and transport. No new endpoint, header or secret. The
  event stays backward-compatible with the deployed CFactory.
- Emits are best-effort and never raise into the reconcile, reap or stop
  loops.
- Usage is a cumulative snapshot. Send full totals or no usage block at all;
  never an empty or zero block. The #1407 marker must still stop duplicate
  terminal and handoff events.
- Multi-replica: the replica handling a failure may not be the one that ran
  the build, and two replicas may detect the same failure. Usage must be
  readable from any replica, or the fix sends nothing.
- The #1628 ownership guard must keep stopping the server from finalising a
  Job-owned task.
- `token_usage.json` and the spec dir are agent-written and untrusted
  (as in #1672, #1673). Validate and bound the values; never take tenant or
  project identity from them.
- Overlaps #1669, #1670 and #1677 in the same kubejob functions.
- Lands on `dev`. CFactory and TFactory changes are separate PRs, and
  AIFactory must not depend on them.

## Open questions

1. Scope: fix only the AIFactory kubejob gaps, or also open matching issues
   in TFactory (usage only on a terminal outcome) and CFactory ("no model
   called" vs "nothing reported yet")?
2. Should stopped, reaped and abandoned builds report spend too, or only
   builds whose Job ran to a failure?
3. Resumed builds: should a build that resumes after human_review re-send
   its final status and usage (relaxing or splitting the fire-once marker),
   or is that a separate issue?
4. Gather runtime logs from the MyFriends build first, to confirm its
   backend and why live "running" snapshots never arrived, or go ahead on
   the code findings alone?
5. Land #1669, #1670 and #1677 first and rebase on top, or work in parallel
   and accept the conflicts?
