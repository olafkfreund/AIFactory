---
status: draft
issue: 1669
author: olafkfreund
---

# Intent: every replica sees a kubejob build as running, not only the one that dispatched it

Follows #1662 (fixed in PR #1678, shipped in 3.8.3). The #1662 spec recorded
this gap as accepted (`spec/2026-10-08-1662-kubejob-active-at-dispatch.md:83-86`,
`plan/2026-10-08-1662-kubejob-active-at-dispatch.md:38-43`).

## Problem

Paths are relative to `apps/web-server/server/` unless they start with `charts/`.

Since #1662, a kubejob task counts as running from the moment it is dispatched,
but only in the memory of the replica that dispatched it
(`services/agent_kubejob.py:371-372`). Each replica has its own
`_active_kubejob_task_ids` set (`services/agent_service.py:107,111`).
`AgentService.is_running` reads only that set and `running_tasks`
(`services/agent_service.py:1569`). It never asks the durable store, even though
`JobStateStore.is_running` exists (`services/job_state_store.py:518-529`).

Another replica learns of the build only from its own reconcile tick
(`reconcile_kubejob_builds`, `services/agent_kubejob.py:690,733`), which runs
every 15 seconds (`:768-769`). Until then it says the live Job is not running.
During that window, on the replica that did not dispatch:

- **Recover** passes the guard (`routes/execution.py:1006`, called at `:1074`)
  and resets `implementation_plan.json` and the task status while the Job keeps
  running and writing. This is the #1619 hazard and the real harm. With
  `autoRestart`, `JobStateStore.admit` refuses a second Job
  (`services/job_state_store.py:226-230`), but the reset has already happened.
- **Stop** returns `404 "Task is not running"` for a live Job
  (`routes/execution.py:966-970`).
- **Status** (`routes/execution.py:260`, `:275`) reports `is_running:false`, so
  the cockpit shows the card as "Stuck" and offers Recover.
- **Plan approval** skips its stale-process cleanup (`routes/plan_approval.py:170`).
  That cleanup calls `stop_task`, which deletes a kubejob build too
  (`services/agent_service.py:1385-1389`). So on the dispatching replica,
  approving a plan stops a live Job; on any other replica it does not.
- **Start** passes the guard at `routes/execution.py:690`. The dispatching
  replica answers 409 "Task is already running" (`:703-706`), or, with an
  approved plan, stops the live Job first (`:691-701`, same `stop_task`).
  Another replica runs on: it may write `human_review` over the live build's
  status (`:738-771`) or take the delegation branch (`:788-812`) before it
  reaches `admit`.

The same request gets a different answer depending on which pod the load
balancer picks. `POST /start` does not double-dispatch: `admit` raises
`ValueError` (`services/job_state_store.py:226-230`). That error is not mapped
to 409, despite the comment at `services/agent_service.py:891`: the route's
`except Exception` turns it into a 500 "Failed to start task"
(`routes/execution.py:843-847`).

The reaper and the log streamer are already safe. `reap_abandoned_tasks` falls
back to the store through `_kubejob_liveness` (`services/agent_kubejob.py:871-874`),
and `_kubejob_still_active` has a 45-second grace period (`:625-646`,
`_DISPATCH_GRACE_SECONDS` at `:37`).

Reachability: the default install runs one replica. The pin to one replica
applies only when rmux is on (`charts/aifactory/templates/deployment.yaml:12`),
and rmux is off by default (`charts/aifactory/values.yaml:537`). An operator who
sets `replicaCount > 1` (`values.yaml:32`) or enables the HPA
(`values.yaml:191-194`) with rmux off is exposed today. The issue cites
`values.yaml:6`, which is only the comment; the setting is at line 32.

## Proposed outcome

- While a kubejob build is live, every replica refuses Recover with 409, unless
  `force=True` is set.
- Stop, status and the cockpit badge give the same answer on every replica.
- Recovery and the badge read the same predicate, so they never disagree.
- Single-replica installs behave exactly as today.

## Affected users and systems

- Operators who run more than one web-server replica with the kubejob backend.
- Cockpit users who click Recover or Stop, or read the running/"Stuck" badge.
- `services/agent_service.py`, `services/agent_kubejob.py`,
  `services/job_state_store.py`, `routes/execution.py`, `routes/plan_approval.py`.
- The chart (`charts/aifactory/values.yaml`, `templates/deployment.yaml`), at
  most for a comment or docs.

## Constraints

- Fail toward leaving the build alone (#1551). If the store cannot answer, that
  must not count as "not running". Only a proven terminal row or a proven
  absence does.
- With no durable store (one pod, dev), `is_running()` stays the full answer.
  The in-memory admission fallback keeps working.
- Keep the #1619 rule: the active set is replaced each tick, never only added
  to. A failed read keeps the previous tick's answer.
- Keep the #1662 reset-before-first-await rule and the union with
  `_kubejob_dispatched_this_tick` (`services/agent_kubejob.py:688`).
- `is_running` is synchronous, and every caller, including the async reaper,
  calls it without `await`. It must stay cheap: the cockpit polls status once per card.
- `force=True` still skips the guard. Recovery never deletes or resets a live
  k8s Job.
- `recover_task` is at the ruff branch limit; extra guard logic goes outside it.
- Store rows are keyed by `job_id == task_id`. A `running` row whose worker
  kind is still `pending` (slot granted, not yet claimed, #1606) is a live
  claim. `JobStateStore.is_running` checks only `running`; `queued` is the
  other active state (`services/job_state_store.py:46`).
- Do not touch unrelated `is_running` methods (`routes/github.py`,
  `changelog_service.py`, `changelog.py`).
- Use the existing `JobStateStore` session and credentials only. No new
  secrets, env vars or egress. Task ids go through `sanitize_log` before logging.
- This fix does not lift the replica pin. Multi-replica also needs Redis
  pub/sub and a multi-pod rmux.
- Existing tests for #1619, #1662, #1551 and #1001 stay green.
- Overlaps #1670 (log streamer grace period, same predicate) and #1677 (Job
  created before `set_worker_ref`, same guard). They need sequencing.

## Open questions

1. Scope: fix only the recovery guard, or every `is_running` caller (start,
   stop, status, plan approval, badge)? Guard-only lets the badge and the
   refusal disagree.
2. Timing: fix now, or wait until multiple replicas are actually planned?
3. Cost: is one DB read per status poll acceptable, or should only
   state-changing actions (recover, stop) ask the store?
4. Store read failure: should each action fail closed (treat as running) or
   proceed? Failing closed on `/start` blocks users during a DB hiccup.
5. Should `is_running` become async, with the caller changes that brings, or
   should the store check sit beside it, as in the reaper?
6. Subprocess builds on another replica (`running_tasks` is pod-local): in
   scope or out?
7. #1670 and #1677: one combined change or separate PRs, and in what order?
8. Verification: is a unit test with two `AgentService` instances sharing one
   store enough? There is no multi-replica environment today.
9. Plan approval and an approved `/start` stop a live kubejob build on the
   replica that sees it. Once every replica sees it, every replica will. Is
   stopping the Job there intended, or should those paths leave a kubejob
   build alone?
10. Should the chart comment say #1669 is a precondition for `replicaCount > 1`
   with kubejob?
