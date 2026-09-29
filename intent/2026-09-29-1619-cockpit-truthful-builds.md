---
status: approved
issue: 1619
author: olafkfreund
---

# Intent: the cockpit tells the truth about a running build

## Problem

A healthy kubejob build shows in the cockpit as **Stuck / Interrupted**, with
"Nothing building", a frozen progress bar, an age an hour older than reality,
and a **Recover** button. Measured on task
`888c19de…:022-myfriends-shared-core-remediat` while it was 49 minutes into a
real run, marking subtask C7 complete and writing files:

```
kubectl get job factory-aifactory-shared-core-remediat -> Running 0/1 49m
pod log last line                                      -> 47 seconds earlier

GET /api/tasks/888c19de…:022-…/running                 -> {"is_running": false}
```

Three separate defects produce that card, and they compound:

1. **`is_running` only knows in-pod subprocesses.**
   `agent_service.py:1500` is `return task_id in self.running_tasks`, and the
   only write to that dict (`agent_service.py:1196`) sits directly after
   `asyncio.create_subprocess_exec`. A kubejob build dispatches a Kubernetes Job
   and is tracked in the Postgres `job_state_store` instead, so it never enters
   the dict. `TaskCard.tsx:136` computes `isStuck = !checkTaskRunning(task.id)`
   five seconds after the card renders and every 30s after, so *every* kubejob
   build reads as stuck from five seconds in until it ends. The dict is also
   in-process, so even a subprocess build reads as stuck after a web-server
   restart.

2. **The coder is handed a spec path relative to a directory it is already in.**
   Its cwd is `/work/.aifactory/worktrees/tasks/<spec_id>` and it is told
   `.aifactory/…`, so the segment repeats and the read fails; the path also
   points at `worktrees/`, while the spec lives at `/work/.aifactory/specs/<spec_id>/`.
   In the run above this produced 26 failures against 32 successes in one
   window, and the agent coded without ever reading `spec.md` or
   `implementation_plan.json`.

3. **Timestamps are written without a timezone.**
   `agent_service.py:1200` writes `datetime.now().isoformat()`. The frontend
   parses it with `new Date(...)`, and JavaScript reads an offset-less ISO
   string as *local* time. The pod is UTC and the operator is on BST, so every
   age is an hour stale — a 32-minute-old task rendered "1h ago".

**Why this is worth fixing rather than explaining.** `POST /{task_id}/recover`
resets the task to `backlog` and can re-dispatch it, but it does **not** delete
the Kubernetes Job. So the most prominent button on the card, offered because of
a false verdict, resets the record under a live build and can put a second Job
on the same task while the first is still writing. The wrong signal is attached
to the destructive action.

## Proposed outcome

A build that is running says it is running, with an age that matches the clock,
and the Recover button does not appear — or refuses — while a Job for that task
is alive. A coding agent reads the spec and plan it was given, and a run where
that read fails says so rather than continuing quietly.

## Affected users and systems

- `apps/web-server/server/services/agent_service.py` (`is_running`, the
  timestamp writers) and the kubejob backend / `job_state_store` it must consult.
- `apps/web-server/server/routes/execution.py` — the `/running` and `/recover`
  routes.
- Whatever composes the coder's prompt paths (`apps/backend/agents/coder.py`
  and the kubejob manifest/runner).
- `apps/frontend-web/src/components/TaskCard.tsx` only if the fix cannot be made
  entirely server-side; the preference is server-side, so every client benefits.
- Not the build execution path itself: no change to how a Job is dispatched, run
  or reaped.

## Constraints

- **Do not make `is_running` lie the other way.** Reporting a dead build as
  running strands work and blocks `/start` ("already running"). The new source
  must be the one the reaper already keeps truthful, not an optimistic guess.
- **`is_running` is called synchronously** from a route; the fix must not make a
  blocking Kubernetes API call per card render.
- **No change to the demo run in flight** — it must be observed, not disturbed.
- Prove it against a **real kubejob build**, not a unit test alone: the defect is
  precisely that unit-level reasoning about `running_tasks` looked correct.
- The timezone fix must not renumber or re-render existing stored timestamps
  inconsistently; mixed naive and aware values in one store is its own bug.

## Approved answers (2026-09-29)

All four answered with the recommendation as written:

1. `is_running` reads the dict **or** a set of active kubejob task ids that the
   existing reconcile poll maintains — no Kubernetes call or database
   round-trip on the request path.
2. `recover` **refuses** while a Job for that task is live, with an explicit
   `force: true` to override.
3. The frozen progress bar stays **out of scope**, with #1618 owning it; the
   card will stop saying "Stuck" before it starts showing progress.
4. The timezone fix covers **new writes**, reading tolerantly, with no
   migration of rows already stored.

## Open questions

1. **Where should `is_running` read from?** My recommendation: the existing
   reconcile poll maintains a `set[str]` of active kubejob task ids on
   `AgentService`, and `is_running` checks the dict *or* that set — no new
   Kubernetes call on the request path, and the reaper already keeps the rows
   honest. The alternative, querying `job_state_store` per call, is simpler to
   read but puts a database round-trip behind a 30-second-per-card poll.
2. **Should `recover` refuse or force?** I recommend refuse by default with an
   explicit `force: true` to override, because the current behaviour silently
   diverges the record from a live Job. Refusing changes an existing API's
   behaviour, so it is a decision rather than a fix.
3. **Is the frozen progress bar in scope?** The bar and the Plan/Code/QA steps
   stay grey because the Job cannot report progress at all (#1618). I recommend
   keeping that out of this change: it is a design change, and this one is a
   correctness fix. The risk of splitting is that the card still looks inert
   even once it stops saying "Stuck".
4. **Should the timestamp fix include a migration** for rows already written
   naive, or only new writes? I lean to new writes plus reading tolerantly,
   since the stored values are recent and self-correcting, but a store that
   mixes both formats will mislead until the old rows age out.
