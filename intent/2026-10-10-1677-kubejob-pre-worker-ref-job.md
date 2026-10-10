---
status: approved
issue: 1677
author: olafkfreund
---

# Intent: a kubejob build whose worker ref write fails keeps running unseen

## Problem

`KubeJobBuildBackend.dispatch`
(`apps/web-server/server/services/build_backend.py:1204`) creates the
Kubernetes Job at `:1287` and only then records it on the durable row with
`set_worker_ref` at `:1294-1297`. That write has no error handling around it. If
it raises (a database or session error), `dispatch` raises while the Job is
already running in the cluster. The issue cites `:1281-1291`; the code has
moved since it was filed.

The callers take a raise from `dispatch` to mean nothing is running:

- `_dispatch_build_job` (`agent_kubejob.py:362-366`) gives the pooled
  credential back and does not add the task to `_active_kubejob_task_ids` (#1662).
- `start_task_execution` (`agent_service.py:921-924`) and
  `_drain_queue_durable` (`agent_queue.py:160-170`) then `mark_terminal` the
  row as `failed`.

The row never names the Job, and it is no longer `running`.
`get_active_kubejobs` (`job_state_store.py:474-475`) selects only running rows,
so reconcile, the #1606 reaper and its Job-name rebuild never see the row
again. The effects:

- `is_running` stays False for good, so `_refuse_recovery_while_running`
  (`routes/execution.py:991-1012`) lets recovery reset a task whose Job is
  still live. That reopens the #1619 hole.
- /stop cannot delete the Job (`delete_job` needs a `k8s-job` ref).
- The credential goes back to the pool while the Job is still using it, which
  breaks the #670 rule of one token per Job.
- The concurrency slot is freed, so the cap admits one build too many.
- The build has no log stream, no console and no #1249 review re-drive.

This only happens on deployments that use the kubejob backend, and only when
the store write fails right after the Job is created. When it does happen, the
control plane shows the task as failed while its Job keeps writing to the
worktree and branch.

## Proposed outcome

When `dispatch` raises, no Job for that dispatch is left running, or the
durable row names the Job so that every replica can see, stop and reap it. In
both cases:

- The cockpit never shows a task as failed and recoverable while its Job runs.
- The recovery guard refuses recovery for as long as a Job for the task exists.
- The pooled credential stays held while any Job that mounts it is alive.
- Callers still record their existing failure reasons ("spawn failed during
  admission" / "spawn failed on dequeue"). The original error is not replaced.
- A test drives `dispatch` with a store whose `set_worker_ref` raises and shows
  that the Job does not stay running untracked.

## Affected users and systems

- Operators of deployments with the kubejob build backend (#671).
- `apps/web-server/server/services/build_backend.py` (`dispatch`,
  `delete_job`), `job_state_store.py` (`set_worker_ref`,
  `get_active_kubejobs`), `agent_kubejob.py`, `agent_service.py`,
  `agent_queue.py`, `routes/execution.py`.
- Tests: `tests/test_build_backend_kubejob.py`, plus the kubejob reap,
  `is_running` and mixin suites.

## Constraints

- Rebase onto `dev` before editing. This branch (b753db00) is missing #1691's
  `child_env` change in `build_backend.py`, and the PR must not undo it.
- Keep the contract that a raise from `dispatch` means nothing is running, or
  change all three call sites together.
- Do not weaken `is_running` or `_refuse_recovery_while_running` (#1619).
- Use only the RBAC the chart already grants (jobs create/get/list/watch/delete).
  No new verbs or resources.
- Act only on the Job this dispatch created: use the manifest name or the
  `factory.io/job-id` label, never a rebuilt name. Short names can collide.
- Do not depend on state held in memory on the replica that dispatched. Do not
  widen the #1669 or #1670 windows.
- Keep the #1628 rule that a `k8s-job` ref forces `running`. A crash at any
  point must still end in the #1606 reaper's "gone" path.
- If a cleanup step fails, log it with `sanitize_log`. Do not swallow it, and
  do not let it replace the original error.
- Do not touch the trusted-contract or merge files (#1671-#1673, #1683,
  #1686, #1687), or the build env passthrough and `child_env` (#1674,
  #1688-#1692).

## Open questions

1. **Strategy.** Should we delete the Job when the ref write fails and keep
   the rule that a raise from `dispatch` means nothing is running? Or should
   we write the ref before the Job is created? The second changes how the
   #1628 and #1606 paths behave and what a failed dispatch means.
2. **If the cleanup delete also fails.** Do we accept a Job that runs orphaned
   until its deadline or TTL, or add another path that is sure to reap it? In
   that case, what should the row and the credential show?
3. **The #1667 spawn stamp.** After a rolled-back dispatch, do we leave the
   stamp as it is or correct it? Correcting it touches the trusted-contract
   store that #1683, #1686 and #1687 are changing.
4. **Scope.** Do we fix only the untracked Job here? The other effects are the
   credential released early, the over-admitted slot and the missing log
   stream. Should they be fixed in this PR, filed separately, or folded into
   #1669 and #1670?
