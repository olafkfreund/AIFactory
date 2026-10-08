---
status: draft
issue: 1662
author: olafkfreund
---

# Intent: a kubejob build counts as running from the moment it is dispatched

## Problem

The recovery guard added in #1619 refuses to reset a task while
`AgentService.is_running(task_id)` is true
(`_refuse_recovery_while_running`, `apps/web-server/server/routes/execution.py:981-1001`,
called from `recover_task` at `execution.py:1064`). For a kubejob build,
`is_running` answers from `_active_kubejob_task_ids`
(`apps/web-server/server/services/agent_service.py:1554`, declared at `:107`).

Only the reconcile loop writes that set. `reconcile_kubejob_builds` replaces
it wholesale from the rows it polls (`apps/web-server/server/services/agent_kubejob.py:719`),
and the loop ticks every 15 seconds (`agent_kubejob.py:754-783`, started from
the app lifespan at `apps/web-server/server/main.py:220`). A successful dispatch
in `_dispatch_build_job` (`agent_kubejob.py:268-379`; the backend `dispatch`
call is at `:348`) starts the log stream and emits `in_progress`, but it does
not add the task to the set.

So the guard is open in two windows:

- Between a successful dispatch and the end of the next reconcile tick, up to
  about 15 seconds.
- After a web-server restart, until the first tick finishes. The set starts
  empty (`agent_service.py:107`).

In either window `is_running` is false, so `POST /recover` resets the record
of a live Job. The Job keeps writing against the reset task. With autoRestart
on, a second Job can start on the same task. This is the failure #1619 set out
to prevent.

The other readers of `is_running` see the same gap. The cockpit badge
(`websockets/progress.py:51,85`, `routes/execution.py:254,269`) shows a
just-dispatched build as not running, and so offers Recover. The `/start`
check (`execution.py:684`) and the plan-approval check
(`routes/plan_approval.py:170`) do not see the build either.

#1635 made the hole smaller, because a row that carries a live Job reference no
longer reads as terminal. It did not close it. The log streamer already works
around the gap with a 45-second grace period (`_DISPATCH_GRACE_SECONDS`,
`agent_kubejob.py:32-37`, used at `:618-638`). That is a second copy of the
"is it alive yet?" answer, and the recovery guard does not have it.

## Proposed outcome

- As soon as `_dispatch_build_job` returns, `is_running(task_id)` is true for
  that kubejob build on the replica that dispatched it.
- If someone recovers a task in the window between dispatch and the first tick,
  they get the same 409 as for any other running build, unless they pass
  `force`.
- The cockpit shows the build as running, with no "Stuck" badge and no Recover
  button, from the moment it is dispatched.
- A failed dispatch never marks the task active.
- A finished build still drops out of the set within one tick, as it does today.
- No running build is reported as not running in the gap between a restart and
  the first tick. If that is out of scope, the remaining gap is written down
  and accepted.

## Affected users and systems

- `apps/web-server/server/services/agent_kubejob.py`: dispatch, reconcile, and
  the log-stream liveness check.
- `apps/web-server/server/services/agent_service.py`: `is_running` and the
  active set.
- Readers of `is_running` in `routes/execution.py`, `routes/plan_approval.py`
  and `websockets/progress.py`.
- Every build launch path, because all of them go through `_start_build_unit`
  (`agent_kubejob.py:381-433`): `start_task_execution`
  (`agent_service.py:903`) and the queue-drain paths (`agent_queue.py:90,139`).
- Operators who use Recover in the cockpit, and anyone who calls
  `POST /recover` through the API or MCP (`task_recover`).
- Only the kubejob backend, which is the live default (#671). The in-pod
  subprocess path already registers its process in `running_tasks` when it
  spawns.

## Constraints

- Keep the #1619 rule that the reconcile tick replaces the set wholesale and
  never adds to it piece by piece. A set that only grows starts calling dead
  builds alive, which strands them and blocks `/start`. Whatever marks a build
  active at dispatch must not bring that drift back.
- An early return from a tick because the store read failed must keep leaving
  the previous answer in place (`agent_kubejob.py:677-681`).
- `is_running` stays synchronous and cheap. It must not make a Kubernetes or
  database call per request, because the cockpit polls it.
- `force` must still override the recovery guard.
- Tests come first, as for #1619. A unit test must fail on today's `dev`: in
  the window after dispatch and before a tick, recover is refused.
- This fix must not change the in-pod subprocess backend.

## Open questions

1. Should the restart window be in scope? The loop runs a tick straight away
   at startup (`agent_kubejob.py:769-771`), so the window is only as long as
   the first tick. The other choice is to accept that gap and record it.
2. If dispatch marks a task active, should `_kubejob_still_active` drop its
   45-second grace period (`_DISPATCH_GRACE_SECONDS`) and read the set
   directly, so there is one answer to "is it alive"? Or should that clean-up
   be a separate issue?
3. A tick that starts before dispatch writes the Job reference, and finishes
   after the task was marked active, would replace the set without that task.
   Is closing this race in scope, or is it narrow enough to accept? (#1606 has
   `get_active_kubejobs` include rows that are still pending.)
